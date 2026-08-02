#!/usr/bin/env python3
"""record_placed.py — append a placed-order record so stage_pending.py dedupes it.

The agent calls this via SSH immediately AFTER a successful place_equity_order,
passing the trade_id plus the broker's order id and the executed terms. This is
the single source of truth for "what has actually been routed", so the same
signal can never be double-placed across repeated "stage pending" prompts.

Usage:
  ./venv/bin/python record_placed.py '{"trade_id":"F:LONG:2026-06-15","rh_order_id":"abc","ticker":"F","side":"buy","shares":1,"limit_price":15.07}'
"""
import sys
import json
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
PLACED_LOG = ROOT / "data" / "placed_orders.jsonl"
ET = ZoneInfo("America/New_York")


def main():
    if len(sys.argv) < 2:
        print("usage: record_placed.py '<json with trade_id>'", file=sys.stderr)
        sys.exit(2)
    try:
        rec = json.loads(sys.argv[1])
    except Exception as e:
        print(f"bad json: {e}", file=sys.stderr)
        sys.exit(2)
    if "trade_id" not in rec:
        print("record must include trade_id", file=sys.stderr)
        sys.exit(2)
    rec.setdefault("placed_at", datetime.now(ET).isoformat(timespec="seconds"))
    PLACED_LOG.parent.mkdir(parents=True, exist_ok=True)
    with PLACED_LOG.open("a") as f:
        f.write(json.dumps(rec) + "\n")
    print(f"recorded {rec['trade_id']} -> {PLACED_LOG.name}")


if __name__ == "__main__":
    main()
