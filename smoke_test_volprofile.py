#!/usr/bin/env python3
"""Smoke test for gen_volprofile (MA-engagement + volume-profile liquidity strategy,
heff's primary chart workflow, 2026-06-28). Fetches daily bars for SPY (proxy for SPX --
Alpaca has no index bars) and NVDA via Alpaca, runs the generator with default params,
prints trade-level detail + summary stats. NOT the 99-symbol sweep -- just confirms the
mechanics fire sanely and produces sensible TP/SL routing before committing to a full
walkforward run with the locked holdout.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import pandas as pd
import walkforward_search as wf

SYMS = ["SPY", "NVDA"]

print(f"fetching daily bars (Alpaca, split-adjusted) for {SYMS} ...", file=sys.stderr)
cached = wf.load_daily_cached(SYMS, refresh=True)
if not cached:
    print("NO DATA -- check Alpaca creds / connectivity", file=sys.stderr)
    sys.exit(1)

comp = wf.comp_vp("vp_smoke")
print(f"params: {comp['p']}\n")

for sym, df in cached:
    print(f"=== {sym}: {len(df)} daily bars, {df.index.min().date()} -> {df.index.max().date()} ===")
    rows = wf.gen_volprofile(df, comp["p"])
    if not rows:
        print("  0 trades\n")
        continue
    t = pd.DataFrame(rows, columns=["date", "side", "r_gross", "risk_frac"])
    wins = (t["r_gross"] > 0).sum()
    print(f"  {len(t)} trades | win {wins}/{len(t)} ({100*wins/len(t):.0f}%) | "
          f"avg R {t['r_gross'].mean():+.3f} | total R {t['r_gross'].sum():+.2f} | "
          f"avg risk% {100*t['risk_frac'].mean():.2f}%")
    print(f"  LONG: {len(t[t.side=='LONG'])} trades, avg R {t[t.side=='LONG'].r_gross.mean():+.3f}"
          if len(t[t.side == "LONG"]) else "  LONG: 0 trades")
    print(f"  SHORT: {len(t[t.side=='SHORT'])} trades, avg R {t[t.side=='SHORT'].r_gross.mean():+.3f}"
          if len(t[t.side == "SHORT"]) else "  SHORT: 0 trades")
    print("  sample trades:")
    print(t.head(10).to_string(index=False))
    print()
