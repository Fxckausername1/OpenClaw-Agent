#!/usr/bin/env python3
"""fetch_vix_regime.py — pull VIX + VIX3M daily, compute the term-structure ratio
(VIX3M - VIX)/VIX, save to data/vix_regime.csv for the backtest's VIX regime filter.
ratio > 0  = contango (normal, mean-rev safe);  ratio < 0 = backwardation (panic)."""
import pandas as pd
import yfinance as yf
from pathlib import Path

OUT = Path(__file__).resolve().parent / "data" / "vix_regime.csv"


def closes(df):
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    return df["Close"]


vix = yf.download("^VIX", start="2024-05-01", end="2026-06-13", progress=False, threads=False)
vix3m = yf.download("^VIX3M", start="2024-05-01", end="2026-06-13", progress=False, threads=False)
df = pd.DataFrame({"vix": closes(vix), "vix3m": closes(vix3m)}).dropna()
df["ratio"] = (df["vix3m"] - df["vix"]) / df["vix"]
df.index = df.index.tz_localize(None) if df.index.tz is not None else df.index
df.index.name = "date"
df.to_csv(OUT)
back = int((df["ratio"] < 0).sum())
print(f"saved {len(df)} days -> {OUT}")
print(f"backwardation days (ratio<0): {back} ({back/len(df)*100:.1f}%)")
print(df.tail(4).to_string())
