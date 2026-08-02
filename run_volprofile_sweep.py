#!/usr/bin/env python3
"""99-symbol sweep for gen_volprofile (MA-engagement + volume-profile liquidity strategy),
with the standard LOCKED 75/25 holdout. Reuses the exact same 99-symbol universe as the
intraday mean-rev/ORB sweeps (data/wf_cache/*.parquet symbol list) for apples-to-apples
comparison, but fetches DAILY bars (Alpaca) since this strategy trades off 50/200-DAY MAs.

Uses generate_component() so trades get the same net-of-cost (COST_BPS) treatment and
disk caching as every other strategy in this harness. Reports Win Rate / Profit Factor /
Average R for SEARCH and HOLDOUT separately, and flags any remaining R-magnitude or
stop-distance outliers in the final printout.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import numpy as np
import pandas as pd
import walkforward_search as wf

UNIVERSE = sorted(p.stem for p in wf.CACHE_DIR.glob("*.parquet"))
print(f"universe: {len(UNIVERSE)} symbols (same set as mean-rev/ORB sweeps)", file=sys.stderr)

print("fetching/caching daily bars (Alpaca, split-adjusted) ...", file=sys.stderr)
cached = wf.load_daily_cached(UNIVERSE)
print(f"got daily bars for {len(cached)}/{len(UNIVERSE)} symbols", file=sys.stderr)

comp = wf.comp_vp("vp_floor_v1")  # min_stop_pct=0.015, min_stop_atr_mult=1.0 floor now baked in
print(f"\nparams: {comp['p']}\n")

trades = wf.generate_component(comp, cached)
if trades.empty:
    print("NO TRADES generated -- check data/params before trusting anything downstream.")
    sys.exit(1)

all_dates = trades["date"].tolist()
search_dates, holdout_dates = wf.date_split(all_dates)
print(f"total trades: {len(trades)} | date range {min(all_dates)} -> {max(all_dates)}")
print(f"search region: {len(search_dates)} unique dates | holdout region: {len(holdout_dates)} unique dates\n")


def region_stats(t, label):
    if t.empty:
        print(f"--- {label}: 0 trades ---\n")
        return
    n = len(t)
    wins = t[t["r_gross"] > 0]
    losses = t[t["r_gross"] <= 0]
    win_rate = len(wins) / n
    gross_win = wins["r_gross"].sum()
    gross_loss = -losses["r_gross"].sum()  # positive number
    pf = (gross_win / gross_loss) if gross_loss > 0 else float("inf")
    avg_r_gross = t["r_gross"].mean()
    avg_r_net = t["net_r"].mean()
    total_r_net = t["net_r"].sum()
    daily = t.groupby("date")["net_r"].sum()
    sharpe = float(daily.mean() / daily.std() * np.sqrt(252)) if daily.std() > 0 else 0.0
    avg_risk_pct = t["risk_frac"].mean() * 100

    print(f"--- {label}: {n} trades ---")
    print(f"  Win Rate:        {win_rate*100:.1f}% ({len(wins)}/{n})")
    print(f"  Profit Factor:   {pf:.2f}  (gross win {gross_win:+.2f}R / gross loss {gross_loss:.2f}R)")
    print(f"  Average R (raw): {avg_r_gross:+.3f}")
    print(f"  Average R (net of {wf.COST_BPS}bp cost): {avg_r_net:+.3f}")
    print(f"  Total net R:     {total_r_net:+.2f}")
    print(f"  Sharpe (daily, annualized): {sharpe:.2f}")
    print(f"  Avg stop distance: {avg_risk_pct:.2f}% of entry")
    print(f"  LONG: {len(t[t.side=='LONG'])} tr, avg R {t[t.side=='LONG'].r_gross.mean():+.3f}"
          if len(t[t.side == "LONG"]) else "  LONG: 0 trades")
    print(f"  SHORT: {len(t[t.side=='SHORT'])} tr, avg R {t[t.side=='SHORT'].r_gross.mean():+.3f}"
          if len(t[t.side == "SHORT"]) else "  SHORT: 0 trades")
    print()


search_t = trades[trades["date"].isin(search_dates)]
holdout_t = trades[trades["date"].isin(holdout_dates)]
region_stats(search_t, "SEARCH (first 75% of dates)")
region_stats(holdout_t, "HOLDOUT (locked last 25% of dates)")

# carry check, same bar as every other strategy in this harness
if not search_t.empty and not holdout_t.empty:
    s_per_tr = search_t["net_r"].mean()
    h_per_tr = holdout_t["net_r"].mean()
    print(f"CARRY CHECK: search avg net R {s_per_tr:+.3f} -> holdout avg net R {h_per_tr:+.3f}  "
          f"({'CARRIES' if h_per_tr > 0 and h_per_tr >= 0.5*s_per_tr else 'DOES NOT CARRY / WEAK'})\n")

# ---- outlier flagging ----
print("=== OUTLIER FLAGS ===")
extreme_r = trades[trades["r_gross"].abs() > 5]
if not extreme_r.empty:
    print(f"  {len(extreme_r)} trade(s) with |R| > 5:")
    print(extreme_r.to_string(index=False))
else:
    print("  none with |R| > 5")

tight_stops = trades[trades["risk_frac"] < comp["p"]["min_stop_pct"] * 0.999]
if not tight_stops.empty:
    print(f"  {len(tight_stops)} trade(s) BELOW the {comp['p']['min_stop_pct']*100:.1f}% stop floor "
          f"(should be 0 if the floor is wired correctly):")
    print(tight_stops.to_string(index=False))
else:
    print(f"  none below the {comp['p']['min_stop_pct']*100:.1f}% stop floor -- floor is holding")

big_winners = trades.nlargest(5, "r_gross")[["date", "side", "r_gross", "risk_frac"]]
print(f"\n  top 5 winners (eyeball for fluke pattern):")
print(big_winners.to_string(index=False))
