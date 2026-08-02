#!/usr/bin/env python3
"""Backtest comparison for the two-stage mean reversion strategy.

Replays the live Watch->Trigger state machine over ~60 days of 5-min bars
(15-min cron cadence) and compares configs in ONE pass (data downloaded once
per ticker):

  A) T1=VWAP, no earnings filter      (current live config = baseline)
  B) T1=VWAP, earnings filter
  C) T1=1-sigma band, earnings filter (closer target)
  D) T1=SMA20 mean, earnings filter

Earnings filter = skip any session within +/-1 calendar day of a reported/
expected earnings date (yfinance). Outcome measured in R. LONG side is the
only Robinhood-executable one.

NOTE: yfinance caps 5-min history at ~60 days -> small sample; treat as a
directional read, not proof. Avoid over-tuning on this few trades.
"""
import sys
import argparse
from datetime import timedelta

import numpy as np
import pandas as pd
import yfinance as yf

import mean_reversion_scanner as mr

CONFIGS = [
    ("A: VWAP / no-earn-filter", "vwap", False),
    ("B: VWAP / earn-filter", "vwap", True),
    ("C: 1-sigma / earn-filter", "onesigma", True),
    ("D: SMA20 / earn-filter", "sma", True),
]


def rsi(series, period=14):
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    ag = gain.rolling(period).mean()
    al = loss.rolling(period).mean()
    rs = ag / al
    out = 100 - (100 / (1 + rs))
    return out.where(al != 0, 100.0)


def load(ticker, days):
    try:
        df = yf.download(ticker, period=f"{days}d", interval="5m",
                         progress=False, threads=False)
        if df.empty:
            return None
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        return df.dropna()
    except Exception:
        return None


def prep(df):
    c = df["Close"]
    df = df.copy()
    df["sma20"] = c.rolling(20).mean()
    df["std20"] = c.rolling(20).std()
    df["rsi"] = rsi(c, 14)
    df["upper"] = df["sma20"] + mr.Z_THRESH * df["std20"]
    df["lower"] = df["sma20"] - mr.Z_THRESH * df["std20"]
    df["z"] = (c - df["sma20"]) / df["std20"]
    df["date"] = df.index.date
    typ = (df["High"] + df["Low"] + df["Close"]) / 3.0
    pv = (typ * df["Volume"]).groupby(df["date"]).cumsum()
    cv = df["Volume"].groupby(df["date"]).cumsum()
    df["vwap"] = pv / cv
    df["vwap_dev"] = (c - df["vwap"]) / df["vwap"]
    return df


def get_earnings(ticker):
    try:
        ed = yf.Ticker(ticker).get_earnings_dates(limit=24)
        if ed is None or ed.empty:
            return set()
        s = set()
        for ts in ed.index:
            d = ts.date()
            s.update({d - timedelta(days=1), d, d + timedelta(days=1)})
        return s
    except Exception:
        return set()


def target_level(side, m, mode):
    if mode == "vwap":
        return m["vwap"]
    if mode == "sma":
        return m["sma20"]
    if mode == "onesigma":
        return m["sma20"] + m["std"] if side == "SHORT" else m["sma20"] - m["std"]
    return m["vwap"]


def simulate_trade(side, entry, stop, t1, future):
    risk = abs(entry - stop)
    if risk <= 0:
        return None
    for _, b in future.iterrows():
        if side == "SHORT":
            if b["High"] >= stop:
                return -1.0
            if b["Low"] <= t1:
                return (entry - t1) / risk
        else:
            if b["Low"] <= stop:
                return -1.0
            if b["High"] >= t1:
                return (t1 - entry) / risk
    if future.empty:
        return None
    close = future["Close"].iloc[-1]
    return (entry - close) / risk if side == "SHORT" else (close - entry) / risk


def backtest_ticker(ticker, df, mode, earnings):
    trades = []
    for day, day_df in df.groupby("date", sort=True):
        if earnings and day in earnings:
            continue
        day_df = day_df.dropna(subset=["sma20", "std20", "rsi", "vwap"])
        if day_df.empty:
            continue
        sampled = [i for i, ts in enumerate(day_df.index) if ts.minute % 15 == 0]
        state = {}
        for i in sampled:
            row = day_df.iloc[i]
            m = {"close": float(row["Close"]), "high": float(row["High"]),
                 "low": float(row["Low"]), "sma20": float(row["sma20"]),
                 "std": float(row["std20"]), "rsi": float(row["rsi"]),
                 "upper": float(row["upper"]), "lower": float(row["lower"]),
                 "z": float(row["z"]), "vwap": float(row["vwap"]),
                 "vwap_dev": float(row["vwap_dev"])}
            if any(np.isnan(v) for v in m.values()):
                continue
            future = day_df.iloc[i + 1:]

            setup_long = (m["z"] <= -mr.Z_THRESH and m["close"] < m["lower"]
                          and m["rsi"] < mr.RSI_OVERSOLD
                          and m["vwap_dev"] <= -mr.VWAP_DEV_PCT)
            setup_short = (m["z"] >= mr.Z_THRESH and m["close"] > m["upper"]
                           and m["rsi"] > mr.RSI_OVERBOUGHT
                           and m["vwap_dev"] >= mr.VWAP_DEV_PCT)

            st = state.get("SHORT")
            if st is None:
                if setup_short and m["close"] <= mr.MAX_PRICE:
                    state["SHORT"] = {"stage": "watch", "signal_low": m["low"],
                                      "extreme_high": m["high"], "checks": 0}
            elif st["stage"] == "watch":
                st["extreme_high"] = max(st["extreme_high"], m["high"])
                if m["close"] > m["upper"]:
                    st["signal_low"] = m["low"]; st["checks"] += 1
                elif m["low"] < st["signal_low"]:
                    entry = st["signal_low"]
                    stop = st["extreme_high"] * (1 + mr.STOP_BUFFER)
                    t1 = target_level("SHORT", m, mode)
                    risk = stop - entry
                    rr = (entry - t1) / risk if risk > 0 else 0
                    if rr >= mr.MIN_RR:
                        st["stage"] = "triggered"
                        out = simulate_trade("SHORT", entry, stop, t1, future)
                        if out is not None:
                            trades.append(("SHORT", out))
                    else:
                        st["stage"] = "expired"
                else:
                    st["checks"] += 1
                if st.get("stage") == "watch" and st["checks"] > mr.MAX_WATCH_CHECKS:
                    st["stage"] = "expired"

            st = state.get("LONG")
            if st is None:
                if setup_long and m["close"] <= mr.MAX_PRICE:
                    state["LONG"] = {"stage": "watch", "signal_high": m["high"],
                                     "extreme_low": m["low"], "checks": 0}
            elif st["stage"] == "watch":
                st["extreme_low"] = min(st["extreme_low"], m["low"])
                if m["close"] < m["lower"]:
                    st["signal_high"] = m["high"]; st["checks"] += 1
                elif m["high"] > st["signal_high"]:
                    entry = st["signal_high"]
                    stop = st["extreme_low"] * (1 - mr.STOP_BUFFER)
                    t1 = target_level("LONG", m, mode)
                    risk = entry - stop
                    rr = (t1 - entry) / risk if risk > 0 else 0
                    if rr >= mr.MIN_RR:
                        st["stage"] = "triggered"
                        out = simulate_trade("LONG", entry, stop, t1, future)
                        if out is not None:
                            trades.append(("LONG", out))
                    else:
                        st["stage"] = "expired"
                else:
                    st["checks"] += 1
                if st.get("stage") == "watch" and st["checks"] > mr.MAX_WATCH_CHECKS:
                    st["stage"] = "expired"
    return trades


def stats(trades):
    if not trades:
        return "no trades"
    rs = [t[1] for t in trades]
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]
    pf = (sum(wins) / abs(sum(losses))) if losses and sum(losses) != 0 else float("inf")
    wr = len(wins) / len(rs) * 100
    return (f"{len(rs):>3} trades | win {wr:4.1f}% | exp {np.mean(rs):+.3f}R "
            f"| totalR {sum(rs):+6.1f} | PF {pf:.2f}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-tickers", type=int, default=100)
    ap.add_argument("--days", type=int, default=59)
    args = ap.parse_args()

    tickers = mr.fetch_sp100()[:args.max_tickers]
    results = {label: [] for label, _, _ in CONFIGS}
    sessions = set()

    for n, t in enumerate(tickers, 1):
        df = load(t, args.days)
        if df is None or len(df) < 30:
            continue
        df = prep(df)
        sessions.update(df["date"].tolist())
        earn = get_earnings(t)
        for label, mode, use_filter in CONFIGS:
            tr = backtest_ticker(t, df, mode, earn if use_filter else None)
            results[label].extend([(t,) + x for x in tr])
        print(f"[{n}/{len(tickers)}] {t}", file=sys.stderr)

    print("=" * 72)
    print(f"Window ~{len(sessions)} sessions x {len(tickers)} tickers | "
          f"z>={mr.Z_THRESH} RSI {mr.RSI_OVERSOLD}/{mr.RSI_OVERBOUGHT} "
          f"MIN_RR={mr.MIN_RR} MAX_PRICE={mr.MAX_PRICE}")
    for label, _, _ in CONFIGS:
        all_tr = [(x[1], x[2]) for x in results[label]]
        long_tr = [(x[1], x[2]) for x in results[label] if x[1] == "LONG"]
        print(f"\n{label}")
        print(f"   ALL  : {stats(all_tr)}")
        print(f"   LONG : {stats(long_tr)}")


if __name__ == "__main__":
    main()
