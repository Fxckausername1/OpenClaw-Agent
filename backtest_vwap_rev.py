#!/usr/bin/env python3
"""backtest_vwap_rev.py -- first-ever holdout test of gen_vwap_rev (simple VWAP-deviation
fade, no Bollinger/break double-confirm -- a looser, lower-confirmation cousin of the live
mean-rev champion, aimed at catching different trade days). Uses the full live wide_universe,
same locked 75/25 holdout + concentration/quarterly diagnostics as every other strategy here.
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
    {"name": "vwap_rev: vdev0.015 rr1.0 (default)", "comps": [wf.comp_vwr("vwr_default", vdev=0.015, min_rr=1.0)]},
    {"name": "vwap_rev: vdev0.010 rr1.0 (looser)",   "comps": [wf.comp_vwr("vwr_loose", vdev=0.010, min_rr=1.0)]},
    {"name": "vwap_rev: vdev0.020 rr1.5 (tighter)",  "comps": [wf.comp_vwr("vwr_tight", vdev=0.020, min_rr=1.5)]},
]

run_backtest("vwap_rev", CANDIDATES, cached,
             ROOT / "data" / "vwap_rev_wf_ledger.csv",
             ROOT / "data" / "vwap_rev_wf_result.json")
