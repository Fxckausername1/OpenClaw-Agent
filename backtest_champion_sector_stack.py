#!/usr/bin/env python3
"""backtest_champion_sector_stack.py -- locked-holdout test of whether the new continuous_search
champion (orb_vm2.5_mr0.006_TrueTrue: vol_mult=2.5, max_range_frac=0.006, found 2026-07-03 under
the corrected per-trade-R ranking) stacks with the sector-gate, the way tight-range+sector stacked
before it (see backtest_orb_combo.py). The new champion was found via continuous_search.py's grid,
which only varies vol_mult/max_range_frac/use_vwap/use_vol on plain comp_orb (base "orb") -- it has
never been tested with the sector dimension at all. This checks the missing cell directly on
comp_orbsec (base "orb_sector"), same params.

Standalone script, same reasoning as backtest_orb_combo.py: an isolated comparison, doesn't touch
walkforward_search.py's own CANDIDATES/main() ratchet flow.

Run: ./venv/bin/python backtest_champion_sector_stack.py
"""
import time

import walkforward_search as wf


def fmt(s):
    return f"{s['total_r']:+8.1f}R  n={s['n']:>5}  {s['per_trade']:+.3f}R/tr  Sharpe={s['sharpe']:+.2f}"


def main():
    syms = wf.wide_universe.load_universe(rebuild_if_stale=False)
    t0 = time.time()
    cached = wf.load_cached(syms)
    print(f"loaded {len(cached)} intraday-cached symbols ({time.time()-t0:.0f}s)")

    CANDIDATES = {
        "ORB baseline (uncapped, no sector)":            wf.ORB_CAP,
        "current LIVE (vm1.5, range<=0.66%, sector)":    wf.comp_orbsec("orb_live", vol_mult=1.5, max_range_frac=0.0066),
        "new champion, no sector (vm2.5, range<=0.6%)":  wf.comp_orb("orb_new_champ", vol_mult=2.5, max_range_frac=0.006),
        "new champion + sector-gate STACKED":            wf.comp_orbsec("orb_new_champ_sector", vol_mult=2.5, max_range_frac=0.006),
    }

    trades = {}
    t0 = time.time()
    for name, c in CANDIDATES.items():
        k = wf.component_key(c)
        t = wf.generate_component(c, cached)
        trades[k] = t
        n = len(t)
        tot = float(t["net_r"].sum()) if n else 0.0
        print(f"  {name:<48} {n:>5} tr | net {tot:+7.1f}R ({time.time()-t0:.0f}s)")

    all_dates = []
    for t in trades.values():
        all_dates += t["date"].tolist()
    search, holdout = wf.date_split(all_dates)
    print(f"\nsearch days={len(search)} holdout days={len(holdout)}\n")

    print("SEARCH region (orientation only):")
    search_scores = {}
    for name, c in CANDIDATES.items():
        k = wf.component_key(c)
        s = wf.score_portfolio([k], trades, search)
        search_scores[name] = s
        print(f"  {name:<48} {fmt(s)}")

    print("\nLOCKED HOLDOUT (the honest test):")
    holdout_scores = {}
    for name, c in CANDIDATES.items():
        k = wf.component_key(c)
        h = wf.score_portfolio([k], trades, holdout)
        holdout_scores[name] = h
        print(f"  {name:<48} {fmt(h)}")

    base = holdout_scores["ORB baseline (uncapped, no sector)"]
    live = holdout_scores["current LIVE (vm1.5, range<=0.66%, sector)"]
    champ_nosec = holdout_scores["new champion, no sector (vm2.5, range<=0.6%)"]
    champ_sec = holdout_scores["new champion + sector-gate STACKED"]

    print("\nVERDICT (ranked on per-trade R, matching the corrected continuous_search.py ranking):")
    if champ_sec["n"] < wf.MIN_TRADES:
        print(f"  stacked candidate has too few holdout trades ({champ_sec['n']} < {wf.MIN_TRADES}) to trust")
    else:
        print(f"  new champion (no sector)  vs plain baseline: {champ_nosec['per_trade']/base['per_trade']:.2f}x")
        print(f"  current live (vm1.5+sector) vs plain baseline: {live['per_trade']/base['per_trade']:.2f}x")
        print(f"  new champion + sector STACKED vs plain baseline: {champ_sec['per_trade']/base['per_trade']:.2f}x")
        print(f"  stacked vs current live: {champ_sec['per_trade']/live['per_trade']:.2f}x")
        print(f"  stacked vs new champion alone (no sector): {champ_sec['per_trade']/champ_nosec['per_trade']:.2f}x")
        best_unstacked = max(live["per_trade"], champ_nosec["per_trade"])
        if champ_sec["per_trade"] > best_unstacked * 1.05:
            print("  -> STACKS: sector-gate on top of the new champion beats the better of the two unstacked options")
        elif champ_sec["per_trade"] > best_unstacked * 0.90:
            print("  -> OVERLAPS: roughly the same as the better unstacked option (no real stacking, but no harm)")
        else:
            print("  -> HURTS: stacking is worse than the better unstacked option alone (over-filtering / thin sample)")
        print(f"\n  trade counts -- live: {live['n']}, new champ alone: {champ_nosec['n']}, stacked: {champ_sec['n']}")


if __name__ == "__main__":
    main()
