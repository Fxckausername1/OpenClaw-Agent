#!/usr/bin/env python3
"""One-off: the walkforward harness's auto-champion gate requires beating baseline by 15%
TOTAL R on the search region before it will even look at the holdout -- a volume-cutting
quality filter (like the earlier EMA regime filter and tight-range ORB) can never clear that
bar even when it's a genuine quality upgrade. Manually score the sector-gated ORB leg
(the most promising of the three sector variants, +0.256R/tr vs baseline +0.194R/tr in search)
against the LOCKED holdout the same honest way those two were checked."""
import walkforward_search as wf

syms = wf.wide_universe.load_universe(rebuild_if_stale=False) or wf.mr.fetch_sp100()
cached = wf.load_cached(syms)
print(f"loaded {len(cached)} symbols")

all_dates = set()
for _, df in cached:
    all_dates.update(str(d) for d in df.index.date)
search_dates, holdout_dates = wf.date_split(all_dates)
print(f"search dates: {len(search_dates)}  holdout dates: {len(holdout_dates)}")

comps = {
    "mr_base": wf.MR_CAP,
    "orb_base": wf.ORB_CAP,
    "mr_sector": wf.MR_SECTOR,
    "orb_sector": wf.ORB_SECTOR,
}
trades = {}
for name, c in comps.items():
    t = wf.generate_component(c, cached)
    trades[name] = t
    print(f"  {name}: {len(t)} total trades cached")


def show(label, keys):
    s_search = wf.score_portfolio(keys, trades, search_dates)
    s_hold = wf.score_portfolio(keys, trades, holdout_dates)
    print(f"{label:42s} SEARCH  {wf.fmt(s_search)}")
    print(f"{'':42s} HOLDOUT {wf.fmt(s_hold)}")


print()
show("baseline (mr_base + orb_base)", ["mr_base", "orb_base"])
show("mr_base + orb_SECTOR (candidate 2)", ["mr_base", "orb_sector"])
show("mr_SECTOR + orb_base (candidate 1)", ["mr_sector", "orb_base"])
show("mr_SECTOR + orb_SECTOR (candidate 3)", ["mr_sector", "orb_sector"])
print()
show("orb_base ALONE", ["orb_base"])
show("orb_SECTOR ALONE", ["orb_sector"])
show("mr_base ALONE", ["mr_base"])
show("mr_SECTOR ALONE", ["mr_sector"])
