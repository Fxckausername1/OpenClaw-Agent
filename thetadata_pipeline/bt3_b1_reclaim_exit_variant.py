"""BT-3 reclaim-exit variant runner (heff's 2026-07-30 ask).

Tests ONE change against heff's already-chosen setup (baseline indicator +
moderate_combo selector, the config from the 2026-07-30 selector sweep he
picked to move forward with): replace the fixed +25% premium target with
"a new same-direction sweep-and-reclaim event fires after entry." Stop
(-20%), 30-min no-progress time stop, and forced 15:30 ET close are all
UNCHANGED -- heff's own words, "replace, not layered on top... just
replace 25% with reclaim icon."

Deliberately compared against moderate_combo's ALREADY-KNOWN numbers (from
bt3_b1_selector_sweep's real 2026-07-30 run: 493 filled, 3.04/day, $8.07/
trade, 95% CI $6.13-$10.35, 66.1% win rate, PF 4.79) rather than re-running
that baseline here -- same data, same indicator, same selector, the ONLY
thing this script changes is the exit leg, so the existing number is a
valid, uncontaminated comparison point.

Indicator itself is NOT touched (HeffSmcConfig() defaults, same as every
B1 run) -- this is a pure exit-mechanics experiment, independent of
whatever the indicator holdout sweep (running concurrently... well,
sequentially, box is single-core) finds. If that sweep produces a
validated champion indicator later, this same reclaim-exit logic can be
re-tested against it -- separate, later step, not conflated here.

Isolation: reads backfill_60session/raw, qqq_1min_bars (read-only). Writes
only under data/thetadata/bt3_b1_reclaim_exit_variant/.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import uuid
from pathlib import Path
from typing import Optional

import pandas as pd

from thetadata_pipeline.backfill import MIN_FREE_RAM_MB, _free_ram_mb
from thetadata_pipeline.bt2_exits_reclaim import ReclaimExitConfig, resolve_exit_reclaim
from thetadata_pipeline.bt2_fills import FILL_MODEL_MIDPOINT, FillConfig, friction_metrics, simulate_entry_fill, simulate_exit_fill
from thetadata_pipeline.bt2_schemas import DIRECTION_TO_RIGHT, GATE_FAIL, GATE_PASS, as_fill_array, build_ledger_row
from thetadata_pipeline.bt2_selector import SelectorConfig, build_point_in_time_book, select_contract
from thetadata_pipeline.bt3_b0_random_control import BACKFILL_DIR, BACKFILL_RAW_DIR, _load_raw_session_trades, _load_session_greeks
from thetadata_pipeline.bt3_b1_indicator_only import EXPERIMENT_ID, LABEL_B1, SIDE_TO_DIRECTION, TRIANGLE_EVENTS_PATH, generate_b1_signals, load_triangle_events, session_bootstrap_mean_ci, summarize_b1
from thetadata_pipeline.heff_smc_engine import HeffSmcConfig
from thetadata_pipeline.heff_smc_replay import build_continuous_1min_series, run_replay
from thetadata_pipeline.qqq_bars_fetch import BARS_RAW_DIR, SYMBOL, list_target_sessions, load_all_bars
from thetadata_pipeline.schemas import contract_id as build_contract_id

logger = logging.getLogger("thetadata_pkg.bt3_b1_reclaim_exit_variant")

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "data" / "thetadata" / "bt3_b1_reclaim_exit_variant"
REPORT_PATH = OUT_DIR / "reclaim_exit_report.json"

# heff's chosen selector from the 2026-07-30 selector sweep -- reused verbatim
MODERATE_COMBO = SelectorConfig(min_abs_delta=0.10, premium_low=0.15, premium_high=0.40)
FILL_CONFIG = FillConfig()
RECLAIM_EXIT_CONFIG = ReclaimExitConfig()

# real, already-measured moderate_combo baseline (default exit, 162 sessions) --
# see data/thetadata/bt3_b1_selector_sweep/moderate_combo_report.json
MODERATE_COMBO_BASELINE = {
    "n_filled": 493, "avg_filled_trades_per_session": 3.043,
    "net_expectancy_per_filled_trade": 8.07,
    "net_expectancy_session_bootstrap_95ci": {"lower": 6.1281, "upper": 10.3459},
    "win_rate": 0.6613, "profit_factor": 4.7903,
    "robustness_expectancy_excluding_best_5_sessions": 6.71,
}


def simulate_trade_reclaim(sig: dict, trades_df: pd.DataFrame, greeks: dict, reclaim_bars: pd.DataFrame) -> dict:
    """Mirrors bt2_simulator.simulate_trade exactly for selection/fill
    (same tested functions, same SelectorConfig/FillConfig) -- only the
    exit leg is different: resolve_exit_reclaim instead of resolve_exit,
    and no target-based friction-share denominator since there's no
    target concept in this variant (planned_gross=None, which
    friction_metrics already handles as "not applicable", not a poisoned
    NaN)."""
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
        context_snapshot_id=f"B1-RECLAIM:{sig['symbol']}:{session}:{sig['bar_index']}:{sig['trigger']}",
        experiment_id="bt3_b1_reclaim_exit_variant", invalidation=None,
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
        planned_stop = round(candidate["ask"] * (1 + RECLAIM_EXIT_CONFIG.premium_stop_pct), 4)
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

    decision = resolve_exit_reclaim(
        option_ticks=contract_trades, reclaim_bars=reclaim_bars,
        entry_ts=entry_fill.quote_ts, entry_premium=entry_fill.fill_price,
        right=candidate["right"], session_date=dt.date.fromisoformat(session),
        config=RECLAIM_EXIT_CONFIG,
    )
    rule_flags.extend(decision.rule_flags)

    exit_fill = simulate_exit_fill(quotes, decision.exit_ts, entry_fill.quantity, FILL_CONFIG)
    if exit_fill.gap_flagged:
        rule_flags.append("EXIT_FILL_GAP_FLAGGED")

    if exit_fill.filled:
        # Same unit fix as bt2_simulator.simulate_trade (2026-07-31): net P&L is
        # computed DIRECTLY from real fills. The previous line subtracted
        # PREMIUM-unit slippage from a DOLLAR-denominated midpoint P&L, which
        # under-stated execution cost by qty*100/2. The reclaim-exit headline
        # figures were produced under that bug and are inflated on the same basis.
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

    all_dates = sorted(list_target_sessions())
    raw_bars = load_all_bars(SYMBOL, all_dates, BARS_RAW_DIR)
    continuous = build_continuous_1min_series(raw_bars)

    logger.info("running baseline (unmodified) indicator replay for reclaim data + signals")
    events, diag_df = run_replay(continuous, HeffSmcConfig())
    signals = generate_b1_signals(events)
    logger.info("reclaim-exit variant: %d signals across %d sessions", len(signals), len({s['session'] for s in signals}))

    # diag_df has a 't' timestamp column but no 'session' column of its own
    # (heff_smc_replay.py only attaches "session" to triangle_events, not
    # to every diagnostic row) -- derive it the identical way the replay
    # itself does: floor 't' to date, str() it, so this matches every
    # signal's own "session" field exactly, no drift possible.
    if "t" not in diag_df.columns:
        raise RuntimeError("diagnostic frame has no 't' column -- cannot derive session, aborting rather than guessing")
    diag_df = diag_df.copy()
    diag_df["session"] = diag_df["t"].apply(lambda ts: str(pd.Timestamp(ts).date()))
    diag_by_session = {sess: df for sess, df in diag_df.groupby("session")}

    signals_by_session: dict = {}
    for sig in signals:
        signals_by_session.setdefault(sig["session"], []).append(sig)

    rows = []
    for session in sorted(signals_by_session):
        free_mb = _free_ram_mb()
        if free_mb < MIN_FREE_RAM_MB:
            logger.warning("reclaim-exit variant aborted early at session %s: %.0fMB free RAM", session, free_mb)
            break
        session_signals = signals_by_session[session]
        symbol = session_signals[0]["symbol"]
        trades_df = _load_raw_session_trades(symbol, session, BACKFILL_RAW_DIR)
        greeks = _load_session_greeks(symbol, session, trades_df, BACKFILL_DIR)
        reclaim_bars = diag_by_session.get(session, pd.DataFrame())
        for sig in session_signals:
            rows.append(simulate_trade_reclaim(sig, trades_df, greeks, reclaim_bars))
        del trades_df

    summary = summarize_b1(rows)
    exit_reason_counts = {}
    for r in rows:
        er = r.get("exit_reason")
        exit_reason_counts[er] = exit_reason_counts.get(er, 0) + 1

    payload = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "label": "B1 RECLAIM-EXIT VARIANT -- EXPLORATORY, NOT PROMOTION-EVALUATED",
        "change": "premium +25% fixed target REPLACED with: first new same-direction "
                  "sweep-and-reclaim event after entry. Stop (-20%), 30-min no-progress "
                  "time stop, forced 15:30 ET close all UNCHANGED from live defaults.",
        "selector_config": "moderate_combo (heff's 2026-07-30 choice)",
        "indicator_config": "unmodified live defaults",
        "exit_reason_counts": exit_reason_counts,
        "moderate_combo_baseline_reference": MODERATE_COMBO_BASELINE,
        "summary": summary,
    }
    tmp = REPORT_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2, default=str))
    tmp.replace(REPORT_PATH)
    logger.info("reclaim-exit variant complete: n_filled=%s expectancy=%s exit_reasons=%s",
                summary.get("n_filled"), summary.get("net_expectancy_per_filled_trade"), exit_reason_counts)


if __name__ == "__main__":
    main()
