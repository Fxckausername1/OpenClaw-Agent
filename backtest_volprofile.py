#!/usr/bin/env python3
"""backtest_volprofile.py -- first-ever holdout test of gen_volprofile/gen_volprofile_sector
(the MA-engagement/HVN-LVN swing strategy, daily-bar timeframe -- deliberately different from
the intraday mr/orb champion legs). These generators were fully built (comp_vp/comp_vpsec
already exist in walkforward_search.py) but never run through the locked holdout discipline
and never added to continuous_search.py's grid. Two questions:
  (1) standalone: does volprofile/volprofile_sector carry on its own locked holdout?
  (2) portfolio: even if standalone quality is modest, does adding it as a 3rd leg alongside
      the live champion (mr_z1.5 + orb_sector<=0.66%) improve the BLENDED portfolio's holdout
      Sharpe/total-R (diversification value), and what's its correlation to the mr/orb legs?

Same locked 75/25 holdout + concentration/quarterly diagnostics discipline as every other
strategy tested in this codebase (vwap_rev, vol_reversion, close_drift, overnight_gap,
microstructure, gex_regime).
"""
import json
from pathlib import Path
import pandas as pd
import walkforward_search as wf
import mean_reversion_scanner as mr
from _wf_probe_common import run_backtest, fmt, concentration_diagnostics, verdict

ROOT = Path("/home/heff/.openclaw/workspace")

syms = wf.wide_universe.load_universe(rebuild_if_stale=False) or mr.fetch_sp100()
print("universe: {} symbols".format(len(syms)))
cached = wf.load_cached(syms)
print("cached: {}/{} symbols".format(len(cached), len(syms)))

# ---------------------------------------------------------------- (1) STANDALONE
STANDALONE = [
    {"name": "volprofile: base (ma50/200, both signatures)", "comps": [wf.comp_vp("vp_base")]},
    {"name": "volprofile_sector: sector-gated",               "comps": [wf.comp_vpsec("vp_sector")]},
    {"name": "volprofile: bounce-only",                       "comps": [wf.comp_vp("vp_bounce", signature_mode="bounce")]},
    {"name": "volprofile: continuation-only",                 "comps": [wf.comp_vp("vp_cont", signature_mode="continuation")]},
]

run_backtest("volprofile", STANDALONE, cached,
             ROOT / "data" / "volprofile_wf_ledger.csv",
             ROOT / "data" / "volprofile_wf_result.json")

# ---------------------------------------------------------------- (2) PORTFOLIO (3rd leg)
print("\n" + "=" * 92)
print("PORTFOLIO TEST: does volprofile ADD to the live champion (mr_z1.5 + orb_sector<=0.66%)?")

MR_CHAMP = wf.comp_mr("mr_champ", z=1.5, vdev=0.015, min_rr=1.5)
ORB_CHAMP = wf.comp_orbsec("orb_champ", max_range_frac=0.0066, vol_mult=1.5, use_vwap=True, use_vol=True)
VP_BASE = wf.comp_vp("vp_base")  # reuses cache from the standalone pass above

PORT_CANDS = [
    {"name": "baseline: mr+orb (live champion)",       "comps": [MR_CHAMP, ORB_CHAMP]},
    {"name": "mr+orb+volprofile (3rd leg, base)",       "comps": [MR_CHAMP, ORB_CHAMP, VP_BASE]},
]

uniq = {}
for cand in PORT_CANDS:
    for c in cand["comps"]:
        uniq.setdefault(wf.component_key(c), c)
comp_trades = {}
for k, c in uniq.items():
    comp_trades[k] = wf.generate_component(c, cached)

all_dates = []
for t in comp_trades.values():
    all_dates += t["date"].tolist()
search, holdout = wf.date_split(all_dates)
print("date split: {} search days / {} holdout days".format(len(search), len(holdout)))

keys = lambda cand: [wf.component_key(c) for c in cand["comps"]]
results = {}
for cand in PORT_CANDS:
    s = wf.score_portfolio(keys(cand), comp_trades, search)
    h = wf.score_portfolio(keys(cand), comp_trades, holdout)
    results[cand["name"]] = (s, h)

print("\nSEARCH (in-sample):")
for name, (s, h) in results.items():
    print("  {:<38} {}".format(name, fmt(s)))
print("\nHOLDOUT (locked):")
for name, (s, h) in results.items():
    print("  {:<38} {}".format(name, fmt(h)))

base_s, base_h = results["baseline: mr+orb (live champion)"]
comb_s, comb_h = results["mr+orb+volprofile (3rd leg, base)"]
d_search = comb_s["total_r"] - base_s["total_r"]
d_hold = comb_h["total_r"] - base_h["total_r"]
sharpe_delta_hold = comb_h["sharpe"] - base_h["sharpe"]
print("\nPORTFOLIO-ADD VERDICT:")
print("  search:  3rd-leg delta {:+.1f}R".format(d_search))
print("  holdout: 3rd-leg delta {:+.1f}R  | Sharpe {:+.2f} -> {:+.2f} (delta {:+.2f})".format(
    d_hold, base_h["sharpe"], comb_h["sharpe"], sharpe_delta_hold))

# correlation of vp leg's daily net_r series vs the combined mr+orb daily series (holdout window)
vp_t = comp_trades[wf.component_key(VP_BASE)]
mr_t = comp_trades[wf.component_key(MR_CHAMP)]
orb_t = comp_trades[wf.component_key(ORB_CHAMP)]
mrorb = pd.concat([mr_t, orb_t], ignore_index=True)
vp_h = vp_t[vp_t["date"].isin(holdout)]
mrorb_h = mrorb[mrorb["date"].isin(holdout)]
if not vp_h.empty and not mrorb_h.empty:
    a = vp_h.groupby("date")["net_r"].sum()
    b = mrorb_h.groupby("date")["net_r"].sum()
    days = sorted(set(a.index) | set(b.index))
    import numpy as np
    corr = float(np.corrcoef(a.reindex(days, fill_value=0), b.reindex(days, fill_value=0))[0, 1])
    print("  holdout corr(volprofile daily net_r, mr+orb daily net_r) = {:+.3f}".format(corr))
else:
    corr = None
    print("  holdout corr: N/A (one leg has zero holdout trades)")

diag = concentration_diagnostics(VP_BASE, holdout)
v = verdict(results["volprofile: base (ma50/200, both signatures)"][1] if "volprofile: base (ma50/200, both signatures)" in results else comp_trades and None, diag) if False else None
print("\nvolprofile standalone concentration diagnostics (holdout):")
print("  {}".format(json.dumps(diag) if diag else "N/A"))

out = {
    "baseline_search": base_s, "baseline_holdout": base_h,
    "combined_search": comb_s, "combined_holdout": comb_h,
    "search_delta_r": d_search, "holdout_delta_r": d_hold,
    "holdout_sharpe_delta": sharpe_delta_hold,
    "vp_holdout_corr_to_mr_orb": corr,
    "vp_concentration_diag": diag,
}
(ROOT / "data" / "volprofile_portfolio_result.json").write_text(json.dumps(out, indent=2, default=str))
print("\nwrote {}".format(ROOT / "data" / "volprofile_portfolio_result.json"))
