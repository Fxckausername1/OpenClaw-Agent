"""Runner for the paper-only selector policy experiment (heff-requested,
2026-08-01). Read-only against the frozen v2.2 replay dataset; produces an
isolated run directory under
data/thetadata/runs/selector_policy_experiment_v1/<run_id>/ via
bt_run.new_run. Never touches cron, arming state, broker state, or the
frozen baseline files.
"""
from __future__ import annotations

import json
import logging
import sys
import time

from thetadata_pipeline import selector_policy_experiment as spe
from thetadata_pipeline import selector_policy_stats as sps
from thetadata_pipeline.bt_run import assert_fresh_ledger, new_run

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logger = logging.getLogger("selector_policy_experiment_runner")


def main() -> int:
    t0 = time.time()
    logger.info("Starting 5-variant combined replay (single I/O pass, 162 sessions)...")
    all_rows = spe.run_all_variants()
    for name, rows in all_rows.items():
        logger.info("  %s: %d rows", name, len(rows))
    logger.info("Replay complete in %.1fs", time.time() - t0)

    logger.info("Computing full per-variant/per-segment statistics...")
    report = sps.full_report(all_rows)
    sweep_reclaim_relief = sps.sweep_reclaim_premium_relief_check(all_rows)

    run = new_run(
        "selector_policy_experiment_v1",
        label="Paper-only selector policy experiment: A/B/C1/C2/C3, locked train/holdout split",
        params={
            "variants": {name: str(cfg) for name, cfg in spe.VARIANT_CONFIGS.items()},
            "debit_cap_dollars": spe.DEBIT_CAP_DOLLARS,
            "fee_per_contract": spe.DEFAULT_FEE_PER_CONTRACT,
            "quantity": spe.DEFAULT_QUANTITY,
            "train_start": spe.TRAIN_START, "train_end": spe.TRAIN_END,
            "holdout_start": spe.HOLDOUT_START, "holdout_end": spe.HOLDOUT_END,
            "events_path": str(spe.V22_EVENTS_PATH),
            "promotable": False,
            "note": "Paper-only experiment. No selector policy change made to "
                    "production. No cron/arming/broker state touched.",
        },
    )
    assert_fresh_ledger(run.root / "raw_rows.json")

    (run.root / "raw_rows.json").write_text(json.dumps(all_rows, indent=2, default=str))
    (run.root / "report.json").write_text(json.dumps(report, indent=2, default=str))
    (run.root / "sweep_reclaim_premium_relief.json").write_text(
        json.dumps(sweep_reclaim_relief, indent=2, default=str)
    )
    total_signals = sum(len(rows) for rows in all_rows.values())
    run.mark_complete(sessions_processed=total_signals, sessions_expected=total_signals,
                       note="row-level completeness across all 5 variants combined")

    headline = {
        name: {
            "full_n_filled": report[name]["full"]["n_filled"],
            "full_expectancy": report[name]["full"]["net_expectancy_per_filled_trade"],
            "holdout_n_filled": report[name]["holdout"]["n_filled"],
            "holdout_expectancy": report[name]["holdout"]["net_expectancy_per_filled_trade"],
            "holdout_expectancy_ci": report[name]["holdout"]["net_expectancy_session_bootstrap_95ci"],
        }
        for name in all_rows
    }
    run.update_manifest(headline=headline)

    print(json.dumps({
        "run_dir": str(run.root), "run_id": run.run_id,
        "elapsed_s": round(time.time() - t0, 1),
        "headline": headline,
        "sweep_reclaim_premium_relief": sweep_reclaim_relief,
    }, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
