"""pull_short_interest.py -- one-off pull of short_interest_float and
short_volume_and_ratio across the 194-ticker universe (Part 2, item 7).
Reuses ALL_TICKERS from uw_historical_pull.py for a single source of truth.
"""
import json
import time
from pathlib import Path

import unusualwhales_client as uw
from uw_historical_pull import ALL_TICKERS

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "data" / "unusualwhales"

ENDPOINTS = {
    "short_interest_float": uw.short_interest_float,
    "short_volume_and_ratio": uw.short_volume_and_ratio,
}

for ep_name, fn in ENDPOINTS.items():
    ep_dir = OUT / ep_name
    ep_dir.mkdir(parents=True, exist_ok=True)
    print(f"=== {ep_name} ===")
    for t in ALL_TICKERS:
        try:
            code, body = fn(t)
        except Exception as e:
            print(f"  ERROR {t}: {e}")
            continue
        if code != 200:
            print(f"  [{code}] {t}: {json.dumps(body)[:150]}")
            time.sleep(0.25)
            continue
        (ep_dir / f"{t}.json").write_text(json.dumps(body, indent=2))
        time.sleep(0.25)
    print(f"  done: {len(list(ep_dir.glob('*.json')))} files")
