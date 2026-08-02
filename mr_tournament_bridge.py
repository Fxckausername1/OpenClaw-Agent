#!/usr/bin/env python3
"""mr_tournament_bridge.py - wires the live MR (mean-reversion) trigger feed (data/mr_triggers_
<date>.jsonl, written by mean_reversion_scanner.py) into options_orchestrator.py's already-built
run_tournament(). Mirrors orb_tournament_bridge.py exactly, substituting the MR trigger/processed
files and signal string.

Closes a real gap found in a 2026-07-05 audit: run_tournament(ticker, signal, direction, arm) has
always been signal-agnostic (it filters options_strategies.STRATEGIES by .signal==signal), and
data/mr_triggers_<date>.jsonl has existed the whole time with the same trade_id/ticker/side field
shape as the ORB trigger file -- but nothing tournament-related ever read it. That means the 5
MR-tagged strategies (S4/S5/S6/S8/S10 per options_strategies.py's STRATEGIES list) had never once
received a trigger, independent of any other issue. This script is the missing bridge, built as a
close mirror of orb_tournament_bridge.py. Idempotency tracking is a SEPARATE file
(data/mr_tournament_processed.jsonl) from the ORB bridge's -- different signal type, kept
independent so a bug in one bridge's idempotency state can never cross-contaminate the other's.

Real money: untouched. run_tournament's own --arm path submits PAPER only (paper-api.alpaca.
markets); this script never adds a --live path. Its internal kill-switch check (guardrails.py
--kill-check) runs before every order submission, so engaging the kill-switch still halts new
entries the instant it's set, same as the equity bot.

Usage (cron, wired into scripts/orb_options_tournament_wrapper.sh right after the ORB bridge
call, same */2 13-21 * * 1-5 cadence -- MR triggers can appear as often as once/minute from the
fast watch-loop, so the existing 2-minute poll is the same latency ORB already accepts):
  ./venv/bin/python mr_tournament_bridge.py            # dry-run (no orders, no ledger writes)
  ./venv/bin/python mr_tournament_bridge.py --arm      # submits PAPER, records to the ledger
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
PROCESSED = ROOT / "data" / "mr_tournament_processed.jsonl"


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
    trig_path = ROOT / "data" / f"mr_triggers_{today}.jsonl"
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
        print("no new MR triggers")
        return

    print(f"{len(new)} new MR trigger(s) to send to the tournament (arm={a.arm})")
    for t in new:
        tid = t.get("trade_id"); ticker = t.get("ticker"); side = t.get("side")
        if not (tid and ticker and side in ("LONG", "SHORT")):
            print(f"  skip malformed trigger: {t}")
            continue
        direction = 1 if side == "LONG" else -1
        print(f"=== {tid}  dir={direction} ===")
        res = run_tournament(ticker, "MR", direction, arm=a.arm)
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
