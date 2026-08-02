#!/usr/bin/env python3
"""orb_tournament_bridge.py - wires the live ORB trigger feed (data/orb_triggers_<date>.jsonl,
written by orb_scanner.py) into options_orchestrator.py's already-built run_tournament(). This
is the missing piece from OPTIONS_TOURNAMENT_SPEC.md's "NEXT" step: run_tournament already
builds every matching S1-S10 arm for the signal, filters by EV>0, MILP-gates (3 total / 2 per
side / $100 risk cap), Thompson-sample picks the arm, sizes qty, and (with arm=True) submits +
ledger-records via options_eval -- all of that already exists and is dry-run-verified. This
script just reads NEW ORB triggers and calls it once per trigger, idempotently (a trigger is
only ever sent to the tournament once -- tracked in data/orb_tournament_processed.jsonl -- since
an ORB trigger is tied to a specific intraday breakout level that goes stale, retrying later
would not be "the same trade").

Real money: untouched. run_tournament's own --arm path submits PAPER only (paper-api.alpaca.
markets); this script never adds a --live path. Its internal kill-switch check (guardrails.py
--kill-check) runs before every order submission, so engaging the kill-switch still halts new
entries the instant it's set, same as the equity bot.

Usage (cron, every 2min during RTH, paired with orb_wrapper.sh's cadence):
  ./venv/bin/python orb_tournament_bridge.py            # dry-run (no orders, no ledger writes)
  ./venv/bin/python orb_tournament_bridge.py --arm      # submits PAPER, records to the ledger
"""
import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
from options_orchestrator import run_tournament

ROOT = Path(__file__).resolve().parent
ET = ZoneInfo("America/New_York")
PROCESSED = ROOT / "data" / "orb_tournament_processed.jsonl"


def load_processed():
    if not PROCESSED.exists():
        return set()
    out = set()
    with PROCESSED.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.add(json.loads(line)["trade_id"])
            except Exception:
                pass
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--arm", action="store_true", help="submit to PAPER (default: dry-run, no side effects)")
    ap.add_argument("--date", help="override trigger-file date (YYYY-MM-DD), for testing against a past day")
    a = ap.parse_args()

    today = a.date or datetime.now(ET).date().isoformat()
    trig_path = ROOT / "data" / f"orb_triggers_{today}.jsonl"
    if not trig_path.exists():
        print(f"no trigger file for {today} yet")
        return

    processed = load_processed() if not a.date else set()  # --date test runs never mark processed
    new = []
    with trig_path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                t = json.loads(line)
            except Exception:
                continue
            if t.get("trade_id") in processed:
                continue
            new.append(t)

    if not new:
        print("no new ORB triggers")
        return

    print(f"{len(new)} new ORB trigger(s) to send to the tournament (arm={a.arm})")
    for t in new:
        tid = t.get("trade_id"); ticker = t.get("ticker"); side = t.get("side")
        if not (tid and ticker and side in ("LONG", "SHORT")):
            print(f"  skip malformed trigger: {t}")
            continue
        direction = 1 if side == "LONG" else -1
        print(f"=== {tid}  dir={direction} ===")
        res = run_tournament(ticker, "ORB", direction, arm=a.arm)
        print(f"  candidates={res.get('candidates')} survivors={res.get('survivors')} "
              f"chosen={res.get('chosen')} reason={res.get('reason')} "
              f"armed={res.get('armed')} blocked={res.get('blocked')} "
              f"risk=${res.get('risk', 0):.0f}")
        if not a.date:
            with PROCESSED.open("a") as out:
                out.write(json.dumps({"trade_id": tid, "ticker": ticker, "direction": direction,
                                       "chosen": res.get("chosen"), "armed": res.get("armed"),
                                       "ts": datetime.now(ET).isoformat()}) + "\n")


if __name__ == "__main__":
    main()
