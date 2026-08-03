"""BT-3 B1 selector sweep (heff's 2026-07-29 evening ask): the triangles
already validated on 162 sessions (see bt3_b1_indicator_only_report.json).
heff wants to know if loosening the CONTRACT SELECTOR (not the indicator
itself) toward >=2 filled trades/day is viable, and which lever actually
matters. bt2_selector.py's own SelectorConfig docstring already earmarks
these exact thresholds for a parameter sweep (roadmap Section 10 / BT-3) --
this is that sweep, not off-script tuning.

Deliberately isolates each lever (one loosened at a time) before any
combined variant, so the result says WHICH constraint is binding instead
of just reporting a single cherry-picked "better" number. Reuses the real,
tested simulate_trade() / summarize_b1() machinery unmodified -- only the
SelectorConfig instance passed in changes per variant. Writes to its own
namespace (data/thetadata/bt3_b1_selector_sweep/) -- never touches the
canonical bt3_b1_indicator_only ledger/report from the 162-session run.
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
from thetadata_pipeline.bt3_b0_random_control import (
    BACKFILL_DIR, BACKFILL_RAW_DIR, _load_raw_session_trades, _load_session_greeks,
)
from thetadata_pipeline.bt3_b1_indicator_only import (
    CHARTER_WINDOW_START, CHARTER_WINDOW_END, EXPERIMENT_ID, LABEL_B1,
    SIDE_TO_DIRECTION, TRIANGLE_EVENTS_PATH, generate_b1_signals,
    load_triangle_events, summarize_b1,
)

logger = logging.getLogger("thetadata_pkg.bt3_b1_selector_sweep")

ROOT = Path(__file__).resolve().parent.parent
SWEEP_DIR = ROOT / "data" / "thetadata" / "bt3_b1_selector_sweep"

FILL_CONFIG = FillConfig()
EXIT_CONFIG = ExitConfig()

VARIANTS = {
    "wider_delta": SelectorConfig(min_abs_delta=0.08),
    "wider_premium": SelectorConfig(premium_low=0.10, premium_high=0.50),
    "wider_quality": SelectorConfig(
        max_spread_dollars=0.15, max_spread_pct_mid=0.30,
        max_quote_age_seconds=30.0, min_ask_size=2.0,
    ),
    "moderate_combo": SelectorConfig(
        min_abs_delta=0.10, premium_low=0.15, premium_high=0.40,
    ),
}


def simulate_variant_signal(sig: dict, trades_df, greeks: dict, selector_config: SelectorConfig) -> dict:
    context_snapshot_id = f"B1-SWEEP:{sig['symbol']}:{sig['session']}:{sig['bar_index']}:{sig['trigger']}"
    inputs = TradeInputs(
        session=sig["session"], symbol=sig["symbol"],
        signal_ts=sig["decision_ts"], decision_ts=sig["decision_ts"],
        direction=sig["direction"], context_snapshot_id=context_snapshot_id,
        experiment_id=EXPERIMENT_ID, quantity=1, invalidation_level=None,
    )
    import pandas as pd
    row = simulate_trade(
        inputs, trades_df, pd.DataFrame(), greeks=greeks,
        selector_config=selector_config, fill_config=FILL_CONFIG, exit_config=EXIT_CONFIG,
    )
    row["rule_flags"] = list(row["rule_flags"]) + [LABEL_B1, "SELECTOR_SWEEP"]
    row["heff_smc_trigger"] = sig["trigger"]
    row["heff_smc_score"] = sig["score"]
    row["heff_smc_in_charter_window"] = sig["in_charter_window"]
    return row


def run_variant(name: str, selector_config: SelectorConfig, signals: list) -> dict:
    logger.info("=== selector sweep variant '%s' starting: %s ===", name, selector_config)
    by_session: dict = {}
    for sig in signals:
        by_session.setdefault(sig["session"], []).append(sig)

    rows = []
    for session in sorted(by_session):
        free_mb = _free_ram_mb()
        if free_mb < MIN_FREE_RAM_MB:
            logger.warning("variant %s aborted early at session %s: %.0fMB free RAM", name, session, free_mb)
            break
        session_signals = by_session[session]
        symbol = session_signals[0]["symbol"]
        trades_df = _load_raw_session_trades(symbol, session, BACKFILL_RAW_DIR)
        greeks = _load_session_greeks(symbol, session, trades_df, BACKFILL_DIR)
        for sig in session_signals:
            rows.append(simulate_variant_signal(sig, trades_df, greeks, selector_config))
        del trades_df

    summary = summarize_b1(rows)
    n_sessions_with_signal = len({s["session"] for s in signals})
    summary["avg_filled_trades_per_session"] = round(
        summary["n_filled"] / n_sessions_with_signal, 3
    ) if n_sessions_with_signal else None

    SWEEP_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "variant": name,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "label": "B1 SELECTOR SWEEP -- EXPLORATORY, NOT PROMOTION-EVALUATED",
        "selector_config_used": {
            "allowed_dte": sorted(selector_config.allowed_dte),
            "premium_band": [selector_config.premium_low, selector_config.premium_high],
            "max_spread_dollars": selector_config.max_spread_dollars,
            "max_spread_pct_mid": selector_config.max_spread_pct_mid,
            "min_abs_delta": selector_config.min_abs_delta,
            "max_quote_age_seconds": selector_config.max_quote_age_seconds,
            "min_ask_size": selector_config.min_ask_size,
            "target_delta": selector_config.target_delta,
        },
        "baseline_reference": "data/thetadata/bt3_b1_indicator_only/bt3_b1_indicator_only_report.json "
                               "(162-session run, default SelectorConfig, generated 2026-07-29T23:26Z)",
        "summary": summary,
    }
    report_path = SWEEP_DIR / f"{name}_report.json"
    tmp = report_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2, default=str))
    tmp.replace(report_path)
    logger.info(
        "variant '%s' done: n_filled=%s avg/session=%s expectancy=%s",
        name, summary.get("n_filled"), summary.get("avg_filled_trades_per_session"),
        summary.get("net_expectancy_per_filled_trade"),
    )
    return payload


def main():
    logging.basicConfig(level=logging.INFO)
    events = load_triangle_events(TRIANGLE_EVENTS_PATH)
    signals = generate_b1_signals(events)
    logger.info("selector sweep: %d signals loaded, running %d variants sequentially", len(signals), len(VARIANTS))
    results = {}
    for name, cfg in VARIANTS.items():
        results[name] = run_variant(name, cfg, signals)
    logger.info("selector sweep complete: %s", list(results.keys()))


if __name__ == "__main__":
    main()
