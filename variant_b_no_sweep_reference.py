"""VARIANT_B_NO_SWEEP descriptive reference.

DOCUMENTATION ONLY. This is not a sweep, not an optimization, and not a
validation. It re-describes rows that ALREADY EXIST -- the Variant B rows
written by selector_policy_experiment_v1 -- with SWEEP_RECLAIM signals
removed, because the live frozen candidate excludes that trigger upstream
and Variant B's published totals therefore do not describe it.

No parameter is changed. No candidate is selected. Nothing is re-run. The
only operation is a filter and a recount, so nothing here can constitute a
second look at the holdout for selection purposes -- it is the same single
Variant B result, reported over the subset that will actually trade.

Reports train and holdout separately and never pools them, matching the
locked chronological split the experiment already established.
"""
from __future__ import annotations

import json
import math
import statistics
from pathlib import Path

RAW = Path("data/thetadata/runs/selector_policy_experiment_v1/"
           "20260801T180703Z-bbfc8c/raw_rows.json")
VARIANT = "B_delta_first_debit_cap"
EXCLUDED = "SWEEP_RECLAIM"


def mean_ci(values, n_boot=5000, seed=20260801):
    """Percentile bootstrap CI on the mean. Same estimator family the B-series
    already uses; deterministic seed so this document is reproducible."""
    if len(values) < 2:
        return (None, None, None)
    import random
    rng = random.Random(seed)
    m = statistics.fmean(values)
    boots = []
    n = len(values)
    for _ in range(n_boot):
        boots.append(statistics.fmean([values[rng.randrange(n)] for _ in range(n)]))
    boots.sort()
    return (m, boots[int(0.025 * n_boot)], boots[int(0.975 * n_boot)])


def describe(rows, label):
    filled = [r for r in rows if r.get("entry_fill")]
    pnl = [float(r["net_pnl"]) for r in filled
           if r.get("net_pnl") is not None and not isinstance(r.get("net_pnl"), list)]
    wins = [p for p in pnl if p > 0]
    m, lo, hi = mean_ci(pnl) if pnl else (None, None, None)
    sessions = {r.get("session") for r in rows}
    sessions_filled = {r.get("session") for r in filled}
    gross_win = sum(p for p in pnl if p > 0)
    gross_loss = -sum(p for p in pnl if p < 0)
    return {
        "label": label,
        "signals": len(rows),
        "sessions": len(sessions),
        "filled_trades": len(filled),
        "fill_rate": round(len(filled) / len(rows), 4) if rows else None,
        "sessions_with_a_fill": len(sessions_filled),
        "win_rate": round(len(wins) / len(pnl), 4) if pnl else None,
        "mean_net_pnl": round(m, 4) if m is not None else None,
        "ci95_low": round(lo, 4) if lo is not None else None,
        "ci95_high": round(hi, 4) if hi is not None else None,
        "total_net_pnl": round(sum(pnl), 2) if pnl else None,
        "profit_factor": round(gross_win / gross_loss, 3) if gross_loss else None,
    }


def main():
    all_rows = json.loads(RAW.read_text())
    rows = all_rows[VARIANT]
    print(f"# source: {RAW}")
    print(f"# variant: {VARIANT}   rows: {len(rows)}")

    kept = [r for r in rows if r.get("heff_smc_trigger") != EXCLUDED]
    dropped = [r for r in rows if r.get("heff_smc_trigger") == EXCLUDED]
    print(f"# SWEEP_RECLAIM rows removed: {len(dropped)}   remaining: {len(kept)}\n")

    blocks = []
    for seg in ("train", "holdout"):
        blocks.append(("VARIANT_B (as published)",
                       describe([r for r in rows if r.get("segment") == seg], seg)))
        blocks.append(("VARIANT_B_NO_SWEEP",
                       describe([r for r in kept if r.get("segment") == seg], seg)))
    blocks.append(("VARIANT_B (as published)", describe(rows, "ALL")))
    blocks.append(("VARIANT_B_NO_SWEEP", describe(kept, "ALL")))

    hdr = (f"{'candidate':<26}{'seg':<9}{'sig':>5}{'fills':>6}{'fill%':>7}"
           f"{'win%':>7}{'mean$':>8}{'ci95_lo':>9}{'ci95_hi':>9}{'total$':>9}{'PF':>7}")
    print(hdr)
    print("-" * len(hdr))
    for name, d in blocks:
        print(f"{name:<26}{d['label']:<9}{d['signals']:>5}{d['filled_trades']:>6}"
              f"{(d['fill_rate'] or 0)*100:>6.1f}%{(d['win_rate'] or 0)*100:>6.1f}%"
              f"{d['mean_net_pnl'] if d['mean_net_pnl'] is not None else 0:>8.2f}"
              f"{d['ci95_low'] if d['ci95_low'] is not None else 0:>9.2f}"
              f"{d['ci95_high'] if d['ci95_high'] is not None else 0:>9.2f}"
              f"{d['total_net_pnl'] if d['total_net_pnl'] is not None else 0:>9.2f}"
              f"{d['profit_factor'] if d['profit_factor'] is not None else 0:>7.2f}")

    # Trigger composition of what actually remains.
    comp = {}
    for r in kept:
        comp[r.get("heff_smc_trigger")] = comp.get(r.get("heff_smc_trigger"), 0) + 1
    print(f"\n# VARIANT_B_NO_SWEEP trigger composition: "
          f"{json.dumps(dict(sorted(comp.items(), key=lambda kv: -kv[1])))}")

    out = {"source": str(RAW), "variant": VARIANT,
           "sweep_reclaim_rows_removed": len(dropped),
           "blocks": [{"candidate": n, **d} for n, d in blocks],
           "trigger_composition": comp}
    Path("VARIANT_B_NO_SWEEP_REFERENCE.json").write_text(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
