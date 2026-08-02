#!/usr/bin/env python3
"""Fetch historical data from Finnhub and save CSV.
Usage: FINNHUB_API_KEY=... python finnhub_fetch.py --symbol AAPL --resolution D --outfile out/AAPL.csv
"""
import argparse
import os
from pathlib import Path
from trading_py.connectors.finnhub_connector import FinnhubClient

def main():
    p = argparse.ArgumentParser()
    p.add_argument('--symbol', required=True)
    p.add_argument('--resolution', default='D')
    p.add_argument('--from_ts', type=int, default=None)
    p.add_argument('--to_ts', type=int, default=None)
    p.add_argument('--outfile', default=None)
    args = p.parse_args()

    client = FinnhubClient()
    df = client.get_candles(args.symbol, resolution=args.resolution, _from=args.from_ts, to=args.to_ts)
    out = args.outfile or f"trading-py/output/{args.symbol.replace('/','_')}_finnhub.csv"
    Path(os.path.dirname(out) or '.').mkdir(parents=True, exist_ok=True)
    df.to_csv(out)
    print(f"Saved {len(df)} rows to {out}")

if __name__ == '__main__':
    main()

