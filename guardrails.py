#!/usr/bin/env python3
"""guardrails.py — Phase 1 safety gate in FRONT of order staging/placement.

Robinhood imposes NO server-side limits on agentic accounts (verified against RH's
official docs), so these checks are the ONLY thing between a scanner bug / runaway
loop and the account. A trade may be staged or placed ONLY if check_guardrails(...)
returns allowed=True. Every block is logged to data/guardrail_blocks.jsonl.

Four independent checks — ALL must pass:
  1. KILL-SWITCH    — manual hard stop. Engaged by `guardrails.py --kill` (creates
                      data/kill_switch.flag) or env HALT_TRADING=1. Trumps everything.
  2. DAILY-LOSS HALT— today's realized $ P&L <= -DAILY_LOSS_LIMIT -> refuse all new
                      entries for the rest of the day.
  3. POSITION CAP   — refuse a new entry if it would push open positions past MAX_SLOTS.
  4. MAX-DRAWDOWN HALT — rolling peak-equity drawdown (all-time cumulative dollar_pnl
                      peak minus current cumulative dollar_pnl, from paper_trades.csv /
                      orb_paper_trades.csv) exceeds MAX_DRAWDOWN_LIMIT -> refuse all new
                      entries (2026-07-05: same-day-only loss halt had no memory of a
                      slow multi-day bleed across several small losing days; this closes
                      that gap at 5x the daily-loss limit, per book).

Data sources: PRE-LIVE, realized P&L and open-position count come from local proxy
files (the paper evaluators' *_paper_trades.csv / *_paper_open.json). ONCE LIVE, the
agent (which holds the RH MCP) passes the real values via daily_realized_pnl= /
open_position_count= to override the proxies — same gate, authoritative inputs.

CLI (for the scanner / stage_pending to gate on; exits non-zero when blocked):
  ./venv/bin/python guardrails.py                 # JSON {allowed, halted_for_day, reasons, checks}
  ./venv/bin/python guardrails.py --open 2 --pnl -5 --new 1   # override inputs
  ./venv/bin/python guardrails.py --kill          # ENGAGE kill-switch
  ./venv/bin/python guardrails.py --unkill        # release kill-switch
"""
import os
import sys
import csv
import json
import argparse
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
KILL_FLAG = DATA / "kill_switch.flag"
GUARD_LOG = DATA / "guardrail_blocks.jsonl"
ET = ZoneInfo("America/New_York")

# --- tunables ($1000 account) ---
# FIXED 2026-07-19 (audit Tier-2 #12): the $10/trade-risk assumption behind the old $30/$150
# below was never actually achievable. calculate_position_size()'s capital-per-slot cap
# ($1000/4 slots = $250, whole shares only) binds long before the 1%-risk cap does for
# almost every real trade -- typical stocks run $20-270 and real stops run 0.15%-1.9%, so
# hitting $10 real risk on a median trade would need ~$2,300 of capital in ONE position (and
# on the tightest real stops seen, tens of thousands -- real leverage, not a rounding issue).
# Confirmed empirically: real risk_dollars across all 616 closed MR+ORB paper trades to date
# has median $0.96 (mean $1.07, p90 $1.70), NOT $10 -- so the old $30 limit silently tolerated
# ~15-30 stop-outs/day instead of the intended 3, and $150 drawdown tolerated proportionally
# more too. Did NOT change position sizing (verified there's no safe way to close this gap
# without either real leverage or a much bigger account -- a decision for heff, not a code
# fix) -- only recalibrated these two thresholds to the REAL median risk-per-trade, so the
# ORIGINAL risk TOLERANCE (3 stop-outs/day, 5x-daily drawdown) is restored using real numbers
# instead of an aspirational one whole-share/capital math can't actually produce. Re-check
# this basis periodically as real trade history accumulates -- same discipline as any other
# empirically-calibrated constant in this codebase, not a one-time-forever number.
DAILY_LOSS_LIMIT = 15.0     # 2026-07-22, heff's explicit ask: raised from 3.0 -- room for
                            # ~15 typical stop-outs (real ~$0.96/trade median risk) before
                            # halting, up from ~3. Loosens most of the way back toward the
                            # pre-2026-07-19 $30 tolerance the audit fix had deliberately
                            # tightened -- a real risk-tolerance choice, not a bug fix.
MAX_DRAWDOWN_LIMIT = 75.0   # rolling peak-equity halt: 5x DAILY_LOSS_LIMIT, restored
                            # 2026-07-22 (heff's ask, "raise max drawdown to match") after
                            # DAILY_LOSS_LIMIT went 3.0->15.0 the same day -- was briefly
                            # equal (15/15) and effectively redundant with the daily halt;
                            # this restores real separation (5 bad days in a row, not just 1,
                            # before the all-time check trips independently of the daily one).
# MAX_SLOTS (2026-07-01): now derives from the SAME live_params.json "portfolio" block
# portfolio_gate.py and mean_reversion_scanner.py already read -- this was a THIRD
# independently-hardcoded copy of the slot count (portfolio_gate.py's own docstring even
# warned "Keep MAX_CONCURRENT == guardrails.MAX_SLOTS" and it had already silently drifted:
# found live at MAX_SLOTS=3 while MAX_CONCURRENT had been unified+moved to 4). Local loader,
# not importing portfolio_gate/mean_reversion_scanner, to keep this file's dependency
# surface minimal (it is the FIRST gate every staging path runs through).
_LIVE_PARAMS_PATH = DATA / "live_params.json"
_PORTFOLIO_DEFAULT = {"max_concurrent": 3}


def _load_max_slots():
    try:
        data = json.loads(_LIVE_PARAMS_PATH.read_text()).get("portfolio", {})
        return int({**_PORTFOLIO_DEFAULT, **data}["max_concurrent"])
    except Exception:
        return _PORTFOLIO_DEFAULT["max_concurrent"]


MAX_SLOTS = _load_max_slots()   # max concurrent open positions
PAPER_TRADE_CSVS = ["paper_trades.csv", "orb_paper_trades.csv"]
PAPER_OPEN_JSONS = ["paper_open.json", "orb_paper_open.json"]


def kill_switch_engaged():
    if os.environ.get("HALT_TRADING", "").strip().lower() in ("1", "true", "yes", "on"):
        return True, "env HALT_TRADING"
    if KILL_FLAG.exists():
        return True, f"flag file {KILL_FLAG.name}"
    return False, ""


def today_realized_pnl():
    """Sum dollar_pnl of trades CLOSED today across the paper evaluators' CSVs."""
    today = datetime.now(ET).date().isoformat()
    total = 0.0
    for name in PAPER_TRADE_CSVS:
        p = DATA / name
        if not p.exists():
            continue
        with p.open() as f:
            for row in csv.DictReader(f):
                if (row.get("close_time") or "")[:10] != today:
                    continue
                try:
                    total += float(row.get("dollar_pnl") or 0)
                except Exception:
                    pass
    return round(total, 2)


def peak_drawdown():
    """Rolling peak-equity drawdown across ALL historical closed trades (not just today).
    Reuses the same CSV read/parse approach as today_realized_pnl() (dashboard_snapshot.py's
    closed_trades_today() reads these same two CSVs the same way), but folds in every row
    regardless of close_time, sorted chronologically, to build a cumulative dollar_pnl curve.
    Tracks the all-time MAX of that curve (the peak) vs the LATEST point (current) — recomputed
    fresh on every call rather than persisted, since the CSVs are small (~200 rows total) and
    already hold full history, so no new stateful file is needed.
    Returns (peak, current, drawdown) all rounded to cents; drawdown = max(0, peak - current)."""
    rows = []
    for name in PAPER_TRADE_CSVS:
        p = DATA / name
        if not p.exists():
            continue
        with p.open() as f:
            for row in csv.DictReader(f):
                ct = row.get("close_time") or ""
                if not ct:
                    continue
                try:
                    pnl = float(row.get("dollar_pnl") or 0)
                except Exception:
                    continue
                rows.append((ct, pnl))
    if not rows:
        return 0.0, 0.0, 0.0
    rows.sort(key=lambda r: r[0])
    cum = 0.0
    peak = 0.0  # curve starts at 0 before the first closed trade
    for _, pnl in rows:
        cum += pnl
        if cum > peak:
            peak = cum
    current = cum
    drawdown = max(0.0, peak - current)
    return round(peak, 2), round(current, 2), round(drawdown, 2)


def open_position_count_local():
    """Open positions counted from the paper evaluators' open-position files (pre-live proxy)."""
    n = 0
    for name in PAPER_OPEN_JSONS:
        p = DATA / name
        if not p.exists():
            continue
        try:
            n += len(json.loads(p.read_text()))
        except Exception:
            pass
    return n


def check_guardrails(open_position_count=None, daily_realized_pnl=None, new_entries=1):
    """Decision for the execution gate. Pass live RH values to override local proxies.
    `new_entries` = positions this action would open (for the cap check)."""
    killed, ksrc = kill_switch_engaged()
    pnl = daily_realized_pnl if daily_realized_pnl is not None else today_realized_pnl()
    loss_halt = pnl <= -abs(DAILY_LOSS_LIMIT)
    open_n = open_position_count if open_position_count is not None else open_position_count_local()
    cap_block = (open_n + new_entries) > MAX_SLOTS
    peak, current, drawdown = peak_drawdown()
    drawdown_halt = drawdown > MAX_DRAWDOWN_LIMIT

    reasons = []
    if killed:
        reasons.append(f"KILL-SWITCH engaged ({ksrc}) — all activity aborted")
    if loss_halt:
        reasons.append(f"DAILY-LOSS HALT — realized {pnl:+.2f} <= -{DAILY_LOSS_LIMIT:.2f}; no new entries today")
    if drawdown_halt:
        reasons.append(f"MAX-DRAWDOWN HALT — {drawdown:.2f} > {MAX_DRAWDOWN_LIMIT:.2f} below all-time peak "
                        f"({peak:.2f}); no new entries")
    if cap_block:
        reasons.append(f"POSITION CAP — {open_n} open + {new_entries} new > {MAX_SLOTS} slots")

    return {
        "allowed": not (killed or loss_halt or drawdown_halt or cap_block),
        "halted_for_day": killed or loss_halt or drawdown_halt,   # day-level halt vs a transient cap rejection
        "reasons": reasons,
        "checks": {
            "kill_switch": {"engaged": killed, "source": ksrc},
            "daily_loss": {"realized_pnl": pnl, "limit": -abs(DAILY_LOSS_LIMIT), "halt": loss_halt},
            "max_drawdown": {"peak": peak, "current": current, "drawdown": drawdown,
                              "limit": MAX_DRAWDOWN_LIMIT, "halt": drawdown_halt},
            "position_cap": {"open": open_n, "new": new_entries, "max": MAX_SLOTS, "block": cap_block},
        },
    }


def log_block(result, context=""):
    """Append a structured audit record whenever the gate blocks. No-op if allowed."""
    if result["allowed"]:
        return
    DATA.mkdir(parents=True, exist_ok=True)
    rec = {"ts": datetime.now(ET).isoformat(timespec="seconds"), "context": context,
           "reasons": result["reasons"], "checks": result["checks"]}
    with GUARD_LOG.open("a") as f:
        f.write(json.dumps(rec) + "\n")
    # also emit a clean human line to stderr so cron logs show the block
    print("BLOCKED [" + context + "]: " + " | ".join(result["reasons"]), file=sys.stderr)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--kill", action="store_true", help="ENGAGE kill-switch")
    ap.add_argument("--unkill", action="store_true", help="release kill-switch")
    ap.add_argument("--kill-check", action="store_true",
                    help="FINAL execution gate: exit non-zero iff the kill-switch is engaged")
    ap.add_argument("--open", type=int, default=None, help="override open position count")
    ap.add_argument("--pnl", type=float, default=None, help="override today's realized $ P&L")
    ap.add_argument("--new", type=int, default=1, help="new entries this action would open")
    ap.add_argument("--context", default="cli", help="label for the block log")
    a = ap.parse_args()

    if a.kill:
        DATA.mkdir(parents=True, exist_ok=True)
        KILL_FLAG.write_text(f"engaged {datetime.now(ET).isoformat(timespec='seconds')}\n")
        print(f"KILL-SWITCH ENGAGED -> {KILL_FLAG}")
        return
    if a.kill_check:
        # final gate right before place_equity_order — re-checks the kill-switch in case a
        # halt was engaged during the manual-approval delay. Fast, kill-switch only.
        killed, ksrc = kill_switch_engaged()
        if killed:
            print(f"ABORT PLACE — kill-switch engaged ({ksrc})", file=sys.stderr)
            sys.exit(1)
        print("ok — kill-switch clear, safe to place")
        return
    if a.unkill:
        if KILL_FLAG.exists():
            KILL_FLAG.unlink()
            print("KILL-SWITCH released")
        else:
            print("kill-switch was not engaged")
        return

    result = check_guardrails(open_position_count=a.open, daily_realized_pnl=a.pnl, new_entries=a.new)
    log_block(result, context=a.context)
    print(json.dumps(result, indent=2))
    sys.exit(0 if result["allowed"] else 1)   # non-zero exit when blocked, for shell gating


if __name__ == "__main__":
    main()
