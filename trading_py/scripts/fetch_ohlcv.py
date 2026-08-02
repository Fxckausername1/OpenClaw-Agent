#!/usr/bin/env python3
"""Fetch historical OHLCV using yfinance and save CSV.
Usage: python fetch_ohlcv.py --symbol AAPL --period 1y --outfile data/AAPL.csv
"""
import argparse
import os
import yfinance as yf
import pandas as pd

def fetch(symbol, period="1y", interval="1d"):
    t = yf.Ticker(symbol)
    df = t.history(period=period, interval=interval)
    if df is None or df.empty:
        raise RuntimeError(f"No data for {symbol}")
    df.index = pd.to_datetime(df.index)
    return df

def main():
    p = argparse.ArgumentParser()
    p.add_argument('--symbol', required=True)
    p.add_argument('--period', default='1y')
    p.add_argument('--interval', default='1d')
    p.add_argument('--outfile', default=None)
    args = p.parse_args()

    df = fetch(args.symbol, period=args.period, interval=args.interval)
    out = args.outfile or f"output/{args.symbol.replace('/','_')}.csv"
    os.makedirs(os.path.dirname(out) or '.', exist_ok=True)
    df.to_csv(out)
    print(f"Saved {len(df)} rows to {out}")

if __name__ == '__main__':
    main()

