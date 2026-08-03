"""BT-3 no-target isolation test (heff's 2026-07-30 ask).

The reclaim-exit variant produced a huge expectancy number ($23.95/trade vs
$8.07 baseline), but RECLAIM only fired on 19 of 492 filled trades (3.9%) --
so that result mostly measured "what happens when the +25% target is gone
and trades ride to stop/time-stop/forced-close," NOT "reclaim is a good
exit signal." This test isolates that single variable cleanly: same
moderate_combo selector, same baseline indicator, ONLY the target removed
-- fixed -20% stop-from-entry, 30-min time stop, forced 15:30 ET close all
UNCHANGED, exactly bt2_exits.ExitConfig's own defaults otherwise.

Deliberately the simplest of the three exit experiments tonight: no new
exit-resolution code needed at all. bt2_exits.resolve_exit already has a
target leg that can be effectively disabled by passing a target_return so
large it can never realistically be hit (999.0 = premium would need to
increase 99,900%) -- reuses bt2_simulator.simulate_trade() and
bt2_exits.ExitConfig completely unmodified, just one parameter changed.

Compared against the same moderate_combo baseline reference (493 filled,
$8.07/trade, 66.1% WR, PF 4.79) the reclaim/trailing variants used, and
directly against reclaim-exit's own numbers -- this is the piece that
splits "no target" from "reclaim actually firing" as two separate effects.

Isolation: reads backfill_60session/raw, qqq_1min_bars (read-only). Writes
only under data/thetadata/bt3_b1_no_target_isolation/.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
from pathlib import Path

from thetadata_pipeline.backfill import MIN_FREE_RAM_MB, _free_ram_mb
from thetadata_pipeline.bt2_exits import ExitConfig
from thetadata_pipeline.bt2_fills import FillConfig
from thetadata_pipeline.bt2_selector import SelectorConfig
from thetadata_pipeline.bt2_simulator import TradeInputs, simulate_trade
from thetadata_pipeline.bt3_b0_random_control import BACKFILL_DIR, BACKFILL_RAW_DIR, _load_raw_session_trades, _load_session_greeks
from thetadata_pipeline.bt3_b1_indicator_only import EXPERIMENT_ID, LABEL_B1, SIDE_TO_DIRECTION, TRIANGLE_EVENTS_PATH, generate_b1_signals, load_triangle_events, summarize_b1
import pandas as pd

logger = logging.getLogger("thetadata_pkg.bt3_b1_no_target_isolation")

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "data" / "thetadata" / "bt3_b1_no_target_isolation"
REPORT_PATH = OUT_DIR / "no_target_isolation_report.json"

MODERATE_COMBO = SelectorConfig(min_abs_delta=0.10, premium_low=0.15, premium_high=0.40)
FILL_CONFIG = FillConfig()
# target_return=999.0 -> target_level = entry * 1000, never realistically reachable.
# Effectively disables the target leg with zero new logic -- stop (-20%), time stop
# (30min no-progress), and forced close (15:30 ET) all stay at their real defaults.
NO_TARGET_EXIT_CONFIG = ExitConfig(target_return=999.0)

MODERATE_COMBO_BASELINE = {
    "n_filled": 493, "avg_filled_trades_per_session": 3.043,
    "net_expectancy_per_filled_trade": 8.07,
    "net_expectancy_session_bootstrap_95ci": {"lower": 6.1281, "upper": 10.3459},
    "win_rate": 0.6613, "profit_factor": 4.7903,
    "robustness_expectancy_excluding_best_5_sessions": 6.71,
}
RECLAIM_EXIT_REFERENCE = {
    "n_filled": 492, "net_expectancy_per_filled_trade": 23.95,
    "win_rate": 0.3699, "profit_factor": 7.8914,
    "exit_reason_counts": {"NO_CONTRACT": 1286, "STOP": 234, "FORCED_CLOSE": 191, "TIME_STOP": 49, "RECLAIM": 19},
}


def simulate_b1_signal_no_target(sig: dict, trades_df, greeks: dict) -> dict:
    direction = SIDE_TO_DIRECTION[sig["side"]] if "side" in sig else sig["direction"]
    context_snapshot_id = f"B1-NOTARGET:{sig['symbol']}:{sig['session']}:{sig['bar_index']}:{sig['trigger']}"
    inputs = TradeInputs(
        session=sig["session"], symbol=sig["symbol"],
        signal_ts=sig["decision_ts"], decision_ts=sig["decision_ts"],
        direction=direction, context_snapshot_id=context_snapshot_id,
        experiment_id="bt3_b1_no_target_isolation", quantity=1, invalidation_level=None,
    )
    row = simulate_trade(
        inputs, trades_df, pd.DataFrame(), greeks=greeks,
        selector_config=MODERATE_COMBO, fill_config=FILL_CONFIG, exit_config=NO_TARGET_EXIT_CONFIG,
    )
    row["rule_flags"] = list(row["rule_flags"]) + [LABEL_B1, "NO_TARGET_ISOLATION"]
    row["heff_smc_trigger"] = sig["trigger"]
    return row


def main():
    logging.basicConfig(level=logging.INFO)
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    events = load_triangle_events(TRIANGLE_EVENTS_PATH)
    signals = generate_b1_signals(events)
    logger.info("no-target isolation: %d signals across %d sessions", len(signals), len({s['session'] for s in signals}))

    signals_by_session: dict = {}
    for sig in signals:
        signals_by_session.setdefault(sig["session"], []).append(sig)

    rows = []
    for session in sorted(signals_by_session):
        free_mb = _free_ram_mb()
        if free_mb < MIN_FREE_RAM_MB:
            logger.warning("no-target isolation aborted early at session %s: %.0fMB free RAM", session, free_mb)
            break
        session_signals = signals_by_session[session]
        symbol = session_signals[0]["symbol"]
        trades_df = _load_raw_session_trades(symbol, session, BACKFILL_RAW_DIR)
        greeks = _load_session_greeks(symbol, session, trades_df, BACKFILL_DIR)
        for sig in session_signals:
            rows.append(simulate_b1_signal_no_target(sig, trades_df, greeks))
        del trades_df

    summary = summarize_b1(rows)
    exit_reason_counts = {}
    for r in rows:
        er = r.get("exit_reason")
        exit_reason_counts[er] = exit_reason_counts.get(er, 0) + 1

    payload = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "label": "B1 NO-TARGET ISOLATION -- EXPLORATORY, NOT PROMOTION-EVALUATED",
        "change": "premium +25% fixed target REMOVED (target_return=999.0, never reachable). "
                  "-20% stop-from-entry, 30-min no-progress time stop, forced 15:30 ET close "
                  "ALL UNCHANGED from live defaults -- isolates 'no cap' from 'reclaim firing' "
                  "as two separate effects.",
        "selector_config": "moderate_combo (heff's 2026-07-30 choice)",
        "indicator_config": "unmodified live defaults",
        "exit_reason_counts": exit_reason_counts,
        "moderate_combo_baseline_reference": MODERATE_COMBO_BASELINE,
        "reclaim_exit_reference": RECLAIM_EXIT_REFERENCE,
        "summary": summary,
    }
    tmp = REPORT_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2, default=str))
    tmp.replace(REPORT_PATH)
    logger.info("no-target isolation complete: n_filled=%s expectancy=%s exit_reasons=%s",
                summary.get("n_filled"), summary.get("net_expectancy_per_filled_trade"), exit_reason_counts)


if __name__ == "__main__":
    main()
