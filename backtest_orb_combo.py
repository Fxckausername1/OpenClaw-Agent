#!/usr/bin/env python3
"""backtest_orb_combo.py — locked-holdout test of ORB's two independently-proven refinements
(tight-range <=0.66%, sector-gate) COMBINED. Never tested together before 2026-07-02: the
tight-range sweep (CANDIDATES) and the sector test (SECTOR_CANDIDATES) in walkforward_search.py
are two separate lists that never overlapped. gen_orb_sector already supports max_range_frac
(mirrors gen_orb's full param space), so this is a pure parameter combination on the existing
comp_orbsec() wrapper -- no new generator code needed.

Standalone script (not touching walkforward_search.py's own CANDIDATES/main()) -- same
reasoning as backtest_volprofile.py: an isolated comparison, not meant to disturb the live
main() ratchet flow. Reuses generate_component/date_split/score_portfolio unchanged.

Run: ./venv/bin/python backtest_orb_combo.py
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
        "ORB baseline (uncapped range, no sector)":      wf.ORB_CAP,
        "ORB tight-range only (<=0.66%)":                wf.comp_orb("orb_tight_only", max_range_frac=0.0066),
        "ORB sector-gate only (uncapped range)":         wf.ORB_SECTOR,
        "ORB tight-range + sector-gate COMBINED":        wf.comp_orbsec("orb_combo", max_range_frac=0.0066),
    }

    trades = {}
    t0 = time.time()
    for name, c in CANDIDATES.items():
        k = wf.component_key(c)
        t = wf.generate_component(c, cached)
        trades[k] = t
        n = len(t)
        tot = float(t["net_r"].sum()) if n else 0.0
        print(f"  {name:<42} {n:>5} tr | net {tot:+7.1f}R ({time.time()-t0:.0f}s)")

    all_dates = []
    for t in trades.values():
        all_dates += t["date"].tolist()
    search, holdout = wf.date_split(all_dates)
    print(f"\nsearch days={len(search)} holdout days={len(holdout)}\n")

    print("SEARCH region (orientation only):")
    for name, c in CANDIDATES.items():
        k = wf.component_key(c)
        s = wf.score_portfolio([k], trades, search)
        print(f"  {name:<42} {fmt(s)}")

    print("\nLOCKED HOLDOUT (the honest test):")
    holdout_scores = {}
    for name, c in CANDIDATES.items():
        k = wf.component_key(c)
        h = wf.score_portfolio([k], trades, holdout)
        holdout_scores[name] = h
        print(f"  {name:<42} {fmt(h)}")

    base = holdout_scores["ORB baseline (uncapped range, no sector)"]
    combo = holdout_scores["ORB tight-range + sector-gate COMBINED"]
    tight = holdout_scores["ORB tight-range only (<=0.66%)"]
    sect = holdout_scores["ORB sector-gate only (uncapped range)"]
    print("\nVERDICT:")
    if combo["n"] < wf.MIN_TRADES:
        print(f"  combo has too few holdout trades ({combo['n']} < {wf.MIN_TRADES}) to trust")
    else:
        print(f"  combo vs baseline per-trade ratio: {combo['per_trade']/base['per_trade']:.2f}x "
              f"(tight-only alone: {tight['per_trade']/base['per_trade']:.2f}x, "
              f"sector-only alone: {sect['per_trade']/base['per_trade']:.2f}x)")
        best_single = max(tight["per_trade"], sect["per_trade"])
        if combo["per_trade"] > best_single * 1.05:
            print("  -> STACKS: combined beats the better of the two single filters")
        elif combo["per_trade"] > best_single * 0.90:
            print("  -> OVERLAPS: combined is roughly the same as the better single filter (no real stacking, but no harm)")
        else:
            print("  -> HURTS: combining is worse than the better single filter alone (over-filtering / thin sample)")


if __name__ == "__main__":
    main()
