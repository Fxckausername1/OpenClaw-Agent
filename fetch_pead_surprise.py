#!/usr/bin/env python3
"""fetch_pead_surprise.py -- ONE-SHOT research fetch (not cron'd) of per-ticker EPS
surprise magnitude for the PEAD (post-earnings-announcement drift) research probe,
2026-07-09.

Distinct from build_earnings_cache.py (the live 12:45 cron job): that script only
grabs blackout DATES (+/-1 day) for the mean-reversion scanner's earnings filter and
throws away magnitude. PEAD's edge is sorted by surprise SIZE, so we need the actual
'Surprise(%)' field yfinance already exposes on get_earnings_dates() (confirmed
present: EPS Estimate / Reported EPS / Surprise(%) columns, yfinance 1.4.1 on this
box). limit=28 pulls ~7yrs of quarterly prints per ticker, matching (and exceeding)
the Alpaca daily-bar history window (wf_daily_cache goes back to ~2020-07-27).

Writes data/pead_surprise_cache.json (NEW file, does not touch earnings_cache.json
or bt_earnings.json -- read-only w.r.t. every existing live path).
"""
import json
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import yfinance as yf

ROOT = Path("/home/heff/.openclaw/workspace")
OUT = ROOT / "data" / "pead_surprise_cache.json"
ET = ZoneInfo("America/New_York")


def main():
    import sys
    sys.path.insert(0, str(ROOT))
    import wide_universe as wu
    import mean_reversion_scanner as mr

    universe = wu.load_universe(rebuild_if_stale=False) or mr.fetch_sp100()
    print(f"universe: {len(universe)} symbols")

    out = {}
    n_events = 0
    n_fail = 0
    t0 = time.time()
    for i, t in enumerate(universe):
        try:
            ed = yf.Ticker(t).get_earnings_dates(limit=28)
            rows = []
            if ed is not None and not ed.empty:
                for ts, row in ed.iterrows():
                    surp = row.get("Surprise(%)")
                    rep = row.get("Reported EPS")
                    if surp is None or rep is None:
                        continue
                    try:
                        if pd_isna(surp) or pd_isna(rep):
                            continue
                    except Exception:
                        pass
                    local = ts.tz_convert(ET) if ts.tzinfo else ts
                    rows.append({
                        "date": local.date().isoformat(),
                        "hour": local.hour,
                        "surprise_pct": float(surp),
                    })
            out[t] = rows
            n_events += len(rows)
        except Exception as e:
            out[t] = []
            n_fail += 1
        if (i + 1) % 20 == 0:
            print(f"  {i+1}/{len(universe)} done, {n_events} events so far, {time.time()-t0:.0f}s")

    OUT.write_text(json.dumps({
        "built": datetime.now(ET).date().isoformat(),
        "n_symbols": len(universe),
        "n_events": n_events,
        "n_fetch_fail": n_fail,
        "tickers": out,
    }, indent=2))
    print(f"\nwrote {OUT}")
    print(f"total: {len(universe)} symbols, {n_events} reported-EPS events, {n_fail} fetch failures")


def pd_isna(x):
    import math
    try:
        return x != x  # NaN check without importing pandas at module scope
    except Exception:
        return False


if __name__ == "__main__":
    main()
