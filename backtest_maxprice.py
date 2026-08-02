#!/usr/bin/env python3
"""MAX_PRICE sweep for the mean-reversion strategy.

Runs the chosen live config (T1=VWAP, earnings filter ON) over ~60 days of
5-min bars with NO price cap, tagging each trade with the price it qualified
at (close when the WATCH was created). Then reports performance for several
candidate MAX_PRICE thresholds by post-filtering -- one download pass, exact.

Run: ./venv/bin/python backtest_maxprice.py --max-tickers 100
NOTE: yfinance caps 5-min history at ~60 days -> small sample; directional only.
"""
import sys
import argparse
from datetime import timedelta

import numpy as np
import pandas as pd
import yfinance as yf

import mean_reversion_scanner as mr

THRESHOLDS = [80, 120, 150, 200, 250, 1e9]


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
        ed = yf.Ticker(ticker).get_earnings_dates(limit=12)
        if ed is None or ed.empty:
            return set()
        s = set()
        for ts in ed.index:
            d = ts.date()
            s.update({d - timedelta(days=1), d, d + timedelta(days=1)})
        return s
    except Exception:
        return set()


def simulate(side, entry, stop, t1, future):
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


def backtest_ticker(df, earnings):
    """Return list of (side, outcome_r, watch_price) with NO price cap."""
    trades = []
    for day, day_df in df.groupby("date", sort=True):
        if day in earnings:
            continue
        day_df = day_df.dropna(subset=["sma20", "std20", "rsi", "vwap"])
        if day_df.empty:
            continue
        sampled = [i for i, ts in enumerate(day_df.index) if ts.minute % 15 == 0]
        state = {}
        for i in sampled:
            row = day_df.iloc[i]
            m = {k: float(row[k]) for k in
                 ["Close", "High", "Low", "sma20", "rsi", "upper", "lower", "z", "vwap", "vwap_dev"]}
            if any(np.isnan(v) for v in m.values()):
                continue
            future = day_df.iloc[i + 1:]
            setup_long = (m["z"] <= -mr.Z_THRESH and m["Close"] < m["lower"]
                          and m["rsi"] < mr.RSI_OVERSOLD and m["vwap_dev"] <= -mr.VWAP_DEV_PCT)
            setup_short = (m["z"] >= mr.Z_THRESH and m["Close"] > m["upper"]
                           and m["rsi"] > mr.RSI_OVERBOUGHT and m["vwap_dev"] >= mr.VWAP_DEV_PCT)

            st = state.get("SHORT")
            if st is None:
                if setup_short:
                    state["SHORT"] = {"stage": "watch", "signal_low": m["Low"],
                                      "extreme_high": m["High"], "checks": 0, "wp": m["Close"]}
            elif st["stage"] == "watch":
                st["extreme_high"] = max(st["extreme_high"], m["High"])
                if m["Close"] > m["upper"]:
                    st["signal_low"] = m["Low"]; st["checks"] += 1
                elif m["Low"] < st["signal_low"]:
                    entry = st["signal_low"]; stop = st["extreme_high"] * (1 + mr.STOP_BUFFER)
                    risk = stop - entry
                    rr = (entry - m["vwap"]) / risk if risk > 0 else 0
                    if rr >= mr.MIN_RR:
                        st["stage"] = "triggered"
                        out = simulate("SHORT", entry, stop, m["vwap"], future)
                        if out is not None:
                            trades.append(("SHORT", out, st["wp"]))
                    else:
                        st["stage"] = "expired"
                else:
                    st["checks"] += 1
                if st.get("stage") == "watch" and st["checks"] > mr.MAX_WATCH_CHECKS:
                    st["stage"] = "expired"

            st = state.get("LONG")
            if st is None:
                if setup_long:
                    state["LONG"] = {"stage": "watch", "signal_high": m["High"],
                                     "extreme_low": m["Low"], "checks": 0, "wp": m["Close"]}
            elif st["stage"] == "watch":
                st["extreme_low"] = min(st["extreme_low"], m["Low"])
                if m["Close"] < m["lower"]:
                    st["signal_high"] = m["High"]; st["checks"] += 1
                elif m["High"] > st["signal_high"]:
                    entry = st["signal_high"]; stop = st["extreme_low"] * (1 - mr.STOP_BUFFER)
                    risk = entry - stop
                    rr = (m["vwap"] - entry) / risk if risk > 0 else 0
                    if rr >= mr.MIN_RR:
                        st["stage"] = "triggered"
                        out = simulate("LONG", entry, stop, m["vwap"], future)
                        if out is not None:
                            trades.append(("LONG", out, st["wp"]))
                    else:
                        st["stage"] = "expired"
                else:
                    st["checks"] += 1
                if st.get("stage") == "watch" and st["checks"] > mr.MAX_WATCH_CHECKS:
                    st["stage"] = "expired"
    return trades


def stats(outcomes):
    if not outcomes:
        return "       no trades"
    wins = [r for r in outcomes if r > 0]
    losses = [r for r in outcomes if r <= 0]
    pf = (sum(wins) / abs(sum(losses))) if losses and sum(losses) != 0 else float("inf")
    pfs = "inf " if pf == float("inf") else f"{pf:4.2f}"
    return (f"{len(outcomes):>3} tr | win {len(wins)/len(outcomes)*100:4.1f}% | "
            f"exp {np.mean(outcomes):+.3f}R | total {sum(outcomes):+6.1f}R | PF {pfs}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-tickers", type=int, default=100)
    ap.add_argument("--days", type=int, default=59)
    args = ap.parse_args()

    tickers = mr.fetch_sp100()[:args.max_tickers]
    all_trades = []
    for n, t in enumerate(tickers, 1):
        df = load(t, args.days)
        if df is None or len(df) < 30:
            continue
        df = prep(df)
        earn = get_earnings(t)
        all_trades.extend(backtest_ticker(df, earn))
        print(f"[{n}/{len(tickers)}] {t}", file=sys.stderr)

    print("=" * 78)
    print(f"MAX_PRICE sweep | config: T1=VWAP, earnings-filter ON | "
          f"z>={mr.Z_THRESH} RSI {mr.RSI_OVERSOLD}/{mr.RSI_OVERBOUGHT} MIN_RR={mr.MIN_RR}")
    print(f"(uncapped pool: {len(all_trades)} trades; "
          f"{sum(1 for x in all_trades if x[0]=='LONG')} LONG / "
          f"{sum(1 for x in all_trades if x[0]=='SHORT')} SHORT)")
    for thr in THRESHOLDS:
        label = "no cap" if thr >= 1e9 else f"<= ${int(thr)}"
        sub = [x for x in all_trades if x[2] <= thr]
        longs = [x[1] for x in sub if x[0] == "LONG"]
        allr = [x[1] for x in sub]
        print(f"\nMAX_PRICE {label}")
        print(f"   ALL : {stats(allr)}")
        print(f"   LONG: {stats(longs)}")


if __name__ == "__main__":
    main()
