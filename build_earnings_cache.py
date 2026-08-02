#!/usr/bin/env python3
"""Build a daily earnings blackout cache for the mean-reversion scanner.

Writes data/earnings_cache.json:
  {"built": "YYYY-MM-DD", "tickers": {TICKER: [blackout-date-iso, ...]}}

Blackout = each earnings date +/- 1 calendar day. The scanner skips a ticker
on any of its blackout dates (so it never fades an earnings gap). The scanner
fails open: if this cache is missing or stale it simply applies no filter.

Run once daily before the open.
"""
import json
from datetime import timedelta, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import yfinance as yf
import mean_reversion_scanner as mr

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
CACHE = DATA_DIR / "earnings_cache.json"


def main():
    today = datetime.now(ZoneInfo("America/New_York")).date()
    tickers = mr.fetch_sp100()
    out = {}
    for t in tickers:
        dates = set()
        try:
            ed = yf.Ticker(t).get_earnings_dates(limit=12)
            if ed is not None and not ed.empty:
                for ts in ed.index:
                    d = ts.date()
                    for off in (-1, 0, 1):
                        dates.add((d + timedelta(days=off)).isoformat())
        except Exception:
            pass
        out[t] = sorted(dates)

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps({"built": today.isoformat(), "tickers": out}, indent=2))
    n_today = sum(1 for v in out.values() if today.isoformat() in v)
    print(f"earnings cache built {today} | {len(out)} tickers | {n_today} in blackout today")


if __name__ == "__main__":
    main()
