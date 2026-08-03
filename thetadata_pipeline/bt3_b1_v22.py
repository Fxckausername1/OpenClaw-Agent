"""B1 re-run on the v2.2 indicator (heff's explicit "re-run B1 before optimizing").

Two things this deliberately does NOT do:
  * it does not overwrite the v2.1 triangle_events.json baseline -- v2.2 events go
    to their own file, so the previous run stays reproducible;
  * it does not append to any shared ledger -- output lands in an isolated
    bt_run directory with a manifest recording code hashes and completeness.

Comparison target is the CORRECTED v2.1 baseline (278 fills / $1,720.20 /
$6.19 per trade), i.e. the numbers measured after the 2026-07-31 P&L unit fix --
not the pre-fix $7.27 figure, which was inflated by subtracting premium-unit
slippage from dollar-denominated midpoint P&L.

Everything here is development-window data. Per BT0_CHARTER's 2026-07-31
addendum the development window closed 2026-07-30, so NO result from this run is
promotable or out-of-sample. It measures what v2.2 does to the signal set; it
does not validate it.
"""

from __future__ import annotations

import json
import logging
import sys

from .bt3_b1_indicator_only import (
    EXPERIMENT_ID, generate_b1_signals, load_triangle_events, render_summary_md,
    run_b1, summarize_b1,
)
from .bt_run import assert_fresh_ledger, new_run
from .heff_smc_engine import HeffSmcConfig
from .heff_smc_replay import (
    REPLAY_DIR, build_continuous_1min_series, run_replay,
)
from .qqq_bars_fetch import SYMBOL, list_target_sessions, load_all_bars

logger = logging.getLogger("thetadata_pkg.bt3_b1_v22")

V22_EVENTS_PATH = REPLAY_DIR / "triangle_events_v2.2.json"

# Corrected v2.1 baseline (post-P&L-unit-fix, run 20260731T215546Z-75f595).
V21_BASELINE = {
    "n_filled": 278, "total_net_pnl": 1720.20, "expectancy": 6.19,
    "win_rate": 0.6906, "profit_factor": 4.1129, "n_signals": 1779,
}


def regenerate_v22_events() -> list:
    """Replays the real 162-session QQQ history through the v2.2-synced engine."""
    raw = load_all_bars(SYMBOL, list_target_sessions())
    cont = build_continuous_1min_series(raw)
    events, _diag = run_replay(cont, HeffSmcConfig())
    V22_EVENTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = V22_EVENTS_PATH.with_suffix(".tmp")
    # Same envelope as the canonical triangle_events.json -- load_triangle_events
    # reads payload["events"], so a bare list fails with AttributeError.
    tmp.write_text(json.dumps({"events": events, "indicator_version": "v2.2"},
                              indent=2, default=str))
    tmp.replace(V22_EVENTS_PATH)
    logger.info("v2.2 triangle events written: %d -> %s", len(events), V22_EVENTS_PATH)
    return events


def main() -> int:
    logging.basicConfig(level=logging.INFO)

    events = regenerate_v22_events()
    signals = generate_b1_signals(load_triangle_events(V22_EVENTS_PATH))
    sessions_expected = len({s["session"] for s in signals})
    logger.info("v2.2: %d events -> %d signals across %d sessions",
                len(events), len(signals), sessions_expected)

    run = new_run(EXPERIMENT_ID, label="B1 on indicator v2.2 (default-on fixes)",
                  params={"indicator_version": "v2.2",
                          "indicator_pine_sha256":
                              "360aeadf18f6c3247307fe54d154994d84f8dbd05c487a9ceeebc73c4f04fb76",
                          "events_path": str(V22_EVENTS_PATH),
                          "selector": "SelectorConfig() defaults -- unchanged",
                          "v21_baseline": V21_BASELINE,
                          "promotable": False,
                          "note": "development-window data; BT0 charter forbids "
                                  "calling this validated or out-of-sample"})
    assert_fresh_ledger(run.ledger_path)
    logger.info("run dir: %s", run.root)

    rows = run_b1(events_path=V22_EVENTS_PATH, ledger_path=run.ledger_path)
    summary = summarize_b1(rows)
    sessions_processed = len({r["session"] for r in rows})
    run.mark_complete(sessions_processed=sessions_processed,
                      sessions_expected=sessions_expected)

    total = round(sum(r["net_pnl"] for r in rows if r.get("net_pnl") is not None), 2)
    actual = {
        "n_signals": len(signals), "n_filled": summary["n_filled"],
        "total_net_pnl": total,
        "expectancy": summary["net_expectancy_per_filled_trade"],
        "win_rate": round(summary["win_rate"], 4),
        "profit_factor": summary["profit_factor"],
    }
    delta = {k: round(actual[k] - V21_BASELINE[k], 4) for k in V21_BASELINE if k in actual}

    payload = {"run_id": run.run_id, "indicator_version": "v2.2",
               "v21_baseline": V21_BASELINE, "v22_actual": actual, "delta": delta,
               "sessions_processed": sessions_processed,
               "sessions_expected": sessions_expected,
               "promotable": False, "summary": summary}
    run.report_path.write_text(json.dumps(payload, indent=2, default=str))
    run.summary_path.write_text(render_summary_md(summary))
    run.update_manifest(v22_actual=actual, delta=delta)

    print(json.dumps({"run_dir": str(run.root),
                      "sessions": f"{sessions_processed}/{sessions_expected}",
                      "complete": sessions_processed >= sessions_expected,
                      "v21_baseline": V21_BASELINE, "v22_actual": actual,
                      "delta": delta}, indent=2, default=str))
    return 0 if sessions_processed >= sessions_expected else 2


if __name__ == "__main__":
    sys.exit(main())
