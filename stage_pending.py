#!/usr/bin/env python3
"""stage_pending.py — list today's UN-EXECUTED triggers for conversational staging.

Conversational on-demand execution bridge (no background agent): the headless
scanners log triggers to data/{mr,orb}_triggers_<date>.jsonl. When the user says
"stage pending triggers", the agent runs this to get the pending list, then stages
each via the Robinhood `review_equity_order` MCP and asks for per-trade approval.

This script ONLY reads + filters local files — it never touches a broker endpoint.
A trigger is "pending" if its trade_id is NOT already in data/placed_orders.jsonl
(written by record_placed.py after a successful place_equity_order) and hasn't
appeared earlier in the same run (collapses any duplicate trade_ids).

Output: JSON {date, pending_count, already_placed, pending:[...]} to stdout.

Usage:
  ./venv/bin/python stage_pending.py                 # today (ET)
  ./venv/bin/python stage_pending.py --date 2026-06-15
"""
import sys
import json
import argparse
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

import guardrails       # Phase-1 safety gate (kill-switch / daily-loss / hard cap)
import portfolio_gate   # Portfolio Manager Gate (concurrency + per-side de-correlation + ranking)

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
PLACED_LOG = DATA / "placed_orders.jsonl"
ET = ZoneInfo("America/New_York")
SOURCES = ("mr_triggers", "orb_triggers")   # mean-rev + ORB trigger logs


def load_placed():
    """Set of trade_ids already routed to the broker."""
    placed = set()
    if PLACED_LOG.exists():
        for line in PLACED_LOG.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                placed.add(json.loads(line)["trade_id"])
            except Exception:
                pass
    return placed


def load_triggers(date_str):
    rows = []
    for prefix in SOURCES:
        p = DATA / f"{prefix}_{date_str}.jsonl"
        if not p.exists():
            continue
        for line in p.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                pass
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None, help="ISO date; default = today ET")
    ap.add_argument("--open", type=int, default=None,
                    help="current open position count (LIVE: agent passes real RH count)")
    ap.add_argument("--pnl", type=float, default=None,
                    help="today's realized $ P&L (LIVE: agent passes real RH value)")
    a = ap.parse_args()
    date_str = a.date or datetime.now(ET).date().isoformat()

    placed = load_placed()
    seen = set()
    raw = []
    for t in load_triggers(date_str):
        tid = t.get("trade_id")
        if not tid or tid in placed or tid in seen:
            continue          # already placed, or a duplicate line for the same signal
        seen.add(tid)
        raw.append({
            "trade_id": tid,
            "strategy": t.get("strategy", ""),
            "ticker": t.get("ticker", ""),
            "side": t.get("side", ""),
            "entry": t.get("entry"),
            "stop": t.get("stop"),
            "t1": t.get("t1"),
            "t2": t.get("t2"),
            "planned_rr": t.get("planned_rr"),
            "shares": t.get("shares"),
            "notional": t.get("notional"),
            "risk_dollars": t.get("risk_dollars"),
            "entry_time": t.get("entry_time"),
        })

    # --- STAGING GATE: every trigger must pass check_guardrails() before review ---
    # 1) day-level halt (kill-switch / daily-loss). If tripped, refuse ALL triggers.
    base_open = a.open if a.open is not None else guardrails.open_position_count_local()
    day = guardrails.check_guardrails(open_position_count=base_open,
                                      daily_realized_pnl=a.pnl, new_entries=0)
    stageable, blocked = [], []
    if day["halted_for_day"]:
        guardrails.log_block(day, context="stage_pending:day_halt")
        for tr in raw:
            blocked.append({**tr, "block_reason": "; ".join(day["reasons"])})
    else:
        # 2) PORTFOLIO MANAGER GATE — rank candidates + admit only a de-correlated subset
        #    (concurrency cap + <=MAX_PER_SIDE on one side + capital fit). Replaces the old
        #    first-come position-cap loop; guardrails.MAX_SLOTS stays the hard backstop and
        #    the final --kill-check before place is unchanged.
        if a.open is not None:
            open_positions = [{"side": "?", "notional": 0.0} for _ in range(a.open)]  # live: count only
        else:
            open_positions = portfolio_gate.load_open_positions()                     # pre-live paper proxy
        stageable, rejected = portfolio_gate.select_portfolio(
            raw, open_positions=open_positions, log_context="stage_pending")
        blocked = [{**tr, "block_reason": tr.get("gate_reason", "portfolio gate")} for tr in rejected]

    out = {
        "date": date_str,
        "already_placed": len(placed),
        "guardrails": {"halted_for_day": day["halted_for_day"],
                       "open_at_start": base_open, "checks": day["checks"]},
        "stageable_count": len(stageable),
        "blocked_count": len(blocked),
        "stageable": stageable,    # ONLY these may go to review_equity_order
        "blocked": blocked,        # logged + skipped, shown for transparency
    }
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
