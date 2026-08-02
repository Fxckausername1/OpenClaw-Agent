#!/usr/bin/env python3
import json
from pathlib import Path
import math
import requests
import yfinance as yf
import pandas as pd
from datetime import date, timedelta

ROOT = Path(__file__).resolve().parent
GEX_PATH = ROOT / 'data' / 'live_gex_snapshot.json'
OUT = ROOT / 'gex_confluence.json'

def load_gex():
    j = json.loads(GEX_PATH.read_text())
    return j.get('results', [])

def top_negative(results, n=12, min_abs=1e6):
    neg = [r for r in results if r.get('net_gex') is not None and r['net_gex'] < -min_abs]
    return sorted(neg, key=lambda x: x['net_gex'])[:n]

def recent_candles(ticker, days=120):
    t = yf.Ticker(ticker)
    df = t.history(period=f'{days}d', interval='1d', auto_adjust=False)
    if df.empty:
        return None
    df = df[['Open','High','Low','Close','Volume']].dropna()
    return df

def indicators(df):
    out = {}
    out['sma50'] = df['Close'].rolling(50).mean()
    out['sma200'] = df['Close'].rolling(200).mean()
    out['close_pct_3'] = df['Close'].pct_change().tail(3).sum()
    out['last_close'] = float(df['Close'].iloc[-1])
    out['sma50_last'] = float(out['sma50'].iloc[-1]) if not math.isnan(out['sma50'].iloc[-1]) else None
    out['sma200_last'] = float(out['sma200'].iloc[-1]) if not math.isnan(out['sma200'].iloc[-1]) else None
    return out

def detect_bearish_patterns(df):
    # simple rules: last day is gap down (>0.5% gap) or bearish engulfing
    res = []
    if len(df) < 2:
        return res
    last = df.iloc[-1]; prev = df.iloc[-2]
    gap = (last['Open'] - prev['Close']) / prev['Close']
    if gap < -0.005:
        res.append(f'gap_down {gap:.3%}')
    # bearish engulfing: last body engulfs prev and last close < prev open
    prev_body_high = max(prev['Open'], prev['Close']); prev_body_low = min(prev['Open'], prev['Close'])
    last_body_high = max(last['Open'], last['Close']); last_body_low = min(last['Open'], last['Close'])
    if last_body_high > prev_body_high and last_body_low < prev_body_low and last['Close'] < prev['Open']:
        res.append('bearish_engulfing')
    # 3-day down move
    if df['Close'].pct_change().tail(3).sum() < -0.03:
        res.append('3d_down>3%')
    return res

def fetch_news(ticker, days=14):
    credp = ROOT / 'trading_py' / 'credentials.json'
    key = None
    if credp.exists():
        key = json.loads(credp.read_text()).get('finnhub_api_key')
    if not key:
        return []
    to = date.today(); frm = to - timedelta(days=days)
    url = 'https://finnhub.io/api/v1/company-news'
    params = {'symbol': ticker, 'from': frm.isoformat(), 'to': to.isoformat(), 'token': key}
    r = requests.get(url, params=params, timeout=20)
    if r.status_code != 200:
        return []
    return r.json()

def financials_summary(ticker):
    t = yf.Ticker(ticker)
    info = t.info
    keys = ['marketCap','trailingPE','forwardPE','debtToEquity','profitMargins']
    return {k: info.get(k) for k in keys}

def analyze():
    results = load_gex()
    top = top_negative(results, n=12, min_abs=1e6)
    out = {'generated_at': str(date.today()), 'candidates': []}
    for r in top:
        sym = r['ticker']
        row = {'ticker': sym, 'net_gex': r.get('net_gex'), 'regime': r.get('regime'), 'call_wall': r.get('call_wall'), 'put_wall': r.get('put_wall'), 'flip': r.get('flip')}
        df = recent_candles(sym, days=180)
        if df is None:
            row['error'] = 'no_price'
            out['candidates'].append(row); continue
        ind = indicators(df)
        row.update({'last_close': ind['last_close'], 'sma50': ind['sma50_last'], 'sma200': ind['sma200_last'], '3d_close_pct': ind['close_pct_3']})
        row['bearish_patterns'] = detect_bearish_patterns(df)
        row['financials'] = financials_summary(sym)
        news = fetch_news(sym, days=30)
        row['news_count_30d'] = len(news)
        # simple confluence score
        score = 0
        if ind['sma50_last'] and ind['sma200_last'] and ind['sma50_last'] < ind['sma200_last']:
            score += 1
        if row['bearish_patterns']:
            score += 1
        if r.get('net_gex') and r['net_gex'] < 0:
            score += 1
        if row['news_count_30d']>0:
            score += 1
        row['confluence_score'] = score
        out['candidates'].append(row)
    OUT.write_text(json.dumps(out, indent=2))
    print('wrote', OUT)

if __name__ == '__main__':
    analyze()
