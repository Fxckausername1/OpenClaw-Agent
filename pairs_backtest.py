#!/usr/bin/env python3
"""Pairs / stat-arb — market-neutral spread reversion on correlated twins.

For each pair, spread = log(A) - log(B), z-scored over a 20-day window. Enter when
|z| >= 2 (short the rich leg, long the cheap leg), exit when |z| <= 0.5 (reverted),
stop if |z| >= 3.5 (diverged). Multi-day hold. R measured in spread-sigma:
risk = 1.5 sigma (2->3.5), so revert-to-0.5 = +1.0R, stop = -1.0R.
Reports per-trade R + correlation to mean reversion.

Run: ./venv/bin/python pairs_backtest.py [--quiet]
"""
import sys
import argparse
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pyarrow.dataset as pads

sys.path.insert(0, str(Path(__file__).resolve().parent))

ROOT = Path(__file__).resolve().parent
DB_DIR = ROOT / "data" / "databento"
MR_TRADES = ROOT / "data" / "deep_trades_rich.csv"
ET = ZoneInfo("America/New_York")

PAIRS = [("GOOGL", "GOOG"), ("V", "MA"), ("XOM", "CVX"), ("MRK", "PFE"),
         ("T", "VZ"), ("COST", "WMT"), ("AMGN", "GILD"), ("LMT", "RTX"),
         ("MS", "GS"), ("HON", "GE"), ("ABBV", "BMY"), ("TXN", "ADI"),
         ("QCOM", "AMAT"), ("MU", "LRCX"), ("CRM", "NOW"), ("ISRG", "SYK"),
         ("MAR", "HLT"), ("BKNG", "MAR")]
WIN = 20
Z_ENTRY, Z_EXIT, Z_STOP = 2.0, 0.5, 3.5


def daily_close(ds, sym):
    t = ds.to_table(filter=(pads.field("symbol") == sym.replace("-", ".")),
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
    return (f"{len(rs):>4} tr | win {wins/len(rs)*100:4.1f}% | exp {rs.mean():+.3f}R "
            f"| total {rs.sum():+7.1f}R | PF {pfs}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()

    ds = pads.dataset([str(p) for p in sorted(DB_DIR.glob("chunk_*.parquet"))])
    need = sorted({s for p in PAIRS for s in p})
    px = {}
    for sym in need:
        try:
            s = daily_close(ds, sym)
        except Exception as e:
            print(f"{sym} err {e}", file=sys.stderr); s = None
        if s is not None:
            px[sym] = s
        if not a.quiet:
            print(f"loaded {sym}", file=sys.stderr)

    trades = []   # (pair, exit_date, R)
    for A, B in PAIRS:
        if A not in px or B not in px:
            continue
        df = pd.concat({A: px[A], B: px[B]}, axis=1).dropna()
        if len(df) < WIN + 30:
            continue
        spread = np.log(df[A]) - np.log(df[B])
        z = (spread - spread.rolling(WIN).mean()) / spread.rolling(WIN).std()
        pos = 0       # +1 long spread (z<-2), -1 short spread (z>2)
        z_in = 0.0
        for i in range(WIN, len(z)):
            zi = z.iloc[i]
            if np.isnan(zi):
                continue
            if pos == 0:
                if zi >= Z_ENTRY:
                    pos, z_in = -1, zi
                elif zi <= -Z_ENTRY:
                    pos, z_in = 1, zi
            else:
                hit_stop = abs(zi) >= Z_STOP and (zi * -pos) > 0  # diverged further
                reverted = abs(zi) <= Z_EXIT
                if hit_stop:
                    trades.append((f"{A}/{B}", str(df.index[i]), -1.0)); pos = 0
                elif reverted or (pos == 1 and zi >= 0) or (pos == -1 and zi <= 0):
                    captured = abs(z_in) - abs(zi)
                    trades.append((f"{A}/{B}", str(df.index[i]), captured / (Z_STOP - Z_ENTRY))); pos = 0

    print("=" * 72)
    print(f"PAIRS / STAT-ARB — {len(PAIRS)} pairs, log-spread z-score "
          f"(enter |z|>={Z_ENTRY}, exit |z|<={Z_EXIT}, stop |z|>={Z_STOP}), 20d window")
    print("  ALL :", stats([t[2] for t in trades]))

    if MR_TRADES.exists() and trades:
        mr_daily = pd.read_csv(MR_TRADES).groupby("date")["outcome_r"].sum()
        pd_daily = pd.DataFrame(trades, columns=["pair", "date", "r"]).groupby("date")["r"].sum()
        days = sorted(set(pd_daily.index) | set(mr_daily.index))
        corr = np.corrcoef(pd_daily.reindex(days, fill_value=0), mr_daily.reindex(days, fill_value=0))[0, 1]
        print(f"  corr to mean-rev: {corr:+.3f}")
    # per-pair breakdown
    print("\n  per-pair:")
    dfp = pd.DataFrame(trades, columns=["pair", "date", "r"])
    for pair, sub in dfp.groupby("pair"):
        print(f"    {pair:<14} {stats(sub['r'].tolist())}")


if __name__ == "__main__":
    main()
