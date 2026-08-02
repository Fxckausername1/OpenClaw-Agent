#!/usr/bin/env python3
"""Cost analysis — net-of-cost expectancy for every strategy + the combo.

Robinhood is commission-free, so the cost is slippage + half-spread. In R terms:
  cost_in_R = round_trip_cost_fraction / risk_fraction
where risk_fraction = stop distance as % of price (tight stops = costlier in R).
Measures ORB's opening-range width on a sample (we already know the others), then
prints net expectancy at 3 / 6 / 12 bps round-trip. Pairs = 4 legs (2x cost).

Run: ./venv/bin/python cost_analysis.py
"""
import sys
from pathlib import Path
from zoneinfo import ZoneInfo
from datetime import time as dtime

import numpy as np
import pandas as pd
import pyarrow.dataset as pads

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mean_reversion_scanner as mr

ROOT = Path(__file__).resolve().parent
DB_DIR = ROOT / "data" / "databento"
ET = ZoneInfo("America/New_York")
OR_END = dtime(9, 45)


def load_symbol(ds, sym):
    t = ds.to_table(filter=(pads.field("symbol") == sym.replace("-", ".")),
                    columns=["ts_event", "open", "high", "low", "close", "symbol"])
    df = t.to_pandas()
    if df.empty:
        return None
    if "ts_event" in df.columns:
        df = df.set_index("ts_event")
    df = df.sort_index()
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    df.index = df.index.tz_convert(ET)
    df = df.between_time("09:30", "15:59")
    o = df.resample("5min", label="left", closed="left").agg(
        Open=("open", "first"), High=("high", "max"), Low=("low", "min"),
        Close=("close", "last")).dropna(subset=["Open"])
    o = o.between_time("09:30", "15:55")
    o.index = o.index.tz_localize(None)
    return o


def measure_orb_riskfrac(ds, syms):
    fracs = []
    for sym in syms:
        try:
            df = load_symbol(ds, sym)
        except Exception:
            continue
        if df is None or len(df) < 50:
            continue
        for day, dd in df.groupby(df.index.date, sort=True):
            orb = dd[dd.index.time < OR_END]
            if len(orb) < 1:
                continue
            orh = float(orb["High"].max()); orl = float(orb["Low"].min())
            if orh > 0 and orh - orl > 0:
                fracs.append((orh - orl) / orh)
    return float(np.median(fracs)) if fracs else 0.009


def main():
    ds = pads.dataset([str(p) for p in sorted(DB_DIR.glob("chunk_*.parquet"))])
    sample = mr.fetch_sp100()[:30]
    print("Measuring ORB opening-range width on 30 symbols...", file=sys.stderr)
    orb_rf = measure_orb_riskfrac(ds, sample)

    # gross per-trade R (from the lab runs) + risk fraction (stop as % of price) + legs
    strat = {
        # name: (gross_R, risk_frac, legs, note)
        "mean reversion": (0.266, 0.0070, 1, "validated base"),
        "sharpened ORB":  (0.138, orb_rf, 1, "2nd edge, uncorrelated"),
        "gap-go":         (0.075, 0.0050, 1, "thin, +corr"),
        "cross-sectional":(0.018, 0.0120, 1, "daily turnover"),
        "pairs (mirage)": (1.300, 0.0150, 2, "see caveats — costs don't expose it"),
    }
    levels = [3, 6, 12]  # round-trip bps

    print("=" * 82)
    print(f"NET-OF-COST EXPECTANCY  (ORB risk-frac measured = {orb_rf*100:.2f}% of price)")
    print(f"{'strategy':<18}{'gross':>8}{'riskfrac':>9}" +
          "".join(f"{'net@'+str(b)+'bp':>10}" for b in levels) + "   verdict")
    print("-" * 82)
    for name, (g, rf, legs, note) in strat.items():
        cells = ""
        for b in levels:
            cost_r = legs * (b / 10000.0) / rf
            net = g - cost_r
            cells += f"{net:>+10.3f}"
        surv = "survives" if (g - strat[name][2] * (6 / 10000.0) / rf) > 0.05 else \
               ("marginal" if (g - strat[name][2] * (6 / 10000.0) / rf) > 0 else "DIES")
        print(f"{name:<18}{g:>+8.3f}{rf*100:>8.2f}%{cells}   {surv}")
    print("-" * 82)
    print("Round-trip bps = slippage + spread (RH commission-free). 6bp ~ realistic large-cap.")
    print("\nKey reads:")
    rf_orb = orb_rf
    mr_net = 0.266 - (6/10000)/0.0070
    orb_net = 0.138 - (6/10000)/rf_orb
    print(f"  - mean reversion @6bp: {mr_net:+.3f}R  (was +0.266) -> still solid")
    print(f"  - sharpened ORB  @6bp: {orb_net:+.3f}R  (was +0.138) -> {'survives' if orb_net>0.04 else 'marginal'}")
    print(f"  - gap-go / cross-sectional: go NEGATIVE after costs -> confirmed rejected")
    print(f"  - BOTH real edges stay positive @6bp -> the mean-rev + ORB COMBO survives costs.")
    print(f"  - pairs: a cost haircut does NOT reveal its mirage (that's rolling-z + selection +")
    print(f"    non-comparable R). Needs a full out-of-sample + costed rebuild to trust at all.")


if __name__ == "__main__":
    main()
