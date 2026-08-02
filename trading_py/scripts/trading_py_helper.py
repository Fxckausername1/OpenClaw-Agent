"""Helper utilities for the trading scripts."""
import os
import pandas as pd
from pathlib import Path
import subprocess

def ensure_data(symbol, period='1y'):
    outdir = Path('trading-py/output')
    outdir.mkdir(parents=True, exist_ok=True)
    csv = outdir / f"{symbol.replace('/','_')}.csv"
    if csv.exists():
        df = pd.read_csv(csv, index_col=0, parse_dates=True)
        return df

    # fallback: call fetch_ohlcv.py
    cmd = ["python", "trading-py/scripts/fetch_ohlcv.py", "--symbol", symbol, "--period", period, "--outfile", str(csv)]
    subprocess.check_call(cmd)
    df = pd.read_csv(csv, index_col=0, parse_dates=True)
    return df

