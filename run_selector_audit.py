"""Runner for SELECTOR_REJECTION_AUDIT_v1 -- read-only. Produces an isolated
run directory under data/thetadata/runs/selector_rejection_audit_v1/<run_id>/
with parity.json, per_signal.json, funnel.json, manifest.json. Never touches
cron, arming state, broker state, or selector policy.
"""
from __future__ import annotations

import json
import logging
import sys
import time

from thetadata_pipeline import selector_rejection_audit as sra
from thetadata_pipeline.bt_run import new_run, assert_fresh_ledger

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logger = logging.getLogger("selector_rejection_audit_runner")


def main() -> int:
    t0 = time.time()
    logger.info("Verifying frozen-run inputs unchanged...")
    frozen_manifest = sra.assert_frozen_inputs_unchanged()
    frozen_report = json.loads((sra.FROZEN_RUN_DIR / "report.json").read_text())

    logger.info("Starting instrumented 162-session replay (reproduces %s)...", sra.FROZEN_RUN_ID)
    records = sra.reproduce_with_candidates()
    logger.info("Replay complete: %d signal records in %.1fs", len(records), time.time() - t0)

    logger.info("Verifying parity against frozen report.json...")
    parity = sra.verify_parity(records, frozen_report)
    logger.info("PARITY CONFIRMED: %s", parity)

    logger.info("Building per-signal records and funnel...")
    per_signal, funnel = sra.build_funnel(records)

    logger.info("Computing one-rule-at-a-time counterfactual gate sensitivity...")
    counterfactual = sra.counterfactual_gate_sensitivity(records)
    logger.info("Building candidates index for reproducibility...")
    cand_index = sra.candidates_index(records)

    run = new_run(
        sra.AUDIT_EXPERIMENT_ID,
        label="Selector rejection audit v1 -- read-only funnel over frozen v2.2 B1 run",
        params={
            "frozen_run_id": sra.FROZEN_RUN_ID,
            "frozen_experiment_id": sra.FROZEN_EXPERIMENT_ID,
            "frozen_git_commit": frozen_manifest["git_commit"],
            "frozen_events_path": frozen_manifest["params"]["events_path"],
            "frozen_indicator_pine_sha256": frozen_manifest["params"]["indicator_pine_sha256"],
            "parity": parity,
            "promotable": False,
            "note": "Diagnostic audit only. Not a selector policy change. "
                    "Development window closed 2026-07-30 per BT0_CHARTER.md.",
        },
    )
    assert_fresh_ledger(run.root / "per_signal.json")

    (run.root / "per_signal.json").write_text(json.dumps(per_signal, indent=2, default=str))
    (run.root / "funnel.json").write_text(json.dumps(funnel, indent=2, default=str))
    (run.root / "parity.json").write_text(json.dumps(parity, indent=2, default=str))
    (run.root / "counterfactual_gate_sensitivity.json").write_text(json.dumps(counterfactual, indent=2, default=str))
    (run.root / "candidates_index.json").write_text(json.dumps(cand_index, indent=2, default=str))
    run.mark_complete(sessions_processed=funnel["n_total_signals"], sessions_expected=funnel["n_total_signals"],
                       note="signal-level completeness (not session-level -- every signal produced a record)")
    run.update_manifest(funnel_summary={
        "n_total_signals": funnel["n_total_signals"],
        "by_outcome": funnel["by_outcome"],
        "by_rejection_group": funnel["by_rejection_group"],
    })

    print(json.dumps({
        "run_dir": str(run.root),
        "run_id": run.run_id,
        "elapsed_s": round(time.time() - t0, 1),
        "parity": parity,
        "by_outcome": funnel["by_outcome"],
        "by_rejection_group": funnel["by_rejection_group"],
        "by_primary_category": funnel["by_primary_category"],
        "counterfactual_gate_sensitivity": counterfactual,
    }, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
