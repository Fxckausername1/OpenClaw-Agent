#!/usr/bin/env python3
"""Smoke test for the float32/uint32 downcast in prep() (2026-06-28). Runs gen_mean_rev
and gen_orb on AAPL + NVDA against the OLD float64 baseline (backed up at /tmp before the
rebuild) and the NEW float32 cache, and diffs the resulting trade lists exactly -- same
dates, same sides, same R-outcomes (within float32 rounding) -- to confirm the downcast
didn't change any signal, not just the dtype."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import pandas as pd
import walkforward_search as wf

PAIRS = [("AAPL", "/tmp/AAPL_float64_baseline.parquet", "data/wf_cache/AAPL.parquet"),
         ("NVDA", "/tmp/NVDA_float64_baseline.parquet", "data/wf_cache/NVDA.parquet")]

mr_params = wf.comp_mr("smoke")["p"]
orb_params = wf.comp_orb("smoke")["p"]

all_ok = True
for sym, old_path, new_path in PAIRS:
    old_df = pd.read_parquet(old_path)
    new_df = pd.read_parquet(new_path)

    for gen, params, label in ((wf.gen_mean_rev, mr_params, "mean_rev"), (wf.gen_orb, orb_params, "orb")):
        old_rows = gen(old_df, params)
        new_rows = gen(new_df, params)
        old_t = pd.DataFrame(old_rows, columns=["date", "side", "r_gross", "risk_frac"])
        new_t = pd.DataFrame(new_rows, columns=["date", "side", "r_gross", "risk_frac"])

        same_n = len(old_t) == len(new_t)
        same_dates_sides = same_n and (old_t[["date", "side"]].reset_index(drop=True)
                                        .equals(new_t[["date", "side"]].reset_index(drop=True)))
        max_r_diff = (old_t["r_gross"].to_numpy() - new_t["r_gross"].to_numpy()).__abs__().max() if same_n and len(old_t) else 0.0
        max_risk_diff = (old_t["risk_frac"].to_numpy() - new_t["risk_frac"].to_numpy()).__abs__().max() if same_n and len(old_t) else 0.0

        ok = same_n and same_dates_sides and max_r_diff < 1e-3 and max_risk_diff < 1e-4
        all_ok = all_ok and ok
        print(f"{sym} {label}: old={len(old_t)} tr, new={len(new_t)} tr | "
              f"same dates/sides={same_dates_sides} | max R diff={max_r_diff:.6f} | "
              f"max risk_frac diff={max_risk_diff:.6f} | {'PASS' if ok else 'FAIL'}")

print(f"\n{'ALL SMOKE TESTS PASSED' if all_ok else 'SMOKE TEST FAILURE -- DO NOT TRUST THE REBUILT CACHE'}")
sys.exit(0 if all_ok else 1)
