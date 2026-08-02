#!/usr/bin/env python3
"""Options scanner (one-shot run)
- Uses S&P100 universe
- Detects simple Order Block / FVG / Breaker heuristics on 1m data
- Approximates net flow using option volume vs open interest
- Selects option BUY ideas (0-15 DTE, ~0.20-0.30 delta)
- Prints alerts as plain text (cron will announce them)

Run: ./venv/bin/python scanner.py --once
"""
import sys
import time
import math
import argparse
from datetime import datetime, timedelta
import requests
from bs4 import BeautifulSoup
import yfinance as yf
import numpy as np
import pandas as pd

try:
    from py_vollib.black_scholes.greeks import delta as bs_delta
    from py_vollib.black_scholes import black_scholes
except Exception:
    bs_delta = None


def fetch_sp100():
    # Try Wikipedia first (with a browser UA); if that fails, fall back to a hardcoded S&P-100 list.
    try:
        url = "https://en.wikipedia.org/wiki/S%26P_100"
        r = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        table = soup.find("table", {"class": "wikitable"})
        tickers = []
        if table:
            for row in table.find_all("tr")[1:]:
                cols = row.find_all("td")
                if not cols:
                    continue
                sym = cols[0].get_text(strip=True)
                sym = sym.replace('.', '-')
                tickers.append(sym)
            if tickers:
                return tickers[:100]
    except Exception:
        pass

    # Fallback hardcoded list (common S&P-100 constituents)
    return [
        'AAPL','MSFT','AMZN','NVDA','GOOG','GOOGL','META','TSLA','BRK-B','JNJ',
        'V','UNH','PG','MA','HD','BAC','XOM','CVX','KO','PFE',
        'MRK','ABBV','WMT','DIS','NFLX','ORCL','INTC','CSCO','T','VZ',
        'CRM','MCD','NKE','SBUX','UPS','MMM','CAT','BA','LLY','COST',
        'ABT','TXN','QCOM','AMGN','DHR','BMY','MDLZ','PM','HON','AMAT',
        'GILD','ADP','SPG','BLK','SYK','RTX','GE','ISRG','ZTS','BKNG',
        'SPGI','NOW','TMUS','CVS','SCHW','PLD','ADI','LMT','ATVI','CL',
        'EMR','FDX','GD','HLT','ICE','ITW','KMB','KMI','KHC','MDT',
        'MET','FIS','CI','VRTX','REGN','GPN','MS','GS','AXP','PYPL',
        'ZM','DXCM','ROP','LRCX','MU','SQ','MAR','EW','NOC','PNC'
    ]


def get_minute_data(ticker, days=7):
    try:
        df = yf.download(ticker, period=f"{days}d", interval="1m", progress=False, threads=False)
        if df.empty:
            return None
        df = df.dropna()
        return df
    except Exception:
        return None


def detect_order_block(df):
    # Simple heuristic: look for recent strong candle with vol spike and engulfing
    if df is None or len(df) < 10:
        return False, None
    recent = df.tail(10)
    vol = recent['Volume']
    vmean = vol.mean()
    last = recent.iloc[-1]
    prev = recent.iloc[-2]
    if last['Volume'] > 2.0 * vmean:
        # bullish or bearish engulf
        if last['Close'] > last['Open'] and last['Open'] < prev['Close']:
            return True, 'bull_ob'
        if last['Close'] < last['Open'] and last['Open'] > prev['Close']:
            return True, 'bear_ob'
    return False, None


def detect_fvg(df):
    # Fair Value Gap: look for a gap between consecutive candles exceeding ATR*0.5
    if df is None or len(df) < 20:
        return False
    df2 = df.copy()
    df2['tr'] = np.maximum(df2['High'] - df2['Low'], np.maximum((df2['High'] - df2['Close'].shift(1)).abs(), (df2['Low'] - df2['Close'].shift(1)).abs()))
    atr = df2['tr'].rolling(14).mean().iloc[-1]
    if math.isnan(atr):
        return False
    last_close = df['Close'].iloc[-2]
    curr_open = df['Open'].iloc[-1]
    gap = abs(curr_open - last_close)
    if gap > 0.5 * atr:
        return True
    return False


def detect_breaker(df):
    # Simple breaker: price flips previous support/resistance within last 5 candles
    if df is None or len(df) < 30:
        return False
    highs = df['High'].rolling(10).max().shift(1)
    lows = df['Low'].rolling(10).min().shift(1)
    last = df.iloc[-1]
    if last['Close'] > highs.iloc[-1]:
        return True
    if last['Close'] < lows.iloc[-1]:
        return True
    return False


def approx_historical_volatility(df, days=30):
    try:
        close = df['Close'].resample('1D').last().dropna()
        if len(close) < 10:
            return 0.2
        returns = np.log(close / close.shift(1)).dropna()
        vol = returns.rolling(window=min(len(returns), days)).std().iloc[-1] * np.sqrt(252)
        if vol <= 0 or np.isnan(vol):
            return 0.2
        return float(vol)
    except Exception:
        return 0.2


def find_option_candidates(ticker, spot, hist_vol):
    tk = yf.Ticker(ticker)
    try:
        exps = tk.options
    except Exception:
        return []
    candidates = []
    today = datetime.utcnow().date()
    for exp in exps:
        try:
            d = datetime.strptime(exp, "%Y-%m-%d").date()
        except Exception:
            continue
        dte = (d - today).days
        if dte < 0 or dte > 15:
            continue
        try:
            chain = tk.option_chain(exp)
            calls = chain.calls
            puts = chain.puts
        except Exception:
            continue
        for side, df in (('call', calls), ('put', puts)):
            if df is None or df.empty:
                continue
            # compute delta where possible
            for _, row in df.iterrows():
                K = float(row['strike'])
                iv = float(row.get('impliedVolatility') or hist_vol)
                t = max(dte / 365.0, 1/365)
                delta = None
                if bs_delta is not None:
                    try:
                        # py_vollib delta: (flag, S, K, t, sigma, r)
                        delta = bs_delta('c' if side=='call' else 'p', spot, K, t, iv, 0)
                        delta = abs(delta)
                    except Exception:
                        delta = None
                # fallback: estimate moneyness
                if delta is None:
                    m = abs(spot - K) / spot
                    # rough mapping
                    if m < 0.05:
                        delta = 0.5
                    else:
                        delta = max(0.01, 0.5 - m*5)
                if 0.18 <= delta <= 0.35:
                    vol = int(row.get('volume') or 0)
                    oi = int(row.get('openInterest') or 0)
                    flow_score = 0
                    if oi > 0 and vol > max(5, 0.1*oi):
                        flow_score = 1
                    candidates.append({
                        'ticker': ticker,
                        'side': side,
                        'strike': K,
                        'exp': exp,
                        'dte': dte,
                        'delta': round(delta, 3),
                        'volume': vol,
                        'openInterest': oi,
                        'flow': flow_score
                    })
    return candidates


def has_upcoming_earnings(ticker, within_days=7):
    try:
        tk = yf.Ticker(ticker)
        cal = tk.calendar
        if cal is None or cal.empty:
            return False
        # calendar index may have dates
        for v in cal.values.flatten():
            try:
                dt = pd.to_datetime(v)
                if dt.date() <= (datetime.utcnow().date() + timedelta(days=within_days)):
                    return True
            except Exception:
                continue
        return False
    except Exception:
        return False


def compute_volume_profile(df):
    try:
        prices = df['Close']
        vols = df['Volume']
        bins = np.linspace(prices.min(), prices.max(), 20)
        inds = np.digitize(prices, bins)
        vp = {}
        for i, v in zip(inds, vols):
            b = float(bins[min(i-1, len(bins)-1)])
            vp.setdefault(b, 0)
            vp[b] += int(v)
        # return top price levels by volume
        items = sorted(vp.items(), key=lambda x: x[1], reverse=True)
        top_levels = [p for p, _ in items[:3]]
        return top_levels
    except Exception:
        return []


def scan_universe(tickers, max_tickers=100):
    alerts = []
    for i, t in enumerate(tickers[:max_tickers]):
        try:
            # earnings filter
            if has_upcoming_earnings(t, within_days=1):
                continue
            df = get_minute_data(t, days=7)
            if df is None or df.empty:
                continue
            ob, ob_type = detect_order_block(df)
            fvg = detect_fvg(df)
            br = detect_breaker(df)
            if not (ob or fvg or br):
                continue
            spot = df['Close'].iloc[-1]
            hist_vol = approx_historical_volatility(df, days=30)
            vp = compute_volume_profile(df)
            candidates = find_option_candidates(t, spot, hist_vol)
            # prefer candidates with flow and matching setup
            for c in candidates:
                score = c['flow']
                if ob:
                    score += 1
                if fvg:
                    score += 1
                if br:
                    score += 1
                if score >= 2:
                    reason = []
                    if ob: reason.append(f"OB:{ob_type}")
                    if fvg: reason.append('FVG')
                    if br: reason.append('Breaker')
                    reason_str = ",".join(reason)
                    alerts.append({
                        'ticker': t,
                        'side': c['side'],
                        'strike': c['strike'],
                        'exp': c['exp'],
                        'dte': c['dte'],
                        'delta': c['delta'],
                        'vol': c['volume'],
                        'oi': c['openInterest'],
                        'vp_levels': vp,
                        'reason': reason_str,
                    })
        except Exception as e:
            # keep scanning
            print(f"Error scanning {t}: {e}", file=sys.stderr)
            continue
    return alerts


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--once', dest='once', action='store_true')
    args = parser.parse_args()

    tickers = fetch_sp100()
    if not tickers:
        print("No universe found. Exiting.")
        return
    alerts = scan_universe(tickers, max_tickers=100)
    if not alerts:
        print("No alerts found at", datetime.utcnow().isoformat())
        return
    # print formatted alerts
    for a in alerts:
        print(f"{a['ticker']} | {a['side'].upper()} | strike={a['strike']} exp={a['exp']} DTE={a['dte']} delta={a['delta']} vol={a['vol']} oi={a['oi']} vp={a['vp_levels']} reason={a['reason']}")

if __name__ == '__main__':
    main()
