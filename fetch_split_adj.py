#!/usr/bin/env python3
"""RESEARCH ONLY, one-off. Fetches split-adjusted daily bars for the 11 SPDR sector ETFs
+ SPY (production sector_rotation.py uses adjustment=raw, which is corrupted by the real
Dec-2025 2:1 SPDR sector ETF share split). Writes to a SEPARATE file, does NOT touch
data/sector_etf_daily.parquet (the live production cache) or any other production file."""
import sys
sys.path.insert(0, ".")
import sector_rotation as sr

def fetch_split_adjusted(symbols, days=sr.HISTORY_DAYS):
    import requests
    from datetime import datetime, timedelta
    H = sr._headers()
    start = (datetime.now(sr.ET).date() - timedelta(days=days)).isoformat()
    rows_by_sym = {s: [] for s in symbols}
    page_token = None
    while True:
        params = {"symbols": ",".join(symbols), "timeframe": "1Day", "start": start,
                  "feed": "iex", "adjustment": "split", "limit": 10000, "sort": "asc"}
        if page_token:
            params["page_token"] = page_token
        r = requests.get("https://data.alpaca.markets/v2/stocks/bars",
                          headers=H, params=params, timeout=25)
        r.raise_for_status()
        d = r.json()
        for sym, bars in (d.get("bars") or {}).items():
            rows_by_sym.setdefault(sym, []).extend(bars)
        page_token = d.get("next_page_token")
        if not page_token:
            break
    import pandas as pd
    frames = []
    for sym, bars in rows_by_sym.items():
        if not bars:
            print(f"WARNING: no bars for {sym}")
            continue
        df = pd.DataFrame(bars)
        df["t"] = pd.to_datetime(df["t"], utc=True).dt.tz_convert(sr.ET).dt.date
        df["symbol"] = sym
        df = df.rename(columns={"o": "Open", "h": "High", "l": "Low", "c": "Close", "v": "Volume"})
        frames.append(df[["symbol", "t", "Open", "High", "Low", "Close", "Volume"]])
    out = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    return out

if __name__ == "__main__":
    symbols = sr.SECTOR_ETFS + [sr.BENCHMARK]
    df = fetch_split_adjusted(symbols)
    df.to_parquet("data/sector_etf_daily_SPLITADJ_research.parquet")
    print(f"{len(df)} rows, {df['symbol'].nunique()} symbols -> data/sector_etf_daily_SPLITADJ_research.parquet")
    # quick spot check across the split date
    for sym in ["XLU", "XLE", "XLY"]:
        g = df[df["symbol"] == sym].sort_values("t")
        g["t"] = g["t"].astype(str)
        sub = g[(g["t"] >= "2025-12-01") & (g["t"] <= "2025-12-10")]
        print(sym)
        print(sub[["t", "Open", "Close"]].to_string(index=False))
