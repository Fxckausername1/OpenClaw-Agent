"""BT-3 trailing-exit variant runner (heff's 2026-07-30 ask, companion to
bt3_b1_reclaim_exit_variant.py -- same comparison setup, different exit
mechanism, so the two are directly comparable against the same reference
point).

Tests ONE change against heff's chosen setup (baseline indicator +
moderate_combo selector): replace the fixed +25% target AND the fixed -20%
stop with a single trailing-from-peak stop at -20% (see
bt2_exits_trailing.py's own docstring for why this is a strict
generalization of the existing stop, not an unrelated new mechanism). 30-min
time stop and forced 15:30 ET close unchanged.

Compared against the SAME moderate_combo baseline reference the reclaim
variant used (493 filled, $8.07/trade, 66.1% WR, PF 4.79) -- same data, same
indicator, same selector; only the exit leg differs between this script and
the reclaim one, so all three (baseline, reclaim, trailing) are honestly
comparable side by side.

Isolation: reads backfill_60session/raw, qqq_1min_bars (read-only). Writes
only under data/thetadata/bt3_b1_trailing_exit_variant/.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import uuid
from pathlib import Path

import pandas as pd

from thetadata_pipeline.backfill import MIN_FREE_RAM_MB, _free_ram_mb
from thetadata_pipeline.bt2_exits_trailing import TrailingExitConfig, resolve_exit_trailing
from thetadata_pipeline.bt2_fills import FILL_MODEL_MIDPOINT, FillConfig, friction_metrics, simulate_entry_fill, simulate_exit_fill
from thetadata_pipeline.bt2_schemas import DIRECTION_TO_RIGHT, GATE_FAIL, GATE_PASS, as_fill_array, build_ledger_row
from thetadata_pipeline.bt2_selector import SelectorConfig, build_point_in_time_book, select_contract
from thetadata_pipeline.bt3_b0_random_control import BACKFILL_DIR, BACKFILL_RAW_DIR, _load_raw_session_trades, _load_session_greeks
from thetadata_pipeline.bt3_b1_indicator_only import SIDE_TO_DIRECTION, TRIANGLE_EVENTS_PATH, generate_b1_signals, load_triangle_events, summarize_b1
from thetadata_pipeline.schemas import contract_id as build_contract_id

logger = logging.getLogger("thetadata_pkg.bt3_b1_trailing_exit_variant")

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "data" / "thetadata" / "bt3_b1_trailing_exit_variant"
REPORT_PATH = OUT_DIR / "trailing_exit_report.json"

MODERATE_COMBO = SelectorConfig(min_abs_delta=0.10, premium_low=0.15, premium_high=0.40)
FILL_CONFIG = FillConfig()
TRAILING_EXIT_CONFIG = TrailingExitConfig()

MODERATE_COMBO_BASELINE = {
    "n_filled": 493, "avg_filled_trades_per_session": 3.043,
    "net_expectancy_per_filled_trade": 8.07,
    "net_expectancy_session_bootstrap_95ci": {"lower": 6.1281, "upper": 10.3459},
    "win_rate": 0.6613, "profit_factor": 4.7903,
    "robustness_expectancy_excluding_best_5_sessions": 6.71,
}


def simulate_trade_trailing(sig: dict, trades_df: pd.DataFrame, greeks: dict) -> dict:
    """Mirrors bt3_b1_reclaim_exit_variant.simulate_trade_reclaim for
    selection/fill (identical, same tested functions) -- only the exit
    leg differs: resolve_exit_trailing, no reclaim_bars needed at all."""
    direction = SIDE_TO_DIRECTION[sig["side"]] if "side" in sig else sig["direction"]
    right = DIRECTION_TO_RIGHT[direction]
    rule_flags: list = []
    if FILL_CONFIG.fill_model == FILL_MODEL_MIDPOINT:
        rule_flags.append("OPTIMISTIC_MIDPOINT_MODEL_NEVER_HEADLINE")

    session = sig["session"]
    decision_ts = sig["decision_ts"]
    common = dict(
        trade_id=str(uuid.uuid4()), session=session, symbol=sig["symbol"],
        signal_ts=decision_ts.isoformat(), decision_ts=decision_ts.isoformat(),
        context_snapshot_id=f"B1-TRAILING:{sig['symbol']}:{session}:{sig['bar_index']}:{sig['trigger']}",
        experiment_id="bt3_b1_trailing_exit_variant", invalidation=None,
    )

    book = build_point_in_time_book(trades_df, decision_ts, greeks)
    selection = select_contract(book, right, decision_ts, MODERATE_COMBO)

    if not selection.found:
        rule_flags.append("NO_CONTRACT_SELECTED")
        row = build_ledger_row(
            **common, contract_id=None, contract_gate=GATE_FAIL,
            entry_quote_ts=[], entry_bid=[], entry_ask=[], entry_fill=[], quantity=[],
            target=None, stop=None,
            exit_ts=None, exit_bid=[], exit_ask=[], exit_fill=[], exit_reason="NO_CONTRACT",
            gross_pnl=None, fees=0.0, slippage=None, net_pnl=None, mae=None, mfe=None,
            rule_flags=rule_flags, data_quality=selection.reason,
        )
        row["heff_smc_trigger"] = sig["trigger"]
        return row

    candidate = selection.contract
    cid = build_contract_id(sig["symbol"], dt.date.fromisoformat(candidate["expiration"]), candidate["strike"], candidate["right"])
    contract_trades = (
        trades_df[trades_df["contract_id"] == cid].sort_values("trade_timestamp")
        if trades_df is not None and not trades_df.empty else pd.DataFrame()
    )
    quotes = (
        contract_trades.rename(columns={"trade_timestamp": "quote_ts"})[["quote_ts", "bid", "ask", "bid_size", "ask_size"]]
        if not contract_trades.empty else pd.DataFrame()
    )

    entry_fill = simulate_entry_fill(quotes, decision_ts, 1, FILL_CONFIG)

    if not entry_fill.filled:
        rule_flags.append("SIGNAL_HAD_NO_FILL")
        planned_stop = round(candidate["ask"] * (1 + TRAILING_EXIT_CONFIG.trail_pct), 4)
        row = build_ledger_row(
            **common, contract_id=cid, contract_gate=GATE_PASS,
            entry_quote_ts=[], entry_bid=[], entry_ask=[], entry_fill=[], quantity=[],
            target=None, stop=planned_stop,
            exit_ts=None, exit_bid=[], exit_ask=[], exit_fill=[], exit_reason="NO_FILL",
            gross_pnl=None, fees=0.0, slippage=None, net_pnl=None, mae=None, mfe=None,
            rule_flags=rule_flags, data_quality=entry_fill.status,
        )
        row["heff_smc_trigger"] = sig["trigger"]
        return row

    if entry_fill.gap_flagged:
        rule_flags.append("ENTRY_FILL_GAP_FLAGGED")

    decision = resolve_exit_trailing(
        option_ticks=contract_trades,
        entry_ts=entry_fill.quote_ts, entry_premium=entry_fill.fill_price,
        session_date=dt.date.fromisoformat(session), config=TRAILING_EXIT_CONFIG,
    )
    rule_flags.extend(decision.rule_flags)

    exit_fill = simulate_exit_fill(quotes, decision.exit_ts, entry_fill.quantity, FILL_CONFIG)
    if exit_fill.gap_flagged:
        rule_flags.append("EXIT_FILL_GAP_FLAGGED")

    if exit_fill.filled:
        # Same unit fix as bt2_simulator.simulate_trade (2026-07-31): net P&L is
        # computed DIRECTLY from real fills. The previous line subtracted
        # PREMIUM-unit slippage from a DOLLAR-denominated midpoint P&L, which
        # under-stated execution cost by qty*100/2. This variant's own headline
        # numbers were affected by it too, so any trailing-exit result produced
        # before this date is inflated on the same basis as the baseline was.
        qty = entry_fill.quantity
        friction = friction_metrics(entry_fill, exit_fill, None, quantity=qty)
        net_pnl = round((exit_fill.fill_price - entry_fill.fill_price) * qty * 100
                        - friction["fees"], 2)
        gross_pnl = round((exit_fill.contemporaneous_mid - entry_fill.contemporaneous_mid) * qty * 100, 2)
        slippage = friction["total_slippage_dollars"]
        data_quality = "OK"
    else:
        rule_flags.append("SIGNAL_HAD_NO_FILL")
        gross_pnl, slippage, net_pnl = None, None, None
        friction = {"fees": entry_fill.fee}
        data_quality = exit_fill.status

    row = build_ledger_row(
        **common, contract_id=cid, contract_gate=GATE_PASS,
        entry_quote_ts=as_fill_array(entry_fill.quote_ts.isoformat() if entry_fill.quote_ts is not None else None),
        entry_bid=as_fill_array(entry_fill.bid), entry_ask=as_fill_array(entry_fill.ask),
        entry_fill=as_fill_array(entry_fill.fill_price), quantity=as_fill_array(entry_fill.quantity),
        target=None, stop=decision.stop_level,
        exit_ts=decision.exit_ts.isoformat() if decision.exit_ts is not None else None,
        exit_bid=as_fill_array(exit_fill.bid), exit_ask=as_fill_array(exit_fill.ask),
        exit_fill=as_fill_array(exit_fill.fill_price), exit_reason=decision.exit_reason,
        gross_pnl=gross_pnl, fees=round(friction.get("fees", 0.0), 4), slippage=slippage, net_pnl=net_pnl,
        mae=decision.mae, mfe=decision.mfe,
        rule_flags=rule_flags, data_quality=data_quality,
    )
    row["friction_detail"] = friction if exit_fill.filled else None
    row["heff_smc_trigger"] = sig["trigger"]
    return row


def main():
    logging.basicConfig(level=logging.INFO)
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    events = load_triangle_events(TRIANGLE_EVENTS_PATH)
    signals = generate_b1_signals(events)
    logger.info("trailing-exit variant: %d signals across %d sessions", len(signals), len({s['session'] for s in signals}))

    signals_by_session: dict = {}
    for sig in signals:
        signals_by_session.setdefault(sig["session"], []).append(sig)

    rows = []
    for session in sorted(signals_by_session):
        free_mb = _free_ram_mb()
        if free_mb < MIN_FREE_RAM_MB:
            logger.warning("trailing-exit variant aborted early at session %s: %.0fMB free RAM", session, free_mb)
            break
        session_signals = signals_by_session[session]
        symbol = session_signals[0]["symbol"]
        trades_df = _load_raw_session_trades(symbol, session, BACKFILL_RAW_DIR)
        greeks = _load_session_greeks(symbol, session, trades_df, BACKFILL_DIR)
        for sig in session_signals:
            rows.append(simulate_trade_trailing(sig, trades_df, greeks))
        del trades_df

    summary = summarize_b1(rows)
    exit_reason_counts = {}
    for r in rows:
        er = r.get("exit_reason")
        exit_reason_counts[er] = exit_reason_counts.get(er, 0) + 1

    payload = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "label": "B1 TRAILING-EXIT VARIANT -- EXPLORATORY, NOT PROMOTION-EVALUATED",
        "change": "premium +25% fixed target AND -20% fixed stop-from-entry BOTH REPLACED "
                  "with: a single trailing stop at -20% off the trade's own PEAK bid since "
                  "entry (identical to the old fixed stop when a trade never goes green; "
                  "adapts upward to protect more once it does). 30-min no-progress time "
                  "stop and forced 15:30 ET close UNCHANGED from live defaults.",
        "selector_config": "moderate_combo (heff's 2026-07-30 choice)",
        "indicator_config": "unmodified live defaults",
        "trail_pct": TRAILING_EXIT_CONFIG.trail_pct,
        "exit_reason_counts": exit_reason_counts,
        "moderate_combo_baseline_reference": MODERATE_COMBO_BASELINE,
        "summary": summary,
    }
    tmp = REPORT_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2, default=str))
    tmp.replace(REPORT_PATH)
    logger.info("trailing-exit variant complete: n_filled=%s expectancy=%s exit_reasons=%s",
                summary.get("n_filled"), summary.get("net_expectancy_per_filled_trade"), exit_reason_counts)


if __name__ == "__main__":
    main()
