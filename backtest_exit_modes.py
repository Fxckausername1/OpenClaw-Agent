#!/usr/bin/env python3
"""Compare two exit rules for the mean-reversion strategy (config: T1=VWAP,
earnings filter, MAX_PRICE<=250), on the same triggers:

  Mode 1 (EOD)  : close at the session's last price if TP/SL not hit intraday.
                  (current live behavior)
  Mode 2 (HOLD) : keep the position across days until price touches TP or SL,
                  modeling overnight gaps (a gap through the stop fills at the
                  next open -> loss can exceed 1R). Trades that never resolve
                  within the ~60-day data window are flagged 'unresolved'.

Run: ./venv/bin/python backtest_exit_modes.py --max-tickers 100
NOTE: yfinance 5-min history ~60 days -> small sample; directional only.
"""
import sys
import argparse
from datetime import timedelta

import numpy as np
import pandas as pd
import yfinance as yf

import mean_reversion_scanner as mr


def rsi(s, p=14):
    d = s.diff(); g = d.clip(lower=0); l = -d.clip(upper=0)
    ag = g.rolling(p).mean(); al = l.rolling(p).mean()
    out = 100 - (100 / (1 + ag / al))
    return out.where(al != 0, 100.0)


def load(t, days):
    try:
        df = yf.download(t, period=f"{days}d", interval="5m", progress=False, threads=False)
        if df.empty:
            return None
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        return df.dropna()
    except Exception:
        return None


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


def earnings(t):
    try:
        ed = yf.Ticker(t).get_earnings_dates(limit=12)
        if ed is None or ed.empty:
            return set()
        s = set()
        for ts in ed.index:
            d = ts.date(); s.update({d - timedelta(days=1), d, d + timedelta(days=1)})
        return s
    except Exception:
        return set()


def gen_triggers(df, earn):
    """Yield trigger dicts (entry_ts, side, entry, stop, t1) for the live config."""
    trigs = []
    for day, dd in df.groupby("date", sort=True):
        if day in earn:
            continue
        dd = dd.dropna(subset=["sma20", "std20", "rsi", "vwap"])
        if dd.empty:
            continue
        sampled = [i for i, ts in enumerate(dd.index) if ts.minute % 15 == 0]
        state = {}
        for i in sampled:
            r = dd.iloc[i]; ts = dd.index[i]
            m = {k: float(r[k]) for k in ["Close", "High", "Low", "sma20", "rsi", "upper", "lower", "z", "vwap", "vwap_dev"]}
            if any(np.isnan(v) for v in m.values()):
                continue
            sl = (m["z"] <= -mr.Z_THRESH and m["Close"] < m["lower"] and m["rsi"] < mr.RSI_OVERSOLD and m["vwap_dev"] <= -mr.VWAP_DEV_PCT)
            ss = (m["z"] >= mr.Z_THRESH and m["Close"] > m["upper"] and m["rsi"] > mr.RSI_OVERBOUGHT and m["vwap_dev"] >= mr.VWAP_DEV_PCT)

            st = state.get("SHORT")
            if st is None:
                if ss and m["Close"] <= mr.MAX_PRICE:
                    state["SHORT"] = {"stage": "watch", "sl": m["Low"], "eh": m["High"], "ck": 0}
            elif st["stage"] == "watch":
                st["eh"] = max(st["eh"], m["High"])
                if m["Close"] > m["upper"]:
                    st["sl"] = m["Low"]; st["ck"] += 1
                elif m["Low"] < st["sl"]:
                    entry = st["sl"]; stop = st["eh"] * (1 + mr.STOP_BUFFER); risk = stop - entry
                    rr = (entry - m["vwap"]) / risk if risk > 0 else 0
                    if rr >= mr.MIN_RR:
                        st["stage"] = "triggered"
                        trigs.append({"ts": ts, "side": "SHORT", "entry": entry, "stop": stop, "t1": m["vwap"]})
                    else:
                        st["stage"] = "expired"
                else:
                    st["ck"] += 1
                if st.get("stage") == "watch" and st["ck"] > mr.MAX_WATCH_CHECKS:
                    st["stage"] = "expired"

            st = state.get("LONG")
            if st is None:
                if sl and m["Close"] <= mr.MAX_PRICE:
                    state["LONG"] = {"stage": "watch", "sh": m["High"], "el": m["Low"], "ck": 0}
            elif st["stage"] == "watch":
                st["el"] = min(st["el"], m["Low"])
                if m["Close"] < m["lower"]:
                    st["sh"] = m["High"]; st["ck"] += 1
                elif m["High"] > st["sh"]:
                    entry = st["sh"]; stop = st["el"] * (1 - mr.STOP_BUFFER); risk = entry - stop
                    rr = (m["vwap"] - entry) / risk if risk > 0 else 0
                    if rr >= mr.MIN_RR:
                        st["stage"] = "triggered"
                        trigs.append({"ts": ts, "side": "LONG", "entry": entry, "stop": stop, "t1": m["vwap"]})
                    else:
                        st["stage"] = "expired"
                else:
                    st["ck"] += 1
                if st.get("stage") == "watch" and st["ck"] > mr.MAX_WATCH_CHECKS:
                    st["stage"] = "expired"
    return trigs


def sim_eod(side, entry, stop, t1, df, ts):
    risk = abs(entry - stop)
    sdf = df[(df.index > ts) & (df.index.map(lambda x: x.date()) == ts.date())]
    for _, b in sdf.iterrows():
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
    if sdf.empty:
        return None
    c = float(sdf["Close"].iloc[-1])
    return (c - entry) / risk if side == "LONG" else (entry - c) / risk


def sim_hold(side, entry, stop, t1, df, ts):
    risk = abs(entry - stop)
    fdf = df[df.index > ts]
    prev_date = ts.date()
    for bts, b in fdf.iterrows():
        o, h, l = float(b["Open"]), float(b["High"]), float(b["Low"])
        new_sess = bts.date() != prev_date
        if side == "LONG":
            if new_sess and o <= stop:
                return (o - entry) / risk, "gap_stop", (bts.date() - ts.date()).days
            if new_sess and o >= t1:
                return (o - entry) / risk, "gap_tp", (bts.date() - ts.date()).days
            if l <= stop:
                return -1.0, "stop", (bts.date() - ts.date()).days
            if h >= t1:
                return (t1 - entry) / risk, "tp", (bts.date() - ts.date()).days
        else:
            if new_sess and o >= stop:
                return (entry - o) / risk, "gap_stop", (bts.date() - ts.date()).days
            if new_sess and o <= t1:
                return (entry - o) / risk, "gap_tp", (bts.date() - ts.date()).days
            if h >= stop:
                return -1.0, "stop", (bts.date() - ts.date()).days
            if l <= t1:
                return (entry - t1) / risk, "tp", (bts.date() - ts.date()).days
        prev_date = bts.date()
    return None, "unresolved", None


def report(label, outs):
    outs = [o for o in outs if o is not None]
    if not outs:
        print(f"  {label}: no resolved trades"); return
    wins = [r for r in outs if r > 0]; losses = [r for r in outs if r <= 0]
    pf = (sum(wins) / abs(sum(losses))) if losses and sum(losses) != 0 else float("inf")
    pfs = "inf" if pf == float("inf") else f"{pf:.2f}"
    aw = np.mean(wins) if wins else 0
    al = np.mean(losses) if losses else 0
    print(f"  {label}: {len(outs)} tr | win {len(wins)/len(outs)*100:.1f}% | "
          f"exp {np.mean(outs):+.3f}R | total {sum(outs):+.1f}R | PF {pfs} "
          f"| avgWin {aw:+.2f}R avgLoss {al:+.2f}R")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-tickers", type=int, default=100)
    ap.add_argument("--days", type=int, default=59)
    a = ap.parse_args()

    eod_all, eod_long = [], []
    hold_all, hold_long = [], []
    reasons = {}; hold_days = []; unresolved = 0

    for n, t in enumerate(mr.fetch_sp100()[:a.max_tickers], 1):
        df = load(t, a.days)
        if df is None or len(df) < 30:
            continue
        df = prep(df); earn = earnings(t)
        for tr in gen_triggers(df, earn):
            e = sim_eod(tr["side"], tr["entry"], tr["stop"], tr["t1"], df, tr["ts"])
            h, reason, days = sim_hold(tr["side"], tr["entry"], tr["stop"], tr["t1"], df, tr["ts"])
            if e is not None:
                eod_all.append(e)
                if tr["side"] == "LONG":
                    eod_long.append(e)
            reasons[reason] = reasons.get(reason, 0) + 1
            if reason == "unresolved":
                unresolved += 1
            else:
                hold_all.append(h)
                if tr["side"] == "LONG":
                    hold_long.append(h)
                if days is not None:
                    hold_days.append(days)
        print(f"[{n}] {t}", file=sys.stderr)

    print("=" * 74)
    print(f"Exit-mode comparison | T1=VWAP, earnings filter, MAX_PRICE<={int(mr.MAX_PRICE)}")
    print("\nMode 1 -- EOD close (current live):")
    report("ALL ", eod_all); report("LONG", eod_long)
    print("\nMode 2 -- HOLD until TP/SL (across days, gaps modeled):")
    report("ALL ", hold_all); report("LONG", hold_long)
    print(f"\n  hold exits: " + " / ".join(f"{v} {k}" for k, v in sorted(reasons.items())))
    if hold_days:
        print(f"  holding period (resolved): avg {np.mean(hold_days):.1f}d, "
              f"median {int(np.median(hold_days))}d, max {max(hold_days)}d")
    print(f"  unresolved within data window: {unresolved}")


if __name__ == "__main__":
    main()
