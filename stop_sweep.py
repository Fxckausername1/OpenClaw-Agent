#!/usr/bin/env python3
"""Stop-width sweep on the 2yr Databento data.

79% of the strategy's lost R comes from hard stops, and the losers stop out fast
(1-3 bars) — suggesting the 0.15% stop may be too tight and getting wicked out of
trades that then revert. This re-runs the EXACT strategy but, at each trigger,
evaluates several stop widths in one pass: fixed % beyond the extreme and
ATR-based. Per width it recomputes risk, re-applies the R:R>=1.5 gate, and
re-simulates to EOD. Reports a leaderboard.

Earnings dates are cached to data/bt_earnings.json (built once, then instant).

Run: ./venv/bin/python stop_sweep.py [--symbols AAPL,NKE] [--quiet]
"""
import sys
import json
import argparse
from datetime import timedelta
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
ET = ZoneInfo("America/New_York")

# label, kind ('pct'|'atr'), k
STOP_CONFIGS = [
    ("0.15% (current)", "pct", 0.0015),
    ("0.30%", "pct", 0.003),
    ("0.50%", "pct", 0.005),
    ("0.75%", "pct", 0.0075),
    ("1.00%", "pct", 0.01),
    ("ATR x0.5", "atr", 0.5),
    ("ATR x1.0", "atr", 1.0),
    ("ATR x1.5", "atr", 1.5),
]


def rsi(s, p=14):
    d = s.diff(); g = d.clip(lower=0); l = -d.clip(upper=0)
    ag = g.rolling(p).mean(); al = l.rolling(p).mean()
    out = 100 - (100 / (1 + ag / al))
    return out.where(al != 0, 100.0)


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


def prep(df):
    c = df["Close"]; df = df.copy()
    df["sma20"] = c.rolling(20).mean(); df["std20"] = c.rolling(20).std()
    df["rsi"] = rsi(c, 14)
    df["upper"] = df["sma20"] + mr.Z_THRESH * df["std20"]
    df["lower"] = df["sma20"] - mr.Z_THRESH * df["std20"]
    df["z"] = (c - df["sma20"]) / df["std20"]; df["date"] = df.index.date
    pc = c.shift(1)
    tr = pd.concat([df["High"] - df["Low"], (df["High"] - pc).abs(), (df["Low"] - pc).abs()], axis=1).max(axis=1)
    df["atr"] = tr.rolling(14).mean()
    typ = (df["High"] + df["Low"] + df["Close"]) / 3.0
    df["vwap"] = (typ * df["Volume"]).groupby(df["date"]).cumsum() / df["Volume"].groupby(df["date"]).cumsum()
    df["vwap_dev"] = (c - df["vwap"]) / df["vwap"]
    return df


def earnings_for(symbols, quiet):
    cache = {}
    if EARN_CACHE.exists():
        try:
            cache = json.loads(EARN_CACHE.read_text())
        except Exception:
            cache = {}
    changed = False
    for s in symbols:
        if s in cache:
            continue
        changed = True
        dates = []
        try:
            import yfinance as yf
            ed = yf.Ticker(s).get_earnings_dates(limit=24)
            if ed is not None and not ed.empty:
                for ts in ed.index:
                    d = ts.date()
                    for off in (-1, 0, 1):
                        dates.append((d + timedelta(days=off)).isoformat())
        except Exception:
            pass
        cache[s] = sorted(set(dates))
        if not quiet:
            print(f"  earnings {s}: {len(cache[s])} blackout days", file=sys.stderr)
    if changed:
        EARN_CACHE.write_text(json.dumps(cache))
    return cache


def sim_eod(side, entry, stop, t1, fut):
    risk = abs(entry - stop)
    if risk <= 0:
        return None
    for _, b in fut.iterrows():
        h, l = float(b["High"]), float(b["Low"])
        if side == "LONG":
            if l <= stop:
                return -1.0
            if h >= t1:
                return (t1 - entry) / risk
        else:
            if h >= stop:
                return -1.0
            if l <= t1:
                return (entry - t1) / risk
    if fut.empty:
        return None
    c = float(fut["Close"].iloc[-1])
    return (c - entry) / risk if side == "LONG" else (entry - c) / risk


def stop_price(side, extreme, atr, kind, k):
    if kind == "pct":
        return extreme * (1 + k) if side == "SHORT" else extreme * (1 - k)
    return (extreme + k * atr) if side == "SHORT" else (extreme - k * atr)


def backtest_symbol(df, earn, results):
    for day, dd in df.groupby("date", sort=True):
        if day.isoformat() in earn:
            continue
        dd = dd.dropna(subset=["sma20", "std20", "rsi", "vwap", "atr"])
        if dd.empty:
            continue
        sampled = [i for i, ts in enumerate(dd.index) if ts.minute % 15 == 0]
        state = {}
        for i in sampled:
            r = dd.iloc[i]
            m = {k: float(r[k]) for k in ["Close", "High", "Low", "sma20", "rsi",
                                          "upper", "lower", "z", "vwap", "vwap_dev", "atr"]}
            if any(np.isnan(v) for v in m.values()):
                continue
            fut = dd.iloc[i + 1:]
            setup_long = (m["z"] <= -mr.Z_THRESH and m["Close"] < m["lower"]
                          and m["rsi"] < mr.RSI_OVERSOLD and m["vwap_dev"] <= -mr.VWAP_DEV_PCT)
            setup_short = (m["z"] >= mr.Z_THRESH and m["Close"] > m["upper"]
                           and m["rsi"] > mr.RSI_OVERBOUGHT and m["vwap_dev"] >= mr.VWAP_DEV_PCT)

            for side, setup, key in (("SHORT", setup_short, "SHORT"), ("LONG", setup_long, "LONG")):
                st = state.get(key)
                if st is None:
                    if setup and m["Close"] <= mr.MAX_PRICE:
                        state[key] = {"stage": "watch", "lvl": (m["Low"] if side == "SHORT" else m["High"]),
                                      "ext": (m["High"] if side == "SHORT" else m["Low"]), "ck": 0}
                    continue
                if st["stage"] != "watch":
                    continue
                extended = (m["Close"] > m["upper"]) if side == "SHORT" else (m["Close"] < m["lower"])
                if side == "SHORT":
                    st["ext"] = max(st["ext"], m["High"])
                else:
                    st["ext"] = min(st["ext"], m["Low"])
                broke = (m["Low"] < st["lvl"]) if side == "SHORT" else (m["High"] > st["lvl"])
                if extended:
                    st["lvl"] = m["Low"] if side == "SHORT" else m["High"]
                    st["ck"] += 1
                elif broke:
                    st["stage"] = "triggered"
                    entry = st["lvl"]; t1 = m["vwap"]
                    for label, kind, k in STOP_CONFIGS:
                        stop = stop_price(side, st["ext"], m["atr"], kind, k)
                        risk = abs(entry - stop)
                        if risk <= 0:
                            continue
                        rr = (entry - t1) / risk if side == "SHORT" else (t1 - entry) / risk
                        if rr < mr.MIN_RR:
                            continue
                        out = sim_eod(side, entry, stop, t1, fut)
                        if out is not None:
                            results[label].append((side, out))
                else:
                    st["ck"] += 1
                if st.get("stage") == "watch" and st["ck"] > mr.MAX_WATCH_CHECKS:
                    st["stage"] = "expired"


def stats(rs):
    if not rs:
        return "       no trades"
    rs = np.asarray(rs, float)
    wins = (rs > 0).sum()
    losses = rs[rs <= 0]
    winsum = rs[rs > 0].sum()
    pf = (winsum / abs(losses.sum())) if len(losses) and losses.sum() != 0 else float("inf")
    pfs = "inf " if pf == float("inf") else f"{pf:4.2f}"
    return (f"{len(rs):>4} tr | win {wins/len(rs)*100:4.1f}% | exp {rs.mean():+.3f}R "
            f"| total {rs.sum():+7.1f}R | PF {pfs}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default=None)
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()

    chunks = sorted(DB_DIR.glob("chunk_*.parquet"))
    ds = pads.dataset([str(p) for p in chunks])
    yf_syms = mr.fetch_sp100()
    if a.symbols:
        yf_syms = [s.strip().upper() for s in a.symbols.split(",")]

    earn_cache = earnings_for(yf_syms, a.quiet)
    results = {label: [] for label, _, _ in STOP_CONFIGS}
    for n, yf_sym in enumerate(yf_syms, 1):
        try:
            df = load_symbol(ds, yf_sym.replace("-", "."))
        except Exception as e:
            print(f"[{n}] {yf_sym} load error {e}", file=sys.stderr); continue
        if df is None or len(df) < 50:
            continue
        backtest_symbol(prep(df), set(earn_cache.get(yf_sym, [])), results)
        if not a.quiet:
            print(f"[{n}/{len(yf_syms)}] {yf_sym}", file=sys.stderr)
        del df

    print("=" * 78)
    print("STOP-WIDTH SWEEP — 2yr Databento, exact strategy, R:R>=1.5 gate re-applied per width")
    print(f"{'stop width':<18} ALL")
    for label, _, _ in STOP_CONFIGS:
        print(f"{label:<18} {stats([o for s, o in results[label]])}")
    print("\nLONG only:")
    for label, _, _ in STOP_CONFIGS:
        print(f"{label:<18} {stats([o for s, o in results[label] if s == 'LONG'])}")
    print("\nSHORT only:")
    for label, _, _ in STOP_CONFIGS:
        print(f"{label:<18} {stats([o for s, o in results[label] if s == 'SHORT'])}")


if __name__ == "__main__":
    main()
