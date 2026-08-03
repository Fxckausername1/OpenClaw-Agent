"""BT-3 trailing-exit HOLDOUT validation (heff's 2026-07-30 ask).

Trailing-exit was picked by comparing three exit variants (reclaim/trailing/
no-target) against each other on the SAME full 162-session dataset -- the
same "compare several, take the winner" pattern that creates false-
discovery risk, which is exactly what the indicator holdout sweep spent all
night protecting against with a real dev/holdout split. This applies that
same discipline to the trailing-exit choice itself before treating it as
truly validated.

Comparison isolates the ONE thing actually being decided: given moderate_combo
is already heff's locked-in selector, does trailing-exit beat the original
fixed target/stop exit, with the selector held constant? NOT re-litigating
selector or indicator choices -- those are separately decided already.

Same chronological split as the indicator sweep (HOLDOUT_N=40, most recent
sessions held out, older sessions used to check the improvement first) --
reuses bt3_b1_param_sweep.compare_to_baseline's own pre-registered bar
unmodified (a config only counts as validated if its CI lower bound clears
baseline's own point estimate, computed fresh on the SAME session subset,
not just a higher number).

Isolation: reads backfill_60session/raw, qqq_1min_bars (read-only). Writes
only under data/thetadata/bt3_b1_trailing_exit_holdout/.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
from pathlib import Path

import pandas as pd

from thetadata_pipeline.backfill import MIN_FREE_RAM_MB, _free_ram_mb
from thetadata_pipeline.bt2_exits import ExitConfig
from thetadata_pipeline.bt2_fills import FillConfig
from thetadata_pipeline.bt2_selector import SelectorConfig
from thetadata_pipeline.bt2_simulator import TradeInputs, simulate_trade
from thetadata_pipeline.bt3_b0_random_control import BACKFILL_DIR, BACKFILL_RAW_DIR, _load_raw_session_trades, _load_session_greeks
from thetadata_pipeline.bt3_b1_indicator_only import LABEL_B1, SIDE_TO_DIRECTION, TRIANGLE_EVENTS_PATH, generate_b1_signals, load_triangle_events, summarize_b1
from thetadata_pipeline.bt3_b1_param_sweep import compare_to_baseline
from thetadata_pipeline.bt3_b1_trailing_exit_variant import simulate_trade_trailing
from thetadata_pipeline.qqq_bars_fetch import list_target_sessions

logger = logging.getLogger("thetadata_pkg.bt3_b1_trailing_exit_holdout")

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "data" / "thetadata" / "bt3_b1_trailing_exit_holdout"
MARKER_PATH = OUT_DIR / "status.json"

HOLDOUT_N = 40  # same split as the indicator sweep
MODERATE_COMBO = SelectorConfig(min_abs_delta=0.10, premium_low=0.15, premium_high=0.40)
FILL_CONFIG = FillConfig()
BASELINE_EXIT_CONFIG = ExitConfig()  # unmodified live defaults: +25% target, -20% stop, 30min, 15:30 close
BASELINE_ID = "moderate_combo_default_exit"
TRAILING_ID = "moderate_combo_trailing_exit"


def write_marker(status: str, **kw) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    payload = {"status": status, "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(), **kw}
    tmp = MARKER_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2, default=str))
    tmp.replace(MARKER_PATH)
    logger.info("status -> %s %s", status, kw)


def simulate_trade_baseline_exit(sig: dict, trades_df, greeks: dict) -> dict:
    """moderate_combo selector + the ORIGINAL unmodified exit config
    (target/stop/time-stop/forced-close) -- the actual comparison point,
    not the plain-default-selector B1 baseline (that's a separately
    answered question)."""
    direction = SIDE_TO_DIRECTION[sig["side"]] if "side" in sig else sig["direction"]
    context_snapshot_id = f"B1-HOLDOUT-BASELINE:{sig['symbol']}:{sig['session']}:{sig['bar_index']}:{sig['trigger']}"
    inputs = TradeInputs(
        session=sig["session"], symbol=sig["symbol"],
        signal_ts=sig["decision_ts"], decision_ts=sig["decision_ts"],
        direction=direction, context_snapshot_id=context_snapshot_id,
        experiment_id="bt3_b1_trailing_exit_holdout", quantity=1, invalidation_level=None,
    )
    row = simulate_trade(
        inputs, trades_df, pd.DataFrame(), greeks=greeks,
        selector_config=MODERATE_COMBO, fill_config=FILL_CONFIG, exit_config=BASELINE_EXIT_CONFIG,
    )
    row["rule_flags"] = list(row["rule_flags"]) + [LABEL_B1]
    row["heff_smc_trigger"] = sig["trigger"]
    return row


def _simulate_bucket(signals: list, allowed_sessions: set) -> tuple:
    """Runs BOTH configs per session, loading raw data once per session
    (same load-once-reuse discipline as every other sweep tonight).
    Returns (baseline_rows, trailing_rows)."""
    by_session: dict = {}
    for sig in signals:
        if sig["session"] in allowed_sessions:
            by_session.setdefault(sig["session"], []).append(sig)

    baseline_rows, trailing_rows = [], []
    for session in sorted(by_session):
        free_mb = _free_ram_mb()
        if free_mb < MIN_FREE_RAM_MB:
            logger.warning("bucket sim aborted early at session %s: %.0fMB free RAM", session, free_mb)
            break
        session_signals = by_session[session]
        symbol = session_signals[0]["symbol"]
        trades_df = _load_raw_session_trades(symbol, session, BACKFILL_RAW_DIR)
        greeks = _load_session_greeks(symbol, session, trades_df, BACKFILL_DIR)
        for sig in session_signals:
            baseline_rows.append(simulate_trade_baseline_exit(sig, trades_df, greeks))
            trailing_rows.append(simulate_trade_trailing(sig, trades_df, greeks))
        del trades_df
    return baseline_rows, trailing_rows


def _build_results(baseline_rows: list, trailing_rows: list) -> dict:
    return {
        BASELINE_ID: {"description": "moderate_combo selector, original fixed target/stop exit",
                      "overrides": {}, "summary": summarize_b1(baseline_rows)},
        TRAILING_ID: {"description": "moderate_combo selector, trailing-from-peak exit (-20%)",
                      "overrides": {}, "summary": summarize_b1(trailing_rows)},
    }


def main():
    logging.basicConfig(level=logging.INFO)
    write_marker("phase_a_dev_starting")

    all_dates = sorted(list_target_sessions())
    dev_dates = set(all_dates[:-HOLDOUT_N])
    holdout_dates = set(all_dates[-HOLDOUT_N:])
    logger.info("split: %d dev sessions (%s..%s), %d holdout sessions (%s..%s)",
                len(dev_dates), min(dev_dates), max(dev_dates),
                len(holdout_dates), min(holdout_dates), max(holdout_dates))

    events = load_triangle_events(TRIANGLE_EVENTS_PATH)
    signals = generate_b1_signals(events)

    logger.info("=== PHASE A: dev-set check (%d sessions) ===", len(dev_dates))
    dev_baseline_rows, dev_trailing_rows = _simulate_bucket(signals, dev_dates)
    dev_results = _build_results(dev_baseline_rows, dev_trailing_rows)
    dev_comparisons = compare_to_baseline(dev_results, baseline_id=BASELINE_ID)
    _write = lambda p, payload: (p.parent.mkdir(parents=True, exist_ok=True), p.write_text(json.dumps(payload, indent=2, default=str)))
    _write(OUT_DIR / "phase_a_dev_results.json", {"dev_sessions": sorted(dev_dates), "results": dev_results, "comparisons": dev_comparisons})

    verdict = dev_comparisons[TRAILING_ID]["verdict"]
    logger.info("Phase A verdict for trailing-exit on dev set: %s", verdict)
    write_marker("phase_a_complete", verdict=verdict,
                 dev_expectancy_baseline=dev_results[BASELINE_ID]["summary"].get("net_expectancy_per_filled_trade"),
                 dev_expectancy_trailing=dev_results[TRAILING_ID]["summary"].get("net_expectancy_per_filled_trade"))

    if not verdict.startswith("plausibly better"):
        write_marker("stopped_not_validated_on_dev", verdict=verdict,
                     reason="trailing-exit's dev-set CI lower bound did not clear baseline's dev-set point estimate")
        logger.info("Trailing-exit did not clear the bar on dev set. Stopping -- no holdout check needed.")
        return

    logger.info("=== PHASE B: holdout validation (%d sessions) ===", len(holdout_dates))
    ho_baseline_rows, ho_trailing_rows = _simulate_bucket(signals, holdout_dates)
    ho_results = _build_results(ho_baseline_rows, ho_trailing_rows)
    ho_comparisons = compare_to_baseline(ho_results, baseline_id=BASELINE_ID)
    _write(OUT_DIR / "phase_b_holdout_results.json", {"holdout_sessions": sorted(holdout_dates), "results": ho_results, "comparisons": ho_comparisons})

    ho_verdict = ho_comparisons[TRAILING_ID]["verdict"]
    validated = ho_verdict.startswith("plausibly better")
    logger.info("Phase B verdict for trailing-exit on HOLDOUT set: %s", ho_verdict)
    write_marker("all_done", dev_verdict=verdict, holdout_verdict=ho_verdict, validated=validated,
                 holdout_expectancy_baseline=ho_results[BASELINE_ID]["summary"].get("net_expectancy_per_filled_trade"),
                 holdout_expectancy_trailing=ho_results[TRAILING_ID]["summary"].get("net_expectancy_per_filled_trade"))
    logger.info("Trailing-exit holdout validation complete. Validated on untouched holdout data: %s", validated)


if __name__ == "__main__":
    main()
