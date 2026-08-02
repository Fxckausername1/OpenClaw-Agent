#!/usr/bin/env python3
"""backtest_microstructure.py -- first-ever holdout test of gen_microstructure (fade
single 5-min-bar range extremes back toward session VWAP/midpoint -- the only
bar-level, non-daily-setup generator in the registry, likely much higher trade count
than the others). Full live wide_universe, same locked 75/25 holdout +
concentration/quarterly diagnostics as every other strategy here.
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
    {"name": "microstructure: spike2.5 (default)", "comps": [wf.comp_micro("micro_default", vol_spike_thresh=2.5)]},
    {"name": "microstructure: spike3.5 (bigger spikes only)", "comps": [wf.comp_micro("micro_big", vol_spike_thresh=3.5)]},
    {"name": "microstructure: spike2.0 (more signals)", "comps": [wf.comp_micro("micro_loose", vol_spike_thresh=2.0)]},
]

run_backtest("microstructure", CANDIDATES, cached,
             ROOT / "data" / "microstructure_wf_ledger.csv",
             ROOT / "data" / "microstructure_wf_result.json")
