#!/usr/bin/env python3
"""Backtest for the two-stage mean reversion strategy.

Replays the EXACT live state machine (Watch -> Trigger) over ~60 days of 5-min
bars from yfinance, sampling on the same 15-min cadence the cron runs on, and
simulates each triggered trade forward to stop / target1 / session close.

Outcome is measured in R (multiples of risk). Reports win rate, expectancy,
profit factor, and a LONG-only subset (the only side executable on Robinhood).

Run: ./venv/bin/python backtest_mean_reversion.py --max-tickers 100
NOTE: yfinance caps 5-min history at ~60 days -> ~40 sessions. This is a
directional sanity check, not a statistically robust result.
"""
import sys
import argparse
from datetime import datetime

import numpy as np
import pandas as pd
import yfinance as yf

import mean_reversion_scanner as mr


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
    """Vectorized indicators: continuous SMA20/std20/RSI14 + per-session VWAP."""
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


def simulate_trade(side, entry, stop, t1, future):
    """Return R outcome given the bars after the trigger (same session)."""
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


def backtest_ticker(ticker, df):
    df = prep(df)
    trades = []
    for day, day_df in df.groupby("date", sort=True):
        day_df = day_df.dropna(subset=["sma20", "std20", "rsi", "vwap"])
        if day_df.empty:
            continue
        # 15-min cron cadence: sample bars whose minute is a quarter-hour
        sampled_idx = [i for i, ts in enumerate(day_df.index)
                       if ts.minute % 15 == 0]
        state = {}  # key -> dict
        for i in sampled_idx:
            row = day_df.iloc[i]
            m = {
                "close": float(row["Close"]), "high": float(row["High"]),
                "low": float(row["Low"]), "sma20": float(row["sma20"]),
                "rsi": float(row["rsi"]), "upper": float(row["upper"]),
                "lower": float(row["lower"]), "z": float(row["z"]),
                "vwap": float(row["vwap"]), "vwap_dev": float(row["vwap_dev"]),
            }
            if any(np.isnan(v) for v in m.values()):
                continue
            future = day_df.iloc[i + 1:]

            setup_long = (m["z"] <= -mr.Z_THRESH and m["close"] < m["lower"]
                          and m["rsi"] < mr.RSI_OVERSOLD
                          and m["vwap_dev"] <= -mr.VWAP_DEV_PCT)
            setup_short = (m["z"] >= mr.Z_THRESH and m["close"] > m["upper"]
                           and m["rsi"] > mr.RSI_OVERBOUGHT
                           and m["vwap_dev"] >= mr.VWAP_DEV_PCT)

            # SHORT
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
                    risk = stop - entry
                    rr = (entry - m["vwap"]) / risk if risk > 0 else 0
                    if rr >= mr.MIN_RR:
                        st["stage"] = "triggered"
                        out = simulate_trade("SHORT", entry, stop, m["vwap"], future)
                        if out is not None:
                            trades.append(("SHORT", ticker, str(day), round(rr, 2), round(out, 2)))
                    else:
                        st["stage"] = "expired"
                else:
                    st["checks"] += 1
                if st.get("stage") == "watch" and st["checks"] > mr.MAX_WATCH_CHECKS:
                    st["stage"] = "expired"

            # LONG
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
                    risk = entry - stop
                    rr = (m["vwap"] - entry) / risk if risk > 0 else 0
                    if rr >= mr.MIN_RR:
                        st["stage"] = "triggered"
                        out = simulate_trade("LONG", entry, stop, m["vwap"], future)
                        if out is not None:
                            trades.append(("LONG", ticker, str(day), round(rr, 2), round(out, 2)))
                    else:
                        st["stage"] = "expired"
                else:
                    st["checks"] += 1
                if st.get("stage") == "watch" and st["checks"] > mr.MAX_WATCH_CHECKS:
                    st["stage"] = "expired"
    return trades


def report(label, trades):
    if not trades:
        print(f"\n== {label} == no trades")
        return
    rs = [t[4] for t in trades]
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]
    total_r = sum(rs)
    pf = (sum(wins) / abs(sum(losses))) if losses and sum(losses) != 0 else float("inf")
    print(f"\n== {label} ==")
    print(f"  trades:       {len(trades)}")
    print(f"  win rate:     {len(wins)/len(trades)*100:.1f}%  ({len(wins)}W / {len(losses)}L)")
    print(f"  avg R/trade:  {np.mean(rs):+.3f}R  (expectancy)")
    print(f"  total R:      {total_r:+.1f}R")
    print(f"  profit factor:{pf:.2f}")
    print(f"  best / worst: {max(rs):+.2f}R / {min(rs):+.2f}R")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-tickers", type=int, default=100)
    ap.add_argument("--days", type=int, default=59)
    ap.add_argument("--show-trades", action="store_true")
    args = ap.parse_args()

    tickers = mr.fetch_sp100()[:args.max_tickers]
    all_trades = []
    sessions = set()
    for n, t in enumerate(tickers, 1):
        df = load(t, args.days)
        if df is None or len(df) < 30:
            continue
        sessions.update(df.index.date.tolist())
        tr = backtest_ticker(t, df)
        all_trades.extend(tr)
        print(f"[{n}/{len(tickers)}] {t}: {len(tr)} trades", file=sys.stderr)

    print("=" * 60)
    print(f"Backtest window: ~{len(sessions)} sessions across {len(tickers)} tickers")
    print(f"Params: z>={mr.Z_THRESH} RSI {mr.RSI_OVERSOLD}/{mr.RSI_OVERBOUGHT} "
          f"VWAP_dev>={mr.VWAP_DEV_PCT*100:.1f}% MIN_RR={mr.MIN_RR} MAX_PRICE={mr.MAX_PRICE}")
    report("ALL (long + short)", all_trades)
    report("LONG only (Robinhood-executable)", [t for t in all_trades if t[0] == "LONG"])
    report("SHORT only (needs puts)", [t for t in all_trades if t[0] == "SHORT"])

    if args.show_trades:
        print("\n-- trades (side, ticker, date, plannedRR, outcomeR) --")
        for t in sorted(all_trades, key=lambda x: x[2]):
            print("  ", t)


if __name__ == "__main__":
    main()
