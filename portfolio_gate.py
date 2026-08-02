#!/usr/bin/env python3
"""portfolio_gate.py — Portfolio Manager Gate: concurrency + correlation risk control,
run BEFORE any order is staged/placed (paper or live).

The scanners fire EVERY valid signal — on a big setup day that's 7-8 at once, which a
strict $800 book can neither afford nor survive: piling N same-side trades means a single
regime shift stops them out together (the correlated-loss chain that kills small accounts).
This gate sits in front of staging and admits only a disciplined SUBSET:

  1. CONCURRENCY CAP  — total open (both strategies) never exceeds MAX_CONCURRENT.
  2. SIDE CAP         — at most MAX_PER_SIDE of those on the SAME side, so one directional
                        macro move can't take out the whole book at once (THE core fix).
  3. CAPITAL CAP      — summed open notional stays within TOTAL_CAPITAL (whole-share book).
  4. RANK             — when more candidates than free slots, keep the highest quality
                        (planned_rr desc; ORB has no fixed target -> NEUTRAL_RR; tiebreak =
                        smaller risk$ = more affordable / better R-per-dollar).

Deterministic GUARDRAIL, not a backtested edge — it can only REMOVE/withhold a trade,
never invent one. Complements guardrails.py (kill-switch / daily-loss / hard cap), which
remains the final safety gate. Keep MAX_CONCURRENT == guardrails.MAX_SLOTS.

CLI: ./venv/bin/python portfolio_gate.py --date 2026-06-17   # show what the gate WOULD admit
"""
import sys
import json
import argparse
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
LOG = DATA / "portfolio_gate_log.jsonl"
OPEN_JSONS = ["paper_open.json", "orb_paper_open.json"]
ET = ZoneInfo("America/New_York")
LIVE_PARAMS_PATH = DATA / "live_params.json"

# --- tunables ($800 book) ---
# Unified 2026-07-01: MAX_CONCURRENT/MAX_PER_SIDE/TOTAL_CAPITAL now read from the SAME
# live_params.json "portfolio" block that mean_reversion_scanner.py's NUM_SLOTS/
# TOTAL_CAPITAL/MR_MAX_PRICE derive from -- eliminates the structural disconnect where slot
# count (position sizing) and concurrency cap (gate admission) were two independently
# hardcoded constants that happened to agree by coincidence, not by construction. A tiny
# local loader (not importing mean_reversion_scanner) keeps this file's dependency surface
# minimal, since both scanners AND alpaca_executor.py import it. Fail-open to the values
# live before this unification if the file is missing/corrupt.
_PORTFOLIO_DEFAULT = {"num_slots": 3, "max_concurrent": 3, "max_per_side": 2, "total_capital": 800.0}


def _load_portfolio_config():
    try:
        data = json.loads(LIVE_PARAMS_PATH.read_text()).get("portfolio", {})
        return {**_PORTFOLIO_DEFAULT, **data}
    except Exception:
        return _PORTFOLIO_DEFAULT


_PORTFOLIO = _load_portfolio_config()
MAX_CONCURRENT = int(_PORTFOLIO["max_concurrent"])   # hard cap on total simultaneous open positions
MAX_PER_SIDE = int(_PORTFOLIO["max_per_side"])       # <= this many on the SAME side -> de-correlate
TOTAL_CAPITAL = float(_PORTFOLIO["total_capital"])   # affordability ceiling on summed open notional
NEUTRAL_RR = 2.0        # rank value for signals with no planned target (ORB = EOD exit)


def _rr(c):
    v = c.get("planned_rr")
    try:
        return float(v) if v not in (None, "") else NEUTRAL_RR
    except Exception:
        return NEUTRAL_RR


def _f(v, default):
    try:
        return float(v) if v not in (None, "") else default
    except Exception:
        return default


def rank_key(c):
    # higher planned_rr first; tiebreak smaller risk$ (more affordable / better R per $)
    return (-_rr(c), _f(c.get("risk_dollars"), 9e9))


def select_portfolio(candidates, open_positions=None,
                     max_concurrent=MAX_CONCURRENT, max_per_side=MAX_PER_SIDE,
                     total_capital=TOTAL_CAPITAL, log_context="stage", write_log=True):
    """candidates: pending trigger dicts (ticker, side, planned_rr, risk_dollars, notional,
    strategy, trade_id...). open_positions: dicts already open (side, notional). Returns
    (approved, rejected) — rejected each carry a `gate_reason`."""
    open_positions = open_positions or []
    n_open = len(open_positions)
    side_count = {"LONG": 0, "SHORT": 0}
    notional = 0.0
    for p in open_positions:
        s = str(p.get("side", "")).upper()
        side_count[s] = side_count.get(s, 0) + 1
        notional += _f(p.get("notional"), 0.0)

    approved, rejected = [], []
    for c in sorted(candidates, key=rank_key):
        side = str(c.get("side", "")).upper()
        cn = _f(c.get("notional"), 0.0)
        reason = None
        if n_open + len(approved) >= max_concurrent:
            reason = f"concurrency cap ({max_concurrent}) full"
        elif side_count.get(side, 0) >= max_per_side:
            reason = f"side cap: already {max_per_side} {side} open (correlated-risk limit)"
        elif notional + cn > total_capital:
            reason = f"capital cap: ${notional + cn:.0f} would exceed ${total_capital:.0f}"
        if reason:
            rejected.append({**c, "gate_reason": reason})
        else:
            approved.append(c)
            side_count[side] = side_count.get(side, 0) + 1
            notional += cn
    if write_log:
        _log(approved, rejected, log_context)
    return approved, rejected


def load_open_positions():
    """Pre-live proxy: current open paper positions (side + notional) from the evaluators'
    open-position files. LIVE: the agent passes real RH positions instead."""
    out = []
    for name in OPEN_JSONS:
        p = DATA / name
        if not p.exists():
            continue
        try:
            for rec in json.loads(p.read_text()).values():
                out.append({"ticker": rec.get("ticker"), "side": rec.get("side"),
                            "notional": rec.get("notional")})
        except Exception:
            pass
    return out


def _log(approved, rejected, context):
    try:
        DATA.mkdir(parents=True, exist_ok=True)
        rec = {"ts": datetime.now(ET).isoformat(timespec="seconds"), "context": context,
               "approved": [c.get("trade_id") for c in approved],
               "rejected": [{"trade_id": c.get("trade_id"), "reason": c.get("gate_reason")}
                            for c in rejected]}
        with LOG.open("a") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception:
        pass


def _load_triggers(date_str):
    rows = []
    for prefix in ("mr_triggers", "orb_triggers"):
        p = DATA / f"{prefix}_{date_str}.jsonl"
        if not p.exists():
            continue
        for line in p.read_text().splitlines():
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except Exception:
                    pass
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=datetime.now(ET).date().isoformat())
    a = ap.parse_args()
    cands = _load_triggers(a.date)
    approved, rejected = select_portfolio(cands, open_positions=[], log_context="cli", write_log=False)
    print(f"{a.date}: {len(cands)} signals -> {len(approved)} admitted, {len(rejected)} withheld "
          f"(cap {MAX_CONCURRENT}, {MAX_PER_SIDE}/side, ${TOTAL_CAPITAL:.0f})")
    for c in approved:
        print(f"  ✅ {c.get('strategy','?'):<7} {c.get('ticker'):<5} {c.get('side'):<5} "
              f"RR={c.get('planned_rr','—')} risk${c.get('risk_dollars','?')} notional${c.get('notional','?')}")
    for c in rejected:
        print(f"  ⛔ {c.get('strategy','?'):<7} {c.get('ticker'):<5} {c.get('side'):<5} — {c['gate_reason']}")


if __name__ == "__main__":
    main()
