#!/usr/bin/env python3
"""Deep backtest with rich per-trade instrumentation, for loss analysis.

Same exact strategy/config as the live signaler, run on 2yr Databento 5-min
data, but logs per trade: entry time-of-day, day-of-week, setup strength
(z/RSI/VWAP-dev when the stretch first fired), planned R:R, exit reason
(stop / target / eod), hold time, outcome R. Writes data/deep_trades_rich.csv.

Run: ./venv/bin/python backtest_deep.py [--symbols AAPL,NKE] [--quiet]
"""
import sys
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
ET = ZoneInfo("America/New_York")


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
    typ = (df["High"] + df["Low"] + df["Close"]) / 3.0
    df["vwap"] = (typ * df["Volume"]).groupby(df["date"]).cumsum() / df["Volume"].groupby(df["date"]).cumsum()
    df["vwap_dev"] = (c - df["vwap"]) / df["vwap"]
    return df


def get_earnings(yf_sym):
    try:
        import yfinance as yf
        ed = yf.Ticker(yf_sym).get_earnings_dates(limit=24)
        if ed is None or ed.empty:
            return set()
        s = set()
        for ts in ed.index:
            d = ts.date(); s.update({d - timedelta(days=1), d, d + timedelta(days=1)})
        return s
    except Exception:
        return set()


def sim_eod(side, entry, stop, t1, fut):
    risk = abs(entry - stop)
    if risk <= 0:
        return None
    bars = 0
    for _, b in fut.iterrows():
        bars += 1
        h, l = float(b["High"]), float(b["Low"])
        if side == "LONG":
            if l <= stop:
                return -1.0, "stop", bars
            if h >= t1:
                return (t1 - entry) / risk, "target", bars
        else:
            if h >= stop:
                return -1.0, "stop", bars
            if l <= t1:
                return (entry - t1) / risk, "target", bars
    if fut.empty:
        return None
    c = float(fut["Close"].iloc[-1])
    r = (c - entry) / risk if side == "LONG" else (entry - c) / risk
    return r, ("eod_win" if r > 0 else "eod_loss"), bars


def backtest_symbol(df, earnings, symbol):
    rows = []
    for day, dd in df.groupby("date", sort=True):
        if day in earnings:
            continue
        dd = dd.dropna(subset=["sma20", "std20", "rsi", "vwap"])
        if dd.empty:
            continue
        sampled = [i for i, ts in enumerate(dd.index) if ts.minute % 15 == 0]
        state = {}
        for i in sampled:
            r = dd.iloc[i]; ts = dd.index[i]
            m = {k: float(r[k]) for k in ["Close", "High", "Low", "sma20", "rsi",
                                          "upper", "lower", "z", "vwap", "vwap_dev"]}
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
                        state[key] = {"stage": "watch",
                                      "sl": m["Low"], "sh": m["High"],
                                      "eh": m["High"], "el": m["Low"], "ck": 0,
                                      "z0": m["z"], "rsi0": m["rsi"], "dev0": m["vwap_dev"]}
                    continue
                if st["stage"] != "watch":
                    continue
                if side == "SHORT":
                    st["eh"] = max(st["eh"], m["High"])
                    extended = m["Close"] > m["upper"]
                    if extended:
                        st["sl"] = m["Low"]; st["ck"] += 1
                    elif m["Low"] < st["sl"]:
                        entry = st["sl"]; stop = st["eh"] * (1 + mr.STOP_BUFFER)
                        risk = stop - entry
                        rr = (entry - m["vwap"]) / risk if risk > 0 else 0
                        if rr >= mr.MIN_RR:
                            st["stage"] = "triggered"
                            res = sim_eod("SHORT", entry, stop, m["vwap"], fut)
                            if res:
                                out, reason, bars = res
                                rows.append((side, symbol, str(day), ts.strftime("%H:%M"),
                                             ts.weekday(), round(st["z0"], 2), round(st["rsi0"], 1),
                                             round(st["dev0"] * 100, 2), round(rr, 2),
                                             round(out, 3), reason, bars))
                        else:
                            st["stage"] = "expired"
                    else:
                        st["ck"] += 1
                else:
                    st["el"] = min(st["el"], m["Low"])
                    extended = m["Close"] < m["lower"]
                    if extended:
                        st["sh"] = m["High"]; st["ck"] += 1
                    elif m["High"] > st["sh"]:
                        entry = st["sh"]; stop = st["el"] * (1 - mr.STOP_BUFFER)
                        risk = entry - stop
                        rr = (m["vwap"] - entry) / risk if risk > 0 else 0
                        if rr >= mr.MIN_RR:
                            st["stage"] = "triggered"
                            res = sim_eod("LONG", entry, stop, m["vwap"], fut)
                            if res:
                                out, reason, bars = res
                                rows.append((side, symbol, str(day), ts.strftime("%H:%M"),
                                             ts.weekday(), round(st["z0"], 2), round(st["rsi0"], 1),
                                             round(st["dev0"] * 100, 2), round(rr, 2),
                                             round(out, 3), reason, bars))
                        else:
                            st["stage"] = "expired"
                    else:
                        st["ck"] += 1
                if st.get("stage") == "watch" and st["ck"] > mr.MAX_WATCH_CHECKS:
                    st["stage"] = "expired"
    return rows


def stats(rs):
    if not rs:
        return "no trades"
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]
    pf = (sum(wins) / abs(sum(losses))) if losses and sum(losses) != 0 else float("inf")
    pfs = "inf" if pf == float("inf") else f"{pf:.2f}"
    return (f"{len(rs):>4} tr | win {len(wins)/len(rs)*100:4.1f}% | "
            f"exp {np.mean(rs):+.3f}R | total {sum(rs):+7.1f}R | PF {pfs}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default=None)
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()

    chunks = sorted(DB_DIR.glob("chunk_*.parquet"))
    if not chunks:
        raise SystemExit("no databento chunks found")
    ds = pads.dataset([str(p) for p in chunks])
    yf_syms = mr.fetch_sp100()
    if a.symbols:
        yf_syms = [s.strip().upper() for s in a.symbols.split(",")]

    cols = ["side", "symbol", "date", "entry_time", "dow", "z0", "rsi0",
            "dev0_pct", "planned_rr", "outcome_r", "exit_reason", "hold_bars"]
    all_rows = []
    for n, yf_sym in enumerate(yf_syms, 1):
        try:
            df = load_symbol(ds, yf_sym.replace("-", "."))
        except Exception as e:
            print(f"[{n}] {yf_sym} load error {e}", file=sys.stderr); continue
        if df is None or len(df) < 50:
            continue
        df = prep(df)
        all_rows.extend(backtest_symbol(df, get_earnings(yf_sym), yf_sym))
        if not a.quiet:
            print(f"[{n}/{len(yf_syms)}] {yf_sym}", file=sys.stderr)
        del df

    out = ROOT / "data" / "deep_trades_rich.csv"
    pd.DataFrame(all_rows, columns=cols).to_csv(out, index=False)
    rs = [r[9] for r in all_rows]
    print("=" * 60)
    print(f"{len(all_rows)} trades -> {out}")
    print("ALL  :", stats(rs))
    print("LONG :", stats([r[9] for r in all_rows if r[0] == "LONG"]))
    print("SHORT:", stats([r[9] for r in all_rows if r[0] == "SHORT"]))


if __name__ == "__main__":
    main()
