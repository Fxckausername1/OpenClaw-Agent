#!/usr/bin/env python3
"""Gap strategies — gap-fill (reversion) vs gap-and-go (momentum).

Single-name, R-comparable to ORB / mean-rev. Same data/universe/earnings filter/
$250 cap. On each session with an overnight gap >= GAP_MIN vs the prior close:
  GAP-FILL: fade it — short a gap-up / long a gap-down, target = prior close,
            stop = STOP_PCT beyond the open, EOD exit fallback.
  GAP-GO  : ride it — long a gap-up / short a gap-down, stop = STOP_PCT beyond
            the open, no target, exit at close.
Reports per-trade R for each + correlation to mean reversion.

Run: ./venv/bin/python gap_backtest.py [--symbols ..] [--quiet]
"""
import sys
import json
import argparse
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pyarrow.dataset as pads

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mean_reversion_scanner as mr

ROOT = Path(__file__).resolve().parent
DB_DIR = ROOT / "data" / "databento"
EARN_CACHE = ROOT / "data" / "bt_earnings.json"
MR_TRADES = ROOT / "data" / "deep_trades_rich.csv"
ET = ZoneInfo("America/New_York")
MAX_PRICE = 250.0
GAP_MIN = 0.005     # 0.5% minimum overnight gap
STOP_PCT = 0.005    # stop 0.5% beyond the open


def load_symbol(ds, db_sym):
    t = ds.to_table(filter=(pads.field("symbol") == db_sym),
                    columns=["ts_event", "open", "high", "low", "close", "volume", "symbol"])
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
        Close=("close", "last"), Volume=("volume", "sum")).dropna(subset=["Open"])
    o = o.between_time("09:30", "15:55")
    o.index = o.index.tz_localize(None)
    return o


def sim(side, entry, stop, target, bars):
    risk = abs(entry - stop)
    if risk <= 0:
        return None
    for _, b in bars.iterrows():
        h, l = float(b["High"]), float(b["Low"])
        if side == "LONG":
            if l <= stop:
                return -1.0
            if target is not None and h >= target:
                return (target - entry) / risk
        else:
            if h >= stop:
                return -1.0
            if target is not None and l <= target:
                return (entry - target) / risk
    c = float(bars["Close"].iloc[-1])
    return (c - entry) / risk if side == "LONG" else (entry - c) / risk


def stats(rs):
    if not rs:
        return "       no trades"
    rs = np.asarray(rs, float)
    wins = (rs > 0).sum()
    losses = rs[rs <= 0]
    pf = (rs[rs > 0].sum() / abs(losses.sum())) if len(losses) and losses.sum() != 0 else float("inf")
    pfs = "inf " if pf == float("inf") else f"{pf:4.2f}"
    return (f"{len(rs):>4} tr | win {wins/len(rs)*100:4.1f}% | exp {rs.mean():+.3f}R "
            f"| total {rs.sum():+7.1f}R | PF {pfs}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default=None)
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()

    earn = json.loads(EARN_CACHE.read_text()) if EARN_CACHE.exists() else {}
    ds = pads.dataset([str(p) for p in sorted(DB_DIR.glob("chunk_*.parquet"))])
    syms = mr.fetch_sp100()
    if a.symbols:
        syms = [s.strip().upper() for s in a.symbols.split(",")]

    fill_rows, go_rows = [], []
    for n, sym in enumerate(syms, 1):
        try:
            df = load_symbol(ds, sym.replace("-", "."))
        except Exception as e:
            print(f"[{n}] {sym} err {e}", file=sys.stderr); continue
        if df is None or len(df) < 50:
            continue
        eset = set(earn.get(sym, []))
        prev_close = None
        for day, dd in df.groupby(df.index.date, sort=True):
            day_open = float(dd["Open"].iloc[0])
            day_close = float(dd["Close"].iloc[-1])
            if prev_close is not None and day.isoformat() not in eset and prev_close <= MAX_PRICE:
                gap = day_open / prev_close - 1
                if abs(gap) >= GAP_MIN:
                    up = gap > 0
                    # GAP-FILL (fade toward prev_close)
                    fside = "SHORT" if up else "LONG"
                    fstop = day_open * (1 + STOP_PCT) if up else day_open * (1 - STOP_PCT)
                    rf = sim(fside, day_open, fstop, prev_close, dd)
                    if rf is not None:
                        fill_rows.append((fside, str(day), rf))
                    # GAP-GO (ride the gap)
                    gside = "LONG" if up else "SHORT"
                    gstop = day_open * (1 - STOP_PCT) if up else day_open * (1 + STOP_PCT)
                    rg = sim(gside, day_open, gstop, None, dd)
                    if rg is not None:
                        go_rows.append((gside, str(day), rg))
            prev_close = day_close
        if not a.quiet:
            print(f"[{n}/{len(syms)}] {sym}", file=sys.stderr)
        del df

    mr_daily = None
    if MR_TRADES.exists():
        mr_daily = pd.read_csv(MR_TRADES).groupby("date")["outcome_r"].sum()

    def corr(rows):
        if mr_daily is None or not rows:
            return None
        d = pd.DataFrame(rows, columns=["side", "date", "r"]).groupby("date")["r"].sum()
        days = sorted(set(d.index) | set(mr_daily.index))
        return np.corrcoef(d.reindex(days, fill_value=0), mr_daily.reindex(days, fill_value=0))[0, 1]

    print("=" * 74)
    print(f"GAP STRATEGIES — gap>={GAP_MIN*100:.1f}%, stop {STOP_PCT*100:.1f}% beyond open "
          f"(2yr, earnings-filtered, MAX_PRICE<={int(MAX_PRICE)})")
    print("\nGAP-FILL (fade the gap):")
    print("  ALL :", stats([r[2] for r in fill_rows]))
    print("  LONG:", stats([r[2] for r in fill_rows if r[0] == "LONG"]))
    print("  SHORT:", stats([r[2] for r in fill_rows if r[0] == "SHORT"]))
    c = corr(fill_rows)
    print(f"  corr to mean-rev: {c:+.3f}" if c is not None else "")
    print("\nGAP-GO (ride the gap):")
    print("  ALL :", stats([r[2] for r in go_rows]))
    print("  LONG:", stats([r[2] for r in go_rows if r[0] == "LONG"]))
    print("  SHORT:", stats([r[2] for r in go_rows if r[0] == "SHORT"]))
    c = corr(go_rows)
    print(f"  corr to mean-rev: {c:+.3f}" if c is not None else "")


if __name__ == "__main__":
    main()
