#!/usr/bin/env python3
"""backtest_champion_filters.py -- 2026-07-08 research (thread 3): does adding a
VWAP-deviation gate to the live champion ORB leg, and/or an ATR/volatility-band gate to the
live champion MR leg, improve holdout results -- the SAME pattern as how tight-range and
sector-gate were added as FILTERS on top of ORB, not as standalone systems. Uses the new
gen_orb_vwapdev / gen_mr_volband generators (additive, non-breaking additions to
walkforward_search.py, mirroring gen_orb_sector / gen_mean_rev_regime's structure).

Live champion (verified against data/live_params.json 2026-07-08):
  MR:  z=1.5, vdev=0.015, min_rr=1.5   (comp_mr, no sector/regime/vix/riskoff gate active)
  ORB: vol_mult=1.5, max_range_frac=0.0066, use_vwap=True, use_vol=True, use_sector_gate=True
       (comp_orbsec)

Each new gate's "gate off" default setting exactly reproduces the live champion leg (sanity
row), so every comparison is baseline-vs-baseline-plus-one-new-filter, isolating the new
gate's effect precisely, same discipline as the historical tight-range/sector-gate tests.
"""
import json
from pathlib import Path
import pandas as pd
import numpy as np
import walkforward_search as wf
import mean_reversion_scanner as mr
from _wf_probe_common import fmt

ROOT = Path("/home/heff/.openclaw/workspace")

syms = wf.wide_universe.load_universe(rebuild_if_stale=False) or mr.fetch_sp100()
print("universe: {} symbols".format(len(syms)))
cached = wf.load_cached(syms)
print("cached: {}/{} symbols".format(len(cached), len(syms)))

# ---- champion legs (fixed) ----
MR_CHAMP = wf.comp_mr("mr_champ", z=1.5, vdev=0.015, min_rr=1.5)
ORB_CHAMP = wf.comp_orbsec("orb_champ", max_range_frac=0.0066, vol_mult=1.5, use_vwap=True, use_vol=True)

# ---- ORB + VWAP-deviation gate variants (on top of the FULL champion ORB filter stack) ----
ORB_VWAPDEV_OFF = wf.comp_orb_vwapdev("orb_vwapdev_off (== champion sanity check)",
                                       min_vwapdev_frac=None, sector_gate=True)
ORB_VWAPDEV_002 = wf.comp_orb_vwapdev("orb_vwapdev>=0.2%", min_vwapdev_frac=0.002, sector_gate=True)
ORB_VWAPDEV_004 = wf.comp_orb_vwapdev("orb_vwapdev>=0.4%", min_vwapdev_frac=0.004, sector_gate=True)
ORB_VWAPDEV_008 = wf.comp_orb_vwapdev("orb_vwapdev>=0.8%", min_vwapdev_frac=0.008, sector_gate=True)

# ---- MR + volatility-band gate variants (relvol = std20/sma20 vs its own 78-bar trailing avg) ----
MR_VOLBAND_OFF   = wf.comp_mr_volband("mr_volband_off (== champion sanity check)",
                                       vol_band_lo=0.0, vol_band_hi=float("inf"))
MR_VOLBAND_MID   = wf.comp_mr_volband("mr_volband[0.7,1.4]", vol_band_lo=0.7, vol_band_hi=1.4)
MR_VOLBAND_WIDE  = wf.comp_mr_volband("mr_volband[0.5,1.8]", vol_band_lo=0.5, vol_band_hi=1.8)
MR_VOLBAND_TIGHT = wf.comp_mr_volband("mr_volband[0.85,1.2]", vol_band_lo=0.85, vol_band_hi=1.2)

PORTFOLIOS = [
    {"name": "BASELINE (mr_z1.5 + orb_sector<=0.66%)", "comps": [MR_CHAMP, ORB_CHAMP]},
    # --- ORB VWAP-dev gate legs (MR leg held at champion) ---
    {"name": "orb+vwapdev_off leg (sanity==baseline)",  "comps": [MR_CHAMP, ORB_VWAPDEV_OFF]},
    {"name": "orb+vwapdev>=0.2%",                        "comps": [MR_CHAMP, ORB_VWAPDEV_002]},
    {"name": "orb+vwapdev>=0.4%",                        "comps": [MR_CHAMP, ORB_VWAPDEV_004]},
    {"name": "orb+vwapdev>=0.8%",                        "comps": [MR_CHAMP, ORB_VWAPDEV_008]},
    # --- MR vol-band gate legs (ORB leg held at champion) ---
    {"name": "mr+volband_off leg (sanity==baseline)",    "comps": [MR_VOLBAND_OFF, ORB_CHAMP]},
    {"name": "mr+volband[0.7,1.4]",                      "comps": [MR_VOLBAND_MID, ORB_CHAMP]},
    {"name": "mr+volband[0.5,1.8]",                      "comps": [MR_VOLBAND_WIDE, ORB_CHAMP]},
    {"name": "mr+volband[0.85,1.2]",                     "comps": [MR_VOLBAND_TIGHT, ORB_CHAMP]},
    # --- both gates stacked (best-looking threshold from each, picked after search read) ---
]

uniq = {}
for cand in PORTFOLIOS:
    for c in cand["comps"]:
        uniq.setdefault(wf.component_key(c), c)

comp_trades = {}
import time
t0 = time.time()
for i, (k, c) in enumerate(uniq.items(), 1):
    t = wf.generate_component(c, cached)
    comp_trades[k] = t
    n = len(t)
    tot = float(t["net_r"].sum()) if n else 0.0
    per = tot / n if n else 0.0
    print("  [{}/{}] {:<40} {:>5} tr | net {:+8.1f}R | {:+.3f}R/tr ({:.0f}s)".format(
        i, len(uniq), c["name"], n, tot, per, time.time() - t0))

all_dates = []
for t in comp_trades.values():
    all_dates += t["date"].tolist()
search, holdout = wf.date_split(all_dates)
print("\ndate split: {} search days / {} locked holdout days ({:.0%}/{:.0%})".format(
    len(search), len(holdout), wf.SEARCH_FRAC, 1 - wf.SEARCH_FRAC))

keys = lambda cand: [wf.component_key(c) for c in cand["comps"]]
results = {}
for cand in PORTFOLIOS:
    s = wf.score_portfolio(keys(cand), comp_trades, search)
    h = wf.score_portfolio(keys(cand), comp_trades, holdout)
    results[cand["name"]] = (s, h)

print("\n" + "=" * 100)
print("SEARCH region (in-sample -- orientation only, NOT the verdict):")
for name, (s, h) in results.items():
    print("  {:<42} {}".format(name, fmt(s)))

print("\nLOCKED HOLDOUT (untouched until now):")
for name, (s, h) in results.items():
    print("  {:<42} {}".format(name, fmt(h)))

base_name = "BASELINE (mr_z1.5 + orb_sector<=0.66%)"
base_s, base_h = results[base_name]

print("\n" + "=" * 100)
print("VERDICT PER VARIANT (vs baseline, both regions):")
led = []
for cand in PORTFOLIOS:
    name = cand["name"]
    s, h = results[name]
    d_search = s["total_r"] - base_s["total_r"]
    d_hold = h["total_r"] - base_h["total_r"]
    per_delta_search = s["per_trade"] - base_s["per_trade"]
    per_delta_hold = h["per_trade"] - base_h["per_trade"]
    sharpe_delta_hold = h["sharpe"] - base_h["sharpe"]
    if name == base_name:
        verdict = "-- baseline --"
    elif "sanity" in name:
        verdict = "SANITY CHECK (gate off, should == baseline)"
    else:
        # carries only if BOTH per-trade R and Sharpe improve on holdout vs baseline,
        # same "carries both metrics" discipline as every other filter test in this project
        carries = (per_delta_hold > 0) and (sharpe_delta_hold > 0)
        verdict = "CARRIES (per-trade + Sharpe both improve on holdout)" if carries else \
                  "NO-IMPROVE / MIRAGE (holdout per-trade or Sharpe did not both improve)"
    print("  {:<42} search per_tr {:+.4f} (d{:+.4f}) | holdout per_tr {:+.4f} (d{:+.4f}) "
          "Sharpe d{:+.2f} n={} -> {}".format(
              name, s["per_trade"], per_delta_search, h["per_trade"], per_delta_hold,
              sharpe_delta_hold, h["n"], verdict))
    led.append(dict(name=name, search_total_r=s["total_r"], search_n=s["n"],
                     search_per_trade=s["per_trade"], search_sharpe=s["sharpe"],
                     holdout_total_r=h["total_r"], holdout_n=h["n"],
                     holdout_per_trade=h["per_trade"], holdout_sharpe=h["sharpe"],
                     holdout_per_trade_delta=per_delta_hold, holdout_sharpe_delta=sharpe_delta_hold,
                     verdict=verdict))

pd.DataFrame(led).to_csv(ROOT / "data" / "champion_filters_wf_ledger.csv", index=False)
(ROOT / "data" / "champion_filters_wf_result.json").write_text(json.dumps(led, indent=2, default=str))
print("\nwrote {}\nwrote {}".format(ROOT / "data" / "champion_filters_wf_ledger.csv",
                                     ROOT / "data" / "champion_filters_wf_result.json"))
