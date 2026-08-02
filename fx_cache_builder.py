#!/usr/bin/env python3
"""
fx_cache_builder.py -- download 60d/5-min major FX pairs via yfinance into fx_cache/ (wf_cache schema).

CAVEATS (read before trusting any downstream result):
  * yfinance FX (=X) bars are MID-PRICE proxies with NO bid/ask and NO real volume (Volume ~= 0). Real
    spread is therefore NOT in the data -- fx_orb_search.py imposes a SYNTHETIC 1.5-pip penalty instead.
  * 5-minute history is capped at ~60 days -> a SMALL sample (~43 sessions/pair). First look only.
  * For a real validation use a true FX feed with bid/ask + spreads. Oanda's v20 PRACTICE API is free and
    is the right choice. Alpaca has NO forex; QuantConnect has FX only inside LEAN (paid node).
"""
from pathlib import Path

import pandas as pd
import yfinance as yf

PAIRS = ["EURUSD=X", "GBPUSD=X", "USDJPY=X", "AUDUSD=X", "USDCAD=X", "USDCHF=X"]
OUT = Path("fx_cache")


def fetch(sym: str) -> pd.DataFrame | None:
    df = yf.download(sym, period="60d", interval="5m", auto_adjust=False, progress=False)
    if df is None or df.empty:
        print(f"  {sym}: NO DATA")
        return None
    if isinstance(df.columns, pd.MultiIndex):                 # yfinance often returns a ticker level
        df.columns = df.columns.get_level_values(0)
    df = df.rename(columns=str.title)                         # 'adj close' -> 'Adj Close', etc.
    out = pd.DataFrame({c: df[c] for c in ["Open", "High", "Low", "Close"]})
    out["Volume"] = df["Volume"] if "Volume" in df.columns else 0.0

    idx = pd.DatetimeIndex(out.index)                         # standardize to NAIVE ET (wf_cache convention)
    idx = idx.tz_localize("UTC") if idx.tz is None else idx
    out.index = idx.tz_convert("America/New_York").tz_localize(None)
    out = out[~out.index.duplicated()].sort_index()
    return out.dropna(subset=["Open", "High", "Low", "Close"])


def main() -> None:
    OUT.mkdir(exist_ok=True)
    for sym in PAIRS:
        df = fetch(sym)
        if df is None:
            continue
        name = sym.replace("=X", "")
        df.to_parquet(OUT / f"{name}.parquet")
        print(f"  {name}: {len(df)} bars  {df.index[0]} -> {df.index[-1]} "
              f"| volume_sum={float(df['Volume'].abs().sum()):.0f}")


if __name__ == "__main__":
    main()
