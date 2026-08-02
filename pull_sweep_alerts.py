"""pull_sweep_alerts.py -- one-off pull of real sweep-alert data (Part 2,
item 1: unusual/sweep options activity) across the 194-ticker universe.
Per-ticker filtered calls (is_sweep=true) rather than a single market-wide
page, since a 200-row unfiltered pull only surfaced 84 of our 194 tickers --
per-ticker calls guarantee full coverage at the same total request cost
pattern as everything else pulled today.
"""
import json
import time
from pathlib import Path

import unusualwhales_client as uw
from uw_historical_pull import ALL_TICKERS

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "data" / "unusualwhales" / "sweep_alerts"
OUT.mkdir(parents=True, exist_ok=True)

for t in ALL_TICKERS:
    try:
        code, body = uw.option_trades_flow_alerts(ticker_symbol=t, is_sweep="true", limit=20)
    except Exception as e:
        print(f"  ERROR {t}: {e}")
        continue
    if code != 200:
        print(f"  [{code}] {t}: {json.dumps(body)[:150]}")
        time.sleep(0.25)
        continue
    (OUT / f"{t}.json").write_text(json.dumps(body, indent=2))
    time.sleep(0.25)

print(f"done: {len(list(OUT.glob('*.json')))} files")
