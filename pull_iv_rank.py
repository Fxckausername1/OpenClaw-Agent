"""pull_iv_rank.py -- one-off pull of the iv-rank endpoint (added
2026-07-04, not part of the original 6-endpoint historical pull) across the
same 194-ticker universe as everything else. Reuses ALL_TICKERS from
uw_historical_pull.py so the ticker list stays a single source of truth.
"""
import json
import time
from pathlib import Path

import unusualwhales_client as uw
from uw_historical_pull import ALL_TICKERS

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "data" / "unusualwhales" / "iv_rank"
OUT.mkdir(parents=True, exist_ok=True)

for t in ALL_TICKERS:
    try:
        code, body = uw.iv_rank(t)
    except Exception as e:
        print(f"  ERROR {t}: {e}")
        continue
    if code != 200:
        print(f"  [{code}] {t}: {json.dumps(body)[:150]}")
        time.sleep(0.25)
        continue
    (OUT / f"{t}.json").write_text(json.dumps(body, indent=2))
    n = len(body["data"]) if isinstance(body, dict) and isinstance(body.get("data"), list) else 1
    print(f"  [{code}] {t} -> {n} rows saved")
    time.sleep(0.25)
