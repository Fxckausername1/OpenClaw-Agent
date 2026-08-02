#!/usr/bin/env python3
"""heff's ask: does the sector-rotation gate on ORB still carry (or carry BETTER) with the
relvol>=1.5x entry filter turned off? Tests baseline-novol vs sector-gated-novol, same locked
holdout as every other check."""
import walkforward_search as wf

syms = wf.wide_universe.load_universe(rebuild_if_stale=False) or wf.mr.fetch_sp100()
cached = wf.load_cached(syms)
print(f"loaded {len(cached)} symbols")

all_dates = set()
for _, df in cached:
    all_dates.update(str(d) for d in df.index.date)
search_dates, holdout_dates = wf.date_split(all_dates)

comps = {
    "mr_base": wf.MR_CAP,
    "orb_base": wf.ORB_CAP,
    "orb_sector": wf.ORB_SECTOR,
    "orb_base_novol": wf.comp_orb("orb_base_novol", max_price=250.0, use_vol=False),
    "orb_sector_novol": wf.comp_orbsec("orb_sector_novol", use_vol=False),
}
trades = {}
for name, c in comps.items():
    t = wf.generate_component(c, cached)
    trades[name] = t
    print(f"  {name}: {len(t)} total trades cached")


def show(label, keys):
    s_search = wf.score_portfolio(keys, trades, search_dates)
    s_hold = wf.score_portfolio(keys, trades, holdout_dates)
    print(f"{label:46s} SEARCH  {wf.fmt(s_search)}")
    print(f"{'':46s} HOLDOUT {wf.fmt(s_hold)}")


print()
show("orb_base (vol filter ON, baseline)", ["orb_base"])
show("orb_sector (vol filter ON)", ["orb_sector"])
show("orb_base_novol (vol filter OFF)", ["orb_base_novol"])
show("orb_sector_novol (vol filter OFF)", ["orb_sector_novol"])
print()
show("PORTFOLIO: mr_base + orb_base_novol", ["mr_base", "orb_base_novol"])
show("PORTFOLIO: mr_base + orb_sector_novol", ["mr_base", "orb_sector_novol"])
