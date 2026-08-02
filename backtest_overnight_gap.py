#!/usr/bin/env python3
"""backtest_overnight_gap.py -- first-ever holdout test of gen_overnight_gap (fade large
open-to-VWAP gaps, early entry 09:35, hold intraday for reversion, exit EOD). Full live
wide_universe, same locked 75/25 holdout + concentration/quarterly diagnostics as every
other strategy here.

NOTE: conceptually overlaps with vol_reversion (both fade opening gaps) -- worth comparing
the two verdicts side by side rather than in isolation once both report back tonight.
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
    {"name": "overnight_gap: thresh0.015 (default)", "comps": [wf.comp_ovn("ovn_default", gap_thresh=0.015)]},
    {"name": "overnight_gap: thresh0.020 (bigger gaps only)", "comps": [wf.comp_ovn("ovn_big", gap_thresh=0.020)]},
    {"name": "overnight_gap: thresh0.010 (more signals)", "comps": [wf.comp_ovn("ovn_loose", gap_thresh=0.010)]},
]

run_backtest("overnight_gap", CANDIDATES, cached,
             ROOT / "data" / "overnight_gap_wf_ledger.csv",
             ROOT / "data" / "overnight_gap_wf_result.json")
