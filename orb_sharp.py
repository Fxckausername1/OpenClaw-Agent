#!/usr/bin/env python3
"""Sharpened ORB — test VWAP + volume filters in one pass.

Raw ORB was +0.05R / PF 1.12 (too thin). The failed breaks that immediately
reverse are exactly what mean reversion fades — so filtering ORB to trend-aligned,
volume-confirmed breaks should cut those and lift the edge. Tests 4 configs:
  baseline | +VWAP (break-bar close on the trend side of VWAP) |
  +VOL (break-bar relative volume >= threshold) | +both
Same data/universe/filters; reports each config's R stats + correlation to mean rev.

Run: ./venv/bin/python orb_sharp.py [--symbols ..] [--quiet]
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
OR_END = dtime(9, 45)        # 15-min opening range
VOL_MULT = 1.5               # breakout-bar relative-volume threshold


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


def orb_day(dd):
    orb = dd[dd.index.time < OR_END]
    post = dd[dd.index.time >= OR_END]
    if len(orb) < 1 or len(post) < 2:
        return None
    orh = float(orb["High"].max()); orl = float(orb["Low"].min())
    rng = orh - orl
    if rng <= 0 or orh > MAX_PRICE:
        return None

    typ = (dd["High"] + dd["Low"] + dd["Close"]) / 3.0
    vwap = (typ * dd["Volume"]).cumsum() / dd["Volume"].cumsum()
    avgvol = dd["Volume"].expanding().mean()

    p = post.reset_index()
    tscol = p.columns[0]
    for j in range(len(p)):
        b = p.iloc[j]
        up = b["High"] >= orh
        dn = b["Low"] <= orl
        if not (up or dn):
            continue
        if up and dn:
            side = "LONG" if b["Close"] >= b["Open"] else "SHORT"
        else:
            side = "LONG" if up else "SHORT"
        entry = orh if side == "LONG" else orl
        stop = orl if side == "LONG" else orh
        risk = rng
        ts = b[tscol]
        vw = float(vwap.loc[ts]); av = float(avgvol.loc[ts])
        relvol = b["Volume"] / av if av > 0 else 0
        take_vwap = (b["Close"] > vw) if side == "LONG" else (b["Close"] < vw)
        take_vol = relvol >= VOL_MULT
        # outcome: stop or EOD close
        rest = p.iloc[j:]
        r = None
        for _, bb in rest.iterrows():
            if side == "LONG" and bb["Low"] <= stop:
                r = -1.0; break
            if side == "SHORT" and bb["High"] >= stop:
                r = -1.0; break
        if r is None:
            close = float(p["Close"].iloc[-1])
            r = (close - entry) / risk if side == "LONG" else (entry - close) / risk
        return (side, r, bool(take_vwap), bool(take_vol))
    return None


def stats(rs):
    if not rs:
        return "       no trades"
    rs = np.asarray(rs, float)
    wins = (rs > 0).sum()
    losses = rs[rs <= 0]
    pf = (rs[rs > 0].sum() / abs(losses.sum())) if len(losses) and losses.sum() != 0 else float("inf")
    pfs = "inf " if pf == float("inf") else f"{pf:4.2f}"
    return (f"{len(rs):>5} tr | win {wins/len(rs)*100:4.1f}% | exp {rs.mean():+.3f}R "
            f"| total {rs.sum():+8.1f}R | PF {pfs}")


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

    rows = []  # (date, side, r, take_vwap, take_vol)
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
            res = orb_day(dd)
            if res:
                rows.append((str(day), res[0], res[1], res[2], res[3]))
        if not a.quiet:
            print(f"[{n}/{len(syms)}] {sym}", file=sys.stderr)
        del df

    df = pd.DataFrame(rows, columns=["date", "side", "r", "vwap", "vol"])
    df.to_csv(ROOT / "data" / "orb_sharp_trades.csv", index=False)
    configs = {
        "baseline": df,
        "+VWAP filter": df[df["vwap"]],
        "+VOL filter": df[df["vol"]],
        "+VWAP +VOL": df[df["vwap"] & df["vol"]],
    }

    mr_daily = None
    if MR_TRADES.exists():
        m = pd.read_csv(MR_TRADES)
        mr_daily = m.groupby("date")["outcome_r"].sum()

    print("=" * 76)
    print("SHARPENED ORB — filter comparison (2yr, earnings-filtered, MAX_PRICE<=250)")
    for name, sub in configs.items():
        print(f"\n{name}")
        print("  ALL :", stats(sub["r"].tolist()))
        print("  LONG:", stats(sub[sub.side == 'LONG']["r"].tolist()))
        print("  SHORT:", stats(sub[sub.side == 'SHORT']["r"].tolist()))
        if mr_daily is not None and len(sub):
            od = sub.groupby("date")["r"].sum()
            days = sorted(set(od.index) | set(mr_daily.index))
            corr = np.corrcoef(od.reindex(days, fill_value=0), mr_daily.reindex(days, fill_value=0))[0, 1]
            print(f"  corr to mean-rev: {corr:+.3f}")


if __name__ == "__main__":
    main()
