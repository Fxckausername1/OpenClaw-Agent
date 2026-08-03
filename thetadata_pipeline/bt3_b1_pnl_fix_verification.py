"""Verification run for the 2026-07-31 P&L unit fix.

Re-runs the EXACT B1 indicator-only baseline (default SelectorConfig, unchanged
selection, unchanged exit rule, same 1,779-signal triangle set) with the corrected
accounting, into its OWN immutable run directory, and checks the headline metrics
against the independently-specified expected values.

Expected after the fix (specified by heff, not derived here -- so this is a real
check, not a tautology):
    filled trades  278
    total net P&L  $1,720.20
    expectancy     $6.19 / trade
    win rate       69.06%
    profit factor  ~4.1129

Before the fix the same baseline reported $7.27/trade -- inflated because
execution slippage was subtracted in OPTION PREMIUM units from a DOLLAR
midpoint P&L (see bt2_fills.friction_metrics).

Nothing here changes selection or exit logic; if these numbers land, the ONLY
thing that moved is the accounting.
"""

from __future__ import annotations

import json
import logging
import sys

from .bt3_b1_indicator_only import (
    EXPERIMENT_ID, TRIANGLE_EVENTS_PATH, generate_b1_signals, load_triangle_events,
    render_summary_md, run_b1, summarize_b1,
)
from .bt_run import assert_fresh_ledger, new_run

logger = logging.getLogger("thetadata_pkg.bt3_b1_pnl_fix_verification")

EXPECTED = {
    "n_filled": 278,
    "total_net_pnl": 1720.20,
    "net_expectancy_per_filled_trade": 6.19,
    "win_rate": 0.6906,
    "profit_factor": 4.1129,
}
TOLERANCES = {
    "n_filled": 0,              # exact: selection did not change
    "total_net_pnl": 1.00,      # cents-level rounding across 278 rows
    "net_expectancy_per_filled_trade": 0.01,
    "win_rate": 0.0005,
    "profit_factor": 0.01,
}


def _total_net_pnl(rows: list) -> float:
    return round(sum(r["net_pnl"] for r in rows if r.get("net_pnl") is not None), 2)


def _check(summary: dict, rows: list) -> dict:
    actual = {
        "n_filled": summary["n_filled"],
        "total_net_pnl": _total_net_pnl(rows),
        "net_expectancy_per_filled_trade": summary["net_expectancy_per_filled_trade"],
        "win_rate": round(summary["win_rate"], 4),
        "profit_factor": summary["profit_factor"],
    }
    checks = {}
    for key, expected in EXPECTED.items():
        got = actual[key]
        tol = TOLERANCES[key]
        ok = abs(got - expected) <= tol
        checks[key] = {"expected": expected, "actual": got, "tolerance": tol, "pass": ok}
    return {"actual": actual, "checks": checks,
            "all_pass": all(c["pass"] for c in checks.values())}


def _verify_row_level_identity(rows: list) -> dict:
    """Independent of the headline numbers: EVERY filled row's net_pnl must equal
    (exit_fill - entry_fill) * qty * 100 - fees. This is the contract, checked on
    real data rather than a fixture."""
    mismatches = []
    checked = 0
    for r in rows:
        if r.get("net_pnl") is None or not r.get("entry_fill") or not r.get("exit_fill"):
            continue
        qty = r["quantity"][0] if isinstance(r["quantity"], list) else r["quantity"]
        entry = r["entry_fill"][0] if isinstance(r["entry_fill"], list) else r["entry_fill"]
        exit_ = r["exit_fill"][0] if isinstance(r["exit_fill"], list) else r["exit_fill"]
        direct = round((exit_ - entry) * qty * 100 - (r["fees"] or 0.0), 2)
        checked += 1
        if abs(direct - r["net_pnl"]) > 0.011:
            mismatches.append({"trade_id": r["trade_id"], "direct": direct,
                               "recorded": r["net_pnl"]})
    return {"rows_checked": checked, "mismatches": mismatches[:10],
            "n_mismatches": len(mismatches), "identity_holds": not mismatches}


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    events = load_triangle_events(TRIANGLE_EVENTS_PATH)
    signals = generate_b1_signals(events)
    sessions_expected = len({s["session"] for s in signals})
    logger.info("verification: %d signals across %d sessions", len(signals), sessions_expected)

    run = new_run(EXPERIMENT_ID, label="pnl-unit-fix verification (default SelectorConfig)",
                  params={"selector": "SelectorConfig() defaults -- unchanged",
                          "purpose": "verify 2026-07-31 P&L unit fix reproduces expected baseline",
                          "expected": EXPECTED})
    assert_fresh_ledger(run.ledger_path)
    logger.info("run dir: %s", run.root)

    # run_b1 loads the triangle events itself; we only redirect its OUTPUT into
    # this run's isolated ledger so nothing appends to the shared canonical file.
    rows = run_b1(ledger_path=run.ledger_path)
    summary = summarize_b1(rows)
    sessions_processed = len({r["session"] for r in rows})
    run.mark_complete(sessions_processed=sessions_processed, sessions_expected=sessions_expected)

    verdict = _check(summary, rows)
    identity = _verify_row_level_identity(rows)

    payload = {"run_id": run.run_id, "summary": summary, "verification": verdict,
               "row_level_identity": identity,
               "sessions_processed": sessions_processed,
               "sessions_expected": sessions_expected}
    run.report_path.write_text(json.dumps(payload, indent=2, default=str))
    run.summary_path.write_text(render_summary_md(summary))
    run.update_manifest(verification=verdict, row_level_identity_holds=identity["identity_holds"])

    print(json.dumps({
        "run_dir": str(run.root),
        "sessions": f"{sessions_processed}/{sessions_expected}",
        "complete": sessions_processed >= sessions_expected,
        "verification": verdict,
        "row_level_identity": {k: identity[k] for k in
                                ("rows_checked", "n_mismatches", "identity_holds")},
    }, indent=2, default=str))

    if sessions_processed < sessions_expected:
        logger.error("PARTIAL RUN (%d/%d sessions) -- headline comparison is NOT valid",
                     sessions_processed, sessions_expected)
        return 2
    return 0 if (verdict["all_pass"] and identity["identity_holds"]) else 1


if __name__ == "__main__":
    sys.exit(main())
