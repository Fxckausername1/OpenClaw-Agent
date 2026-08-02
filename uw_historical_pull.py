#!/usr/bin/env python3
"""uw_historical_pull.py -- one-time historical pull from Unusual Whales for
the priority ticker list (SPY/QQQ/IWM + the same 11 sector ETFs live_gex.py
already tracks), to validate/calibrate gex_quant_engine.py's live output.
Bounded 2-week trial project -- see the 2026-07-04 handoff note. Saves raw
responses to data/unusualwhales/{endpoint}/{ticker}.json for later diffing.
"""
import json
import time
from pathlib import Path

import unusualwhales_client as uw

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "data" / "unusualwhales"

PRIORITY_TICKERS = ["SPY", "QQQ", "IWM", "XLK", "XLF", "XLE", "XLV", "XLY",
                    "XLP", "XLI", "XLB", "XLU", "XLRE", "XLC"]

# 2026-07-04 expansion: also cover heff's actual tradable universe (the same
# 180-ticker list mean_reversion/orb/premarket/continuation scanners use),
# not just the sector-ETF sample above. Loaded dynamically so this always
# tracks wide_universe.json rather than a copy-pasted snapshot of it.
def _load_wide_universe_tickers():
    path = ROOT / "data" / "wide_universe.json"
    return json.loads(path.read_text())["symbols"]

ALL_TICKERS = PRIORITY_TICKERS + _load_wide_universe_tickers()

ENDPOINTS = {
    "greek_exposure": uw.greek_exposure,
    "gex_levels": uw.gex_levels,
    "oi_per_strike": uw.oi_per_strike,
    "flow_per_strike": uw.flow_per_strike,
    "darkpool": uw.darkpool_ticker,
    "variance_risk_premium": uw.variance_risk_premium,
}


def main():
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
                msg = body if isinstance(body, str) else json.dumps(body)[:200]
                print(f"  [{code}] {t}: {msg}")
                time.sleep(0.25)
                continue
            (ep_dir / f"{t}.json").write_text(json.dumps(body, indent=2))
            if isinstance(body, dict) and "data" in body:
                n = len(body["data"]) if isinstance(body["data"], list) else 1
            elif isinstance(body, list):
                n = len(body)
            else:
                n = 1
            print(f"  [{code}] {t} -> {n} rows saved")
            time.sleep(0.25)


if __name__ == "__main__":
    main()
