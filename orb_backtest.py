#!/usr/bin/env python3
"""Opening Range Breakout (ORB) backtest — momentum, contrast to mean reversion.

On the same 2yr Databento 5-min data / same universe / same earnings filter /
same $250 cap, so it's R-comparable to the mean-rev strategy.

Logic per symbol per day:
  - Opening range = high/low of the first OR_MINUTES (default 15m).
  - First break of OR-high -> LONG at OR-high; first break of OR-low -> SHORT.
  - Stop = opposite end of the opening range (risk = range).
  - No profit target — ride the trend, exit at the close (or stop). Classic
    momentum shape: capped -1R losers, uncapped winners.
  - One trade per symbol per day.

Also computes the day-by-day return CORRELATION to mean reversion (from
data/deep_trades_rich.csv) — the number that decides if it's worth combining.

Run: ./venv/bin/python orb_backtest.py [--symbols ..] [--or-min 15] [--quiet]
"""
import sys
import json
import argparse
from datetime import time as dtime
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


def orb_day(day_df, or_end):
    orb = day_df[day_df.index.time < or_end]
    post = day_df[day_df.index.time >= or_end]
    if len(orb) < 1 or len(post) < 2:
        return None
    orh = float(orb["High"].max()); orl = float(orb["Low"].min())
    rng = orh - orl
    if rng <= 0 or orh > MAX_PRICE:
        return None

    entered = None
    bars = post.reset_index(drop=True)
    for j in range(len(bars)):
        b = bars.iloc[j]
        if entered is None:
            up = b["High"] >= orh
            dn = b["Low"] <= orl
            if up and not dn:
                entered = ("LONG", orh, orl, j)
            elif dn and not up:
                entered = ("SHORT", orl, orh, j)
            elif up and dn:
                entered = ("LONG", orh, orl, j) if b["Close"] >= b["Open"] else ("SHORT", orl, orh, j)
            if entered is None:
                continue
            side, entry, stop, ej = entered
            risk = abs(entry - stop)
            continue
        # already entered: check stop / hold
        if side == "LONG":
            if b["Low"] <= stop:
                return (side, -1.0)
        else:
            if b["High"] >= stop:
                return (side, -1.0)
    if entered is None:
        return None
    # also check the entry bar onward already done above except entry bar's own stop
    side, entry, stop, ej = entered
    risk = abs(entry - stop)
    # exit at close of last bar
    close = float(bars["Close"].iloc[-1])
    r = (close - entry) / risk if side == "LONG" else (entry - close) / risk
    return (side, r)


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
    ap.add_argument("--or-min", type=int, default=15)
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()
    or_end = dtime(9, 30 + a.or_min) if a.or_min < 30 else dtime(10, a.or_min - 30)

    earn = json.loads(EARN_CACHE.read_text()) if EARN_CACHE.exists() else {}
    chunks = sorted(DB_DIR.glob("chunk_*.parquet"))
    ds = pads.dataset([str(p) for p in chunks])
    syms = mr.fetch_sp100()
    if a.symbols:
        syms = [s.strip().upper() for s in a.symbols.split(",")]

    trades = []
    for n, sym in enumerate(syms, 1):
        try:
            df = load_symbol(ds, sym.replace("-", "."))
        except Exception as e:
            print(f"[{n}] {sym} err {e}", file=sys.stderr); continue
        if df is None or len(df) < 50:
            continue
        eset = set(earn.get(sym, []))
        for day, dd in df.groupby(df.index.date, sort=True):
            if day.isoformat() in eset:
                continue
            res = orb_day(dd, or_end)
            if res:
                trades.append((res[0], sym, str(day), res[1]))
        if not a.quiet:
            print(f"[{n}/{len(syms)}] {sym}", file=sys.stderr)
        del df

    print("=" * 72)
    print(f"OPENING RANGE BREAKOUT — {a.or_min}m range, stop=opposite end, EOD exit")
    print(f"(2yr Databento, earnings-filtered, MAX_PRICE<={int(MAX_PRICE)})")
    print("ALL  :", stats([t[3] for t in trades]))
    print("LONG :", stats([t[3] for t in trades if t[0] == "LONG"]))
    print("SHORT:", stats([t[3] for t in trades if t[0] == "SHORT"]))

    # correlation to mean reversion (daily summed R)
    if MR_TRADES.exists() and trades:
        orb_daily = pd.DataFrame(trades, columns=["side", "sym", "date", "r"]).groupby("date")["r"].sum()
        mr_df = pd.read_csv(MR_TRADES)
        mr_daily = mr_df.groupby("date")["outcome_r"].sum()
        alldays = sorted(set(orb_daily.index) | set(mr_daily.index))
        o = orb_daily.reindex(alldays, fill_value=0.0)
        m = mr_daily.reindex(alldays, fill_value=0.0)
        corr = np.corrcoef(o.values, m.values)[0, 1]
        print(f"\nDaily-return correlation to mean reversion: {corr:+.3f}  "
              f"({len(alldays)} trading days)")
        print("  (near 0 or negative = great to COMBINE; near +1 = redundant)")
    out = ROOT / "data" / "orb_trades.csv"
    pd.DataFrame(trades, columns=["side", "symbol", "date", "outcome_r"]).to_csv(out, index=False)
    print(f"trades -> {out}")


if __name__ == "__main__":
    main()
