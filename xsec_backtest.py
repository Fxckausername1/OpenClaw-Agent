#!/usr/bin/env python3
"""Cross-sectional reversion — buy yesterday's losers, short yesterday's winners.

Portfolio style: each day rank the 100 names by prior-day return, go long the
bottom DECILE and short the top decile, hold 1 day. No stop (held to next close),
so per-position R is volatility-normalized: next-day return / 20d daily vol.
Reports per-position R, win rate, and correlation to mean reversion.

Run: ./venv/bin/python xsec_backtest.py [--k 10] [--quiet]
"""
import sys
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
MR_TRADES = ROOT / "data" / "deep_trades_rich.csv"
ET = ZoneInfo("America/New_York")
MAX_PRICE = 250.0


def daily_close(ds, db_sym):
    t = ds.to_table(filter=(pads.field("symbol") == db_sym),
                    columns=["ts_event", "close", "symbol"])
    df = t.to_pandas()
    if df.empty:
        return None
    if "ts_event" in df.columns:
        df = df.set_index("ts_event")
    df = df.sort_index()
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    df.index = df.index.tz_convert(ET)
    s = df["close"].resample("1D").last().dropna()
    s.index = s.index.date
    return s


def stats(rs):
    if len(rs) == 0:
        return "no trades"
    rs = np.asarray(rs, float)
    wins = (rs > 0).sum()
    losses = rs[rs <= 0]
    pf = (rs[rs > 0].sum() / abs(losses.sum())) if len(losses) and losses.sum() != 0 else float("inf")
    pfs = "inf " if pf == float("inf") else f"{pf:4.2f}"
    return (f"{len(rs):>5} pos | win {wins/len(rs)*100:4.1f}% | exp {rs.mean():+.3f}R "
            f"| total {rs.sum():+8.1f}R | PF {pfs}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--k", type=int, default=10)
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()

    ds = pads.dataset([str(p) for p in sorted(DB_DIR.glob("chunk_*.parquet"))])
    syms = mr.fetch_sp100()
    closes = {}
    for n, sym in enumerate(syms, 1):
        try:
            s = daily_close(ds, sym.replace("-", "."))
        except Exception as e:
            print(f"[{n}] {sym} err {e}", file=sys.stderr); continue
        if s is not None and len(s) > 40:
            closes[sym] = s
        if not a.quiet:
            print(f"[{n}/{len(syms)}] {sym}", file=sys.stderr)

    px = pd.DataFrame(closes).sort_index()
    ret = px.pct_change()
    vol = ret.rolling(20).std()
    dates = list(px.index)

    long_rs, short_rs = [], []
    daily = {}
    for i in range(21, len(dates) - 1):
        d_prev, d_now = dates[i - 1], dates[i]
        signal = ret.loc[d_now]                  # return into the close of d_now (known at d_now close)
        elig = signal.dropna()
        # only names priced <= MAX_PRICE at d_now
        elig = elig[px.loc[d_now, elig.index] <= MAX_PRICE]
        if len(elig) < 2 * a.k:
            continue
        ranked = elig.sort_values()
        losers = ranked.index[:a.k]              # buy these (long)
        winners = ranked.index[-a.k:]            # short these
        d_next = dates[i + 1]
        fwd = (px.loc[d_next] - px.loc[d_now]) / px.loc[d_now]   # next-day return
        day_r = []
        for s in losers:
            v = vol.loc[d_now, s]
            if v and not np.isnan(v) and not np.isnan(fwd[s]):
                r = fwd[s] / v
                long_rs.append(r); day_r.append(r)
        for s in winners:
            v = vol.loc[d_now, s]
            if v and not np.isnan(v) and not np.isnan(fwd[s]):
                r = -fwd[s] / v
                short_rs.append(r); day_r.append(r)
        if day_r:
            daily[str(d_next)] = float(np.sum(day_r))

    print("=" * 72)
    print(f"CROSS-SECTIONAL REVERSION — long bottom-{a.k} / short top-{a.k} by "
          f"prior-day return, 1-day hold (vol-normalized R)")
    print("  ALL  :", stats(long_rs + short_rs))
    print("  LONG (losers) :", stats(long_rs))
    print("  SHORT (winners):", stats(short_rs))

    if MR_TRADES.exists() and daily:
        mr_daily = pd.read_csv(MR_TRADES).groupby("date")["outcome_r"].sum()
        xd = pd.Series(daily)
        days = sorted(set(xd.index) | set(mr_daily.index))
        corr = np.corrcoef(xd.reindex(days, fill_value=0), mr_daily.reindex(days, fill_value=0))[0, 1]
        print(f"\n  corr to mean-rev: {corr:+.3f}  ({len(days)} days)")


if __name__ == "__main__":
    main()
