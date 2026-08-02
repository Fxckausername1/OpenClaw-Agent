#!/usr/bin/env python3
"""backtest_vol_reversion.py -- first-ever holdout test of gen_vol_reversion (fade large
prev-close-to-open gaps vs intraday sigma, entry 09:45 after algos absorb the open, exit
EOD). Full live wide_universe, same locked 75/25 holdout + concentration/quarterly
diagnostics as every other strategy here.
"""
from pathlib import Path
import walkforward_search as wf
import mean_reversion_scanner as mr
from _wf_probe_common import run_backtest

ROOT = Path("/home/heff/.openclaw/workspace")

syms = wf.wide_universe.load_universe(rebuild_if_stale=False) or mr.fetch_sp100()
print("universe: {} symbols".format(len(syms)))
cached = wf.load_cached(syms)
print("cached: {}/{} symbols".format(len(cached), len(syms)))

CANDIDATES = [
    {"name": "vol_reversion: thresh2.0 (default)", "comps": [wf.comp_volr("volr_default", vol_thresh=2.0)]},
    {"name": "vol_reversion: thresh2.5 (bigger gaps only)", "comps": [wf.comp_volr("volr_big", vol_thresh=2.5)]},
    {"name": "vol_reversion: thresh1.5 (more signals)", "comps": [wf.comp_volr("volr_loose", vol_thresh=1.5)]},
]

run_backtest("vol_reversion", CANDIDATES, cached,
             ROOT / "data" / "vol_reversion_wf_ledger.csv",
             ROOT / "data" / "vol_reversion_wf_result.json")
