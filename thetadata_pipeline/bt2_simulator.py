"""BT-2 top-level orchestrator -- ties the Section 6 selector, Section 7
fill models, and Section 8 exit engine together into one simulated trade,
and owns the trade-ledger writer (Section 4/16).
BOT_NEXUS_Options_Strategy_Backtesting_Roadmap.pdf Section 18's BT-2
acceptance gate: "Golden-path and adversarial tests pass" -- see
thetadata_pipeline/tests/test_bt2_simulator.py.

Isolation rule (same as the rest of this package): writes only under
data/thetadata/bt2_simulator/, never touches live_gex_snapshot.json,
vex_history.json, iv_intraday_state.json, or anything backfill.py/
collector.py already own.

Input contract: `trades_df` must already be normalize.classify_trades()'d
(carries contract_id/underlying/right/bid/ask/bid_size/ask_size/
trade_timestamp) -- this module never re-classifies raw ThetaData rows
itself, same "compute once, pass the derived frame around" convention as
collector.py/bt1_pilot.py.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import os
import uuid
from pathlib import Path
from typing import Callable, Optional

import pandas as pd

from .bt2_exits import ExitConfig, resolve_exit
from .bt_dedup import ADMIT
from .bt2_fills import (
    FILL_MODEL_MIDPOINT, FillConfig, friction_metrics, simulate_entry_fill, simulate_exit_fill,
)
from .bt2_schemas import (
    DIRECTION_TO_RIGHT, GATE_FAIL, GATE_PASS, SCHEMA_VERSION, as_fill_array, build_ledger_row,
)
from .bt2_selector import SelectorConfig, build_point_in_time_book, select_contract
from .schemas import contract_id as build_contract_id

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "thetadata"
BT2_DIR = DATA / "bt2_simulator"
BT2_LEDGER_PATH = BT2_DIR / "bt2_trade_ledger.json"

FLAG_MIDPOINT_NEVER_HEADLINE = "OPTIMISTIC_MIDPOINT_MODEL_NEVER_HEADLINE"
FLAG_NO_FILL = "SIGNAL_HAD_NO_FILL"
FLAG_NO_CONTRACT = "NO_CONTRACT_SELECTED"
FLAG_ADMISSION_REJECT = "ADMISSION_REJECTED"

DATA_QUALITY_OK = "OK"


def _atomic_write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".tmp{os.getpid()}")
    tmp.write_text(json.dumps(payload, indent=2, default=str))
    os.replace(tmp, path)


@dataclasses.dataclass
class TradeInputs:
    session: str                    # "YYYY-MM-DD"
    symbol: str
    signal_ts: dt.datetime
    decision_ts: dt.datetime
    direction: str                  # "CALL WATCH" | "PUT WATCH" (bt2_schemas.VALID_DIRECTION_GATES)
    context_snapshot_id: str        # e.g. f"{catalyst_brief_report_id}:{symbol}" -- BT-0 Section 3
    experiment_id: str
    quantity: int = 1
    underlying_price: Optional[float] = None
    invalidation_level: Optional[float] = None


def _planned_target_stop(ask: float, exit_config: ExitConfig) -> tuple:
    return (
        round(ask * (1 + exit_config.target_return), 4),
        round(ask * (1 + exit_config.premium_stop_pct), 4),
    )


def simulate_trade(
    inputs: TradeInputs,
    trades_df: pd.DataFrame,
    underlying_bars: pd.DataFrame,
    greeks: Optional[dict] = None,
    selector_config: SelectorConfig = SelectorConfig(),
    fill_config: FillConfig = FillConfig(),
    exit_config: ExitConfig = ExitConfig(),
    admission_check: Optional[Callable[[str, int], tuple]] = None,
) -> dict:
    """One full signal -> selection -> fill -> exit -> ledger-row pipeline.
    ALWAYS returns a ledger row (bt2_schemas.TRADE_LEDGER_FIELDS) -- a NO
    CONTRACT selection or a missed fill is recorded as a real outcome
    (contract_gate/data_quality/rule_flags), never a dropped row, per the
    roadmap's own explicit "no signal is a discarded outcome" rule."""
    right = DIRECTION_TO_RIGHT[inputs.direction]
    rule_flags: list = []
    if fill_config.fill_model == FILL_MODEL_MIDPOINT:
        rule_flags.append(FLAG_MIDPOINT_NEVER_HEADLINE)

    common = dict(
        trade_id=str(uuid.uuid4()), session=inputs.session, symbol=inputs.symbol,
        signal_ts=inputs.signal_ts.isoformat(), decision_ts=inputs.decision_ts.isoformat(),
        context_snapshot_id=inputs.context_snapshot_id, experiment_id=inputs.experiment_id,
        invalidation=inputs.invalidation_level,
    )

    book = build_point_in_time_book(
        trades_df, inputs.decision_ts, greeks,
        underlying_price=inputs.underlying_price,
    )
    selection = select_contract(book, right, inputs.decision_ts, selector_config)

    if not selection.found:
        rule_flags.append(FLAG_NO_CONTRACT)
        return build_ledger_row(
            **common, contract_id=None, contract_gate=GATE_FAIL,
            entry_quote_ts=[], entry_bid=[], entry_ask=[], entry_fill=[], quantity=[],
            target=None, stop=None,
            exit_ts=None, exit_bid=[], exit_ask=[], exit_fill=[], exit_reason="NO_CONTRACT",
            gross_pnl=None, fees=0.0, slippage=None, net_pnl=None, mae=None, mfe=None,
            rule_flags=rule_flags, data_quality=selection.reason,
        )

    candidate = selection.contract
    cid = build_contract_id(
        inputs.symbol, dt.date.fromisoformat(candidate["expiration"]), candidate["strike"], candidate["right"],
    )

    if admission_check is not None:
        admission_decision, admission_reason = admission_check(cid, inputs.quantity)
        if admission_decision != ADMIT:
            rule_flags.append(f"{FLAG_ADMISSION_REJECT}:{admission_decision}")
            row = build_ledger_row(
                **common, contract_id=cid, contract_gate=GATE_PASS,
                entry_quote_ts=[], entry_bid=[], entry_ask=[], entry_fill=[], quantity=[],
                target=None, stop=None,
                exit_ts=None, exit_bid=[], exit_ask=[], exit_fill=[], exit_reason="ADMISSION_REJECT",
                gross_pnl=None, fees=0.0, slippage=None, net_pnl=None, mae=None, mfe=None,
                rule_flags=rule_flags, data_quality=admission_decision,
            )
            row["admission_decision"] = admission_decision
            row["admission_reason"] = admission_reason
            row["friction_detail"] = None
            return row
    contract_trades = (
        trades_df[trades_df["contract_id"] == cid].sort_values("trade_timestamp")
        if trades_df is not None and not trades_df.empty else pd.DataFrame()
    )
    quotes = (
        contract_trades.rename(columns={"trade_timestamp": "quote_ts"})[["quote_ts", "bid", "ask", "bid_size", "ask_size"]]
        if not contract_trades.empty else pd.DataFrame()
    )

    entry_fill = simulate_entry_fill(quotes, inputs.decision_ts, inputs.quantity, fill_config)

    if not entry_fill.filled:
        rule_flags.append(FLAG_NO_FILL)
        planned_target, planned_stop = _planned_target_stop(candidate["ask"], exit_config)
        return build_ledger_row(
            **common, contract_id=cid, contract_gate=GATE_PASS,
            entry_quote_ts=[], entry_bid=[], entry_ask=[], entry_fill=[], quantity=[],
            target=planned_target, stop=planned_stop,
            exit_ts=None, exit_bid=[], exit_ask=[], exit_fill=[], exit_reason="NO_FILL",
            gross_pnl=None, fees=0.0, slippage=None, net_pnl=None, mae=None, mfe=None,
            rule_flags=rule_flags, data_quality=entry_fill.status,
        )

    if entry_fill.gap_flagged:
        rule_flags.append("ENTRY_FILL_GAP_FLAGGED")

    decision = resolve_exit(
        option_ticks=contract_trades, underlying_bars=underlying_bars,
        entry_ts=entry_fill.quote_ts, entry_premium=entry_fill.fill_price,
        right=candidate["right"], invalidation_level=inputs.invalidation_level,
        session_date=dt.date.fromisoformat(inputs.session), config=exit_config,
    )
    rule_flags.extend(decision.rule_flags)

    exit_fill = simulate_exit_fill(quotes, decision.exit_ts, entry_fill.quantity, fill_config)
    if exit_fill.gap_flagged:
        rule_flags.append("EXIT_FILL_GAP_FLAGGED")

    if exit_fill.filled:
        qty = entry_fill.quantity
        # NET P&L IS COMPUTED DIRECTLY FROM REAL FILLS. This is the definition;
        # everything else is decomposition for reporting.
        #
        #     net_pnl = (exit_fill - entry_fill) * qty * 100 - dollar_fees
        #
        # The previous version built net_pnl out of midpoint P&L minus a
        # PREMIUM-unit slippage term minus DOLLAR fees, which under-subtracted
        # execution cost by qty*100/2 and inflated the 162-session B1 expectancy
        # from $6.19 to $7.27/trade. See bt2_fills.friction_metrics for the full
        # bug note; friction_metrics now asserts the decomposition reconciles to
        # this number, so the two can never silently diverge again.
        planned_gross = round((decision.target_level - entry_fill.fill_price) * qty * 100, 2)
        friction = friction_metrics(entry_fill, exit_fill, planned_gross, quantity=qty)
        fees_dollars = friction["fees"]
        net_pnl = round((exit_fill.fill_price - entry_fill.fill_price) * qty * 100 - fees_dollars, 2)

        # Reporting decomposition, all in account dollars.
        gross_pnl = round((exit_fill.contemporaneous_mid - entry_fill.contemporaneous_mid) * qty * 100, 2)
        slippage = friction["total_slippage_dollars"]
        reconciled = round(gross_pnl - slippage - fees_dollars, 2)
        if abs(reconciled - net_pnl) > 0.011:  # 1c tolerance for the two roundings
            raise AssertionError(
                f"P&L decomposition failed to reconcile: direct net_pnl={net_pnl} vs "
                f"mid-minus-friction={reconciled} (gross_mid={gross_pnl} "
                f"slippage_dollars={slippage} fees={fees_dollars})")
        data_quality = DATA_QUALITY_OK
    else:
        rule_flags.append(FLAG_NO_FILL)
        gross_pnl, slippage, net_pnl = None, None, None
        friction = {"fees": entry_fill.fee}
        data_quality = exit_fill.status

    row = build_ledger_row(
        **common, contract_id=cid, contract_gate=GATE_PASS,
        entry_quote_ts=as_fill_array(entry_fill.quote_ts.isoformat() if entry_fill.quote_ts is not None else None),
        entry_bid=as_fill_array(entry_fill.bid), entry_ask=as_fill_array(entry_fill.ask),
        entry_fill=as_fill_array(entry_fill.fill_price), quantity=as_fill_array(entry_fill.quantity),
        target=decision.target_level, stop=decision.stop_level,
        exit_ts=decision.exit_ts.isoformat() if decision.exit_ts is not None else None,
        exit_bid=as_fill_array(exit_fill.bid), exit_ask=as_fill_array(exit_fill.ask),
        exit_fill=as_fill_array(exit_fill.fill_price), exit_reason=decision.exit_reason,
        gross_pnl=gross_pnl, fees=round(friction.get("fees", 0.0), 4), slippage=slippage, net_pnl=net_pnl,
        mae=decision.mae, mfe=decision.mfe,
        rule_flags=rule_flags, data_quality=data_quality,
    )
    row["friction_detail"] = friction if exit_fill.filled else None
    row["exit_quote_ts"] = exit_fill.quote_ts.isoformat() if exit_fill.quote_ts is not None else None
    return row


def append_ledger_rows(rows: list, path: Path = BT2_LEDGER_PATH) -> None:
    """Same atomic-write + append pattern as collector.py's
    _atomic_write_json / bt1_pilot's manifest writer -- temp file in the
    same directory + os.replace, so a concurrent reader never sees a
    partially-written ledger."""
    existing = []
    if path.exists():
        try:
            existing = json.loads(path.read_text()).get("trades", [])
        except Exception:
            existing = []
    existing.extend(rows)
    _atomic_write_json(path, {"schema_version": SCHEMA_VERSION, "trades": existing})
