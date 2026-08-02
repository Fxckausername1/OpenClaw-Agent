#!/usr/bin/env python3
"""Position-sizing analysis for the mean-reversion strategy (LONG, MAX_PRICE<=250,
earnings filter). Re-runs the backtest capturing each trade's entry + stop, then
converts to real fractional-share sizes under different account sizes and
risk-per-trade fractions, flagging when a position would exceed available cash.
"""
import sys
import argparse
from datetime import timedelta

import numpy as np
import pandas as pd
import yfinance as yf

import mean_reversion_scanner as mr


def rsi(series, period=14):
    delta = series.diff()
    gain = delta.clip(lower=0); loss = -delta.clip(upper=0)
    ag = gain.rolling(period).mean(); al = loss.rolling(period).mean()
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


def sim(side, entry, stop, t1, fut):
    risk = abs(entry - stop)
    if risk <= 0:
        return None
    for _, b in fut.iterrows():
        if side == "LONG":
            if b["Low"] <= stop:
                return -1.0
            if b["High"] >= t1:
                return (t1 - entry) / risk
    if fut.empty:
        return None
    return (float(fut["Close"].iloc[-1]) - entry) / risk


def trades_for(df, earn):
    """LONG trades only, capped at MAX_PRICE. Returns (entry, stop, outcome_r)."""
    out = []
    for day, dd in df.groupby("date", sort=True):
        if day in earn:
            continue
        dd = dd.dropna(subset=["sma20", "std20", "rsi", "vwap"])
        if dd.empty:
            continue
        sampled = [i for i, ts in enumerate(dd.index) if ts.minute % 15 == 0]
        st = None
        for i in sampled:
            r = dd.iloc[i]
            m = {k: float(r[k]) for k in ["Close", "High", "Low", "sma20", "rsi", "lower", "z", "vwap", "vwap_dev"]}
            if any(np.isnan(v) for v in m.values()):
                continue
            fut = dd.iloc[i + 1:]
            setup_long = (m["z"] <= -mr.Z_THRESH and m["Close"] < m["lower"]
                          and m["rsi"] < mr.RSI_OVERSOLD and m["vwap_dev"] <= -mr.VWAP_DEV_PCT)
            if st is None:
                if setup_long and m["Close"] <= mr.MAX_PRICE:
                    st = {"stage": "watch", "sh": m["High"], "el": m["Low"], "checks": 0}
            elif st["stage"] == "watch":
                st["el"] = min(st["el"], m["Low"])
                if m["Close"] < m["lower"]:
                    st["sh"] = m["High"]; st["checks"] += 1
                elif m["High"] > st["sh"]:
                    entry = st["sh"]; stop = st["el"] * (1 - mr.STOP_BUFFER)
                    risk = entry - stop
                    rr = (m["vwap"] - entry) / risk if risk > 0 else 0
                    if rr >= mr.MIN_RR:
                        st["stage"] = "triggered"
                        o = sim("LONG", entry, stop, m["vwap"], fut)
                        if o is not None:
                            out.append((entry, stop, o))
                    else:
                        st["stage"] = "expired"
                else:
                    st["checks"] += 1
                if st.get("stage") == "watch" and st["checks"] > mr.MAX_WATCH_CHECKS:
                    st["stage"] = "expired"
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-tickers", type=int, default=100)
    ap.add_argument("--days", type=int, default=59)
    a = ap.parse_args()

    trades = []
    for n, t in enumerate(mr.fetch_sp100()[:a.max_tickers], 1):
        df = load(t, a.days)
        if df is None or len(df) < 30:
            continue
        trades.extend(trades_for(prep(df), earnings(t)))
        print(f"[{n}] {t}", file=sys.stderr)

    print("=" * 70)
    print(f"LONG trades (MAX_PRICE<={int(mr.MAX_PRICE)}, earnings filter): {len(trades)}")
    if not trades:
        return

    risk_share = [e - s for e, s, _ in trades]
    risk_pct = [(e - s) / e * 100 for e, s, _ in trades]
    prices = [e for e, _, _ in trades]
    print(f"Entry price:     avg ${np.mean(prices):.0f}  range ${min(prices):.0f}-${max(prices):.0f}")
    print(f"Stop distance:   avg ${np.mean(risk_share):.2f}/share  ({np.mean(risk_pct):.2f}% of price; "
          f"range {min(risk_pct):.2f}-{max(risk_pct):.2f}%)")

    print("\nPosition sizing per trade (fractional shares; one position at a time):")
    print("  acct  risk%  $risk  ->  avg shares | avg notional | % cash-capped | avg $ actually risked")
    for acct in (500, 800):
        for rf in (0.01, 0.02):
            rdoll = acct * rf
            shares, notionals, capped, real_risk, pnl = [], [], 0, [], 0.0
            for (e, s, o) in trades:
                rps = e - s
                want_sh = rdoll / rps
                want_notional = want_sh * e
                if want_notional > acct:
                    sh = acct / e; capped += 1
                else:
                    sh = want_sh
                notional = sh * e
                rr_dollars = sh * rps
                shares.append(sh); notionals.append(notional); real_risk.append(rr_dollars)
                pnl += o * rr_dollars
            print(f"  ${acct}  {int(rf*100)}%   ${rdoll:>4.0f}  ->  {np.mean(shares):5.2f} sh | "
                  f"${np.mean(notionals):6.0f}     | {capped/len(trades)*100:4.0f}%        | "
                  f"${np.mean(real_risk):4.2f}   [sample total P&L ${pnl:+.0f}]")


if __name__ == "__main__":
    main()
