#!/usr/bin/env python3
"""One-off sizing screener (2026-06-28, Phase 3 universe-expansion check): count tradable,
active NYSE/NASDAQ common-stock equities priced $5-$266 (reuses wide_universe.build_universe's
already-fixed asset+price filter) AND with 30-trading-day Average Daily Volume > 2,000,000
shares. Does NOT modify wide_universe.py, does NOT touch any scanner fetch logic, does NOT
rebuild any live cache -- this only reports a count for heff to review before deciding whether
to proceed with the actual universe-expansion rebuild.

NOTE: deliberately does NOT reuse wide_universe.fetch_bars_batch() for the volume pull -- that
function's .between_time("09:30","16:00") filter is intraday-bar-specific and would silently
empty out EVERY daily bar (Alpaca's 1Day bars are timestamped 04:00:00Z = midnight ET, outside
that window -- verified empirically before writing this). Reimplements the same batched/paginated
fetch pattern (proven safe at scale from the 2026-06-26 incident fixes) without that filter.
"""
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
import wide_universe as wu

ET = ZoneInfo("America/New_York")
ADV_MIN = 2_000_000
LOOKBACK_DAYS = 35  # calendar days, so ~24-25 trading days land inside; good enough for "30-day ADV"


def fetch_daily_volume_batch(tickers, days=LOOKBACK_DAYS, batch_size=100):
    """Batched daily-bar fetch -> {ticker: mean Volume over available bars}. Same
    multi-symbol-per-request + page_token pagination as wide_universe.fetch_bars_batch,
    no intraday time filter (doesn't apply to daily bars)."""
    H = wu._headers()
    start = (datetime.now(ET).date() - timedelta(days=days)).isoformat()
    out = {}
    for i in range(0, len(tickers), batch_size):
        batch = tickers[i:i + batch_size]
        rows_by_sym = {s: [] for s in batch}
        page_token = None
        while True:
            params = {"symbols": ",".join(batch), "timeframe": "1Day", "start": start,
                      "feed": "iex", "adjustment": "raw", "limit": 10000, "sort": "asc"}
            if page_token:
                params["page_token"] = page_token
            try:
                r = requests.get("https://data.alpaca.markets/v2/stocks/bars",
                                  headers=H, params=params, timeout=25)
                if r.status_code != 200:
                    break
                d = r.json()
            except Exception:
                break
            for sym, bars in (d.get("bars") or {}).items():
                rows_by_sym.setdefault(sym, []).extend(bars)
            page_token = d.get("next_page_token")
            if not page_token:
                break
        for sym, bars in rows_by_sym.items():
            if not bars:
                continue
            vols = [b["v"] for b in bars[-30:]]  # most recent up-to-30 trading days
            if vols:
                out[sym] = sum(vols) / len(vols)
        if (i // batch_size) % 10 == 0:
            print(f"  ... {min(i+batch_size, len(tickers))}/{len(tickers)} symbols fetched", file=sys.stderr)
    return out


print("step 1: building price-filtered universe ($5-$266, tradable/active NYSE/NASDAQ common stock)...",
      file=sys.stderr)
t0 = time.time()
candidates = wu.build_universe()
print(f"  -> {len(candidates)} candidates after price filter ({time.time()-t0:.0f}s)", file=sys.stderr)

print(f"step 2: fetching {LOOKBACK_DAYS}-day daily bars (batched, {len(candidates)} symbols)...",
      file=sys.stderr)
t0 = time.time()
adv = fetch_daily_volume_batch(candidates)
print(f"  -> got volume data for {len(adv)}/{len(candidates)} symbols ({time.time()-t0:.0f}s)",
      file=sys.stderr)

passing = {s: v for s, v in adv.items() if v > ADV_MIN}

print(f"\n=== RESULT ===")
print(f"Price filter ($5-$266, tradable/active NYSE/NASDAQ common stock): {len(candidates)} symbols")
print(f"  + 30-day ADV data available: {len(adv)} symbols")
print(f"  + 30-day ADV > {ADV_MIN:,} shares: {len(passing)} symbols")
print(f"\nTOP 10 BY ADV:")
for s, v in sorted(passing.items(), key=lambda x: -x[1])[:10]:
    print(f"  {s:<6} {v:>15,.0f}")
