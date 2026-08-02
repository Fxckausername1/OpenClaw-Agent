#!/usr/bin/env python3
"""Paper-trade evaluator for the live ORB signaler.

Ingests ORB triggers, opens paper positions, and evaluates them: stop hit ->
-1R; otherwise (momentum hold, no target) exit at the session close with --eod.
Appends to data/orb_paper_trades.csv (strategy-tagged). Mirrors paper_eval.py
but with ORB's exit rule. --eod also prints a recap (wrapper sends to Telegram).
"""
import csv
import json
import shutil
import argparse
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

from paper_execution_model import (
    STATE_FILLED_CLOSED, STATE_NEVER_FILLED, advance_boundary_limit,
    append_rows_dedup, append_terminal, atomic_write_json, evaluator_lock,
    terminal_ids,
)

import mean_reversion_scanner as mrs  # shared Alpaca-first (yfinance fallback) data fetch

from log_setup import get_logger
log = get_logger("eval")  # shared with paper_eval.py -> logs/eval.log

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
OPEN_PATH = DATA / "orb_paper_open.json"
LOCK_PATH = DATA / ".orb_paper_eval.lock"
TERMINAL_PATH = DATA / "orb_paper_terminal.jsonl"
CSV_PATH = DATA / "orb_paper_trades.csv"
RECAP_PATH = DATA / "orb_recap_latest.txt"
ET = ZoneInfo("America/New_York")
FIELDS = ["trade_id", "strategy", "ticker", "side", "entry", "stop",
          "entry_time", "exit_price", "exit_reason", "outcome_r", "close_time",
          "shares", "risk_dollars", "dollar_pnl", "net_r_6bp", "net_r_12bp"]


def money(d):
    return f"+${d:,.2f}" if d >= 0 else f"-${abs(d):,.2f}"


def rd(r, d):
    """Format an R-multiple with its realized dollars, e.g. '+1.50R (+$7.50)'."""
    return f"{r:+.2f}R ({money(d)})"


def realized_dollars(rec, outcome_r, entry, stop):
    """Realized $ P&L = R * risk_dollars (= shares * per-share price delta). Falls back
    to shares*|entry-stop| for records predating the sizing fields, else $0."""
    risk_dollars = float(rec.get("risk_dollars") or 0)
    if not risk_dollars and rec.get("shares"):
        risk_dollars = int(rec["shares"]) * abs(entry - stop)
    return round(outcome_r * risk_dollars, 2)


def net_r(outcome_r, entry, stop, cost_bps):
    """Net-of-friction R multiple at a flat `cost_bps` round-trip cost assumption
    (Tier-2 audit finding #10: outcome_r is friction-free gross, this reconciles it
    toward the real backtest-replay net figure). 1R = risk_pct of entry price, so a flat
    $ cost expressed in bps of entry converts to R by dividing by risk_pct:
        risk_pct = |entry - stop| / entry
        friction_r = (cost_bps / 10000) / risk_pct
        net_r = outcome_r - friction_r
    Returns None (don't fabricate) if risk_pct <= 0 -- e.g. entry == stop or entry == 0."""
    if not entry:
        return None
    risk_pct = abs(entry - stop) / entry
    if risk_pct <= 0:
        return None
    friction_r = (cost_bps / 10000) / risk_pct
    return round(outcome_r - friction_r, 3)


def trg_path(d):
    return DATA / f"orb_triggers_{d.isoformat()}.jsonl"


def load_open():
    if OPEN_PATH.exists():
        try:
            return json.loads(OPEN_PATH.read_text())
        except Exception:
            return {}
    return {}


def in_csv(tid):
    if not CSV_PATH.exists():
        return False
    with CSV_PATH.open() as f:
        return any(r.get("trade_id") == tid for r in csv.DictReader(f))


def ingest(today, op):
    p = trg_path(today)
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        tid = rec.get("trade_id")
        if (tid and tid not in op and not in_csv(tid)
                and tid not in terminal_ids(TERMINAL_PATH)):
            op[tid] = rec


def today_5m(ticker, today):
    # Alpaca real-time (yfinance fallback) via the scanner's shared fetch, so ORB
    # paper fills are evaluated on the SAME feed that produced the signal.
    try:
        df = mrs.get_5m_data(ticker, days=2)
        if df is None or df.empty:
            return None
        if df.index.tz is not None:
            df.index = df.index.tz_convert(ET).tz_localize(None)  # naive ET to match entry_time
        return df[df.index.date == today]
    except Exception:
        return None


def evaluate(today, op, eod=False):
    """Advance each resting ORB order. Requires a real limit touch first.

    Also fixes a second look-ahead: the loop previously started at bars after
    `entry_time` (the OR-break bar) even though the trigger did not exist
    until `detected_at`, which lags by minutes. Bars in that window were
    scored as if an order were already resting. We now start from
    detected_at when it is present."""
    closed, never = [], []
    for tid in list(op.keys()):
        rec = op[tid]
        df = today_5m(rec["ticker"], today)
        if df is None or df.empty:
            continue

        eff = dict(rec)
        if rec.get("detected_at"):
            # The order cannot rest before it was detected.
            eff["entry_time"] = max(pd.Timestamp(rec["entry_time"]),
                                    pd.Timestamp(rec["detected_at"])).isoformat()

        tgt = rec.get("target")
        try:
            res = advance_boundary_limit(
                eff, df, eod=eod,
                target=float(tgt) if tgt not in (None, "") else None)
        except Exception as exc:  # noqa: BLE001
            log.error("evaluate failed for {}: {}", tid, exc)
            continue
        rec["_paper_fill_time"] = eff.get("_paper_fill_time")

        state = res["state"]
        if state == STATE_NEVER_FILLED:
            append_terminal(TERMINAL_PATH, rec, state,
                            datetime.now(ET).isoformat(timespec="seconds"))
            never.append(tid)
            del op[tid]
            continue
        if state != STATE_FILLED_CLOSED:
            continue

        entry = float(rec["entry"]); stop = float(rec["stop"])
        outcome = round(float(res["outcome_r"]), 3)
        row = {k: rec.get(k, "") for k in
               ["trade_id", "strategy", "ticker", "side", "entry", "stop", "entry_time"]}
        row.update(exit_price=round(float(res["exit_price"]), 2),
                   exit_reason=res["exit_reason"],
                   outcome_r=outcome,
                   close_time=datetime.now(ET).isoformat(timespec="seconds"),
                   shares=rec.get("shares", ""), risk_dollars=rec.get("risk_dollars", ""),
                   dollar_pnl=realized_dollars(rec, outcome, entry, stop),
                   net_r_6bp=net_r(outcome, entry, stop, 6),
                   net_r_12bp=net_r(outcome, entry, stop, 12))
        closed.append(row)
        del op[tid]

    if closed:
        append_csv(closed)
    if never:
        log.info("orb_paper_eval: {} never_filled (no limit touch)", len(never))
    return closed


def append_csv(rows):
    """Append, refusing any trade_id already present. ORB has not yet
    manifested the duplicate race, but it shares the identical unlocked
    read-modify-write, so it gets the identical guard."""
    DATA.mkdir(parents=True, exist_ok=True)
    written = append_rows_dedup(CSV_PATH, FIELDS, rows)
    if written != len(rows):
        log.warning("append_csv: {}/{} rows already present, skipped",
                    len(rows) - written, len(rows))
    return written

def track():
    if not CSV_PATH.exists():
        return None
    rs, ds = [], []
    with CSV_PATH.open() as f:
        for r in csv.DictReader(f):
            try:
                rs.append(float(r["outcome_r"]))
                ds.append(float(r.get("dollar_pnl") or 0))
            except Exception:
                pass
    if not rs:
        return None
    w = [x for x in rs if x > 0]
    return dict(n=len(rs), wr=len(w) / len(rs) * 100, exp=sum(rs) / len(rs),
                total=sum(rs), total_dollars=sum(ds))


def backfill_friction():
    """One-time migration (2026-07-19, Tier-2 audit finding #10): add net_r_6bp/net_r_12bp
    to existing CSV rows logged before those fields existed. Pure arithmetic on already-
    present columns (outcome_r, entry, stop) -- no API calls, no bar fetches needed. Backs
    up the CSV first (data/orb_paper_trades.csv.bak_20260719_frictioncols, only if that
    backup doesn't already exist), then rewrites atomically (temp file + Path.replace),
    same discipline as scanner_outcome_tracker.py's backfill_early_checkpoints(). Idempotent:
    rows that already carry a non-empty net_r_6bp are recomputed to the same value (a
    no-op); rows with risk_pct<=0 legitimately stay empty on every run."""
    if not CSV_PATH.exists():
        print("no ledger yet, nothing to backfill")
        return
    with CSV_PATH.open(newline="") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        print("ledger empty, nothing to backfill")
        return
    need = [r for r in rows if not str(r.get("net_r_6bp") or "").strip()]
    if not need:
        print("nothing to backfill -- every row already has net_r_6bp/net_r_12bp")
        return
    backup = CSV_PATH.with_name(CSV_PATH.name + ".bak_20260719_frictioncols")
    if not backup.exists():
        shutil.copy2(CSV_PATH, backup)
        print(f"backed up {CSV_PATH.name} -> {backup.name}")
    else:
        print(f"backup already exists ({backup.name}), leaving it as-is")
    print(f"backfilling {len(need)}/{len(rows)} rows...")
    updated = 0
    for r in need:
        try:
            outcome_r = float(r["outcome_r"])
            entry = float(r["entry"])
            stop = float(r["stop"])
        except (KeyError, ValueError, TypeError):
            print(f"  {r.get('trade_id', '?')}: missing/unparseable outcome_r/entry/stop, leaving net fields empty")
            continue
        r["net_r_6bp"] = net_r(outcome_r, entry, stop, 6)
        r["net_r_12bp"] = net_r(outcome_r, entry, stop, 12)
        updated += 1
    tmp = CSV_PATH.with_name(CSV_PATH.name + ".tmp")
    with tmp.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in FIELDS})
    tmp.replace(CSV_PATH)
    print(f"backfill complete: {updated}/{len(need)} rows computed, {len(rows)} total rows rewritten atomically")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--eod", action="store_true")
    ap.add_argument("--backfill-friction", action="store_true",
                    help="one-time migration: add net_r_6bp/net_r_12bp to existing "
                         "orb_paper_trades.csv rows that predate these fields (2026-07-19, "
                         "Tier-2 audit finding #10). Pure arithmetic on outcome_r/entry/stop, "
                         "backs up the CSV first, rewrites atomically.")
    args = ap.parse_args()
    if args.backfill_friction:
        with evaluator_lock(LOCK_PATH):
            backfill_friction()
        return
    today = datetime.now(ET).date()
    with evaluator_lock(LOCK_PATH):
        op = load_open()
        ingest(today, op)
        closed = evaluate(today, op, eod=args.eod)
        atomic_write_json(OPEN_PATH, op)
    if args.eod:
        st = track()
        lines = [f"\U0001F680 ORB paper recap {today.strftime('%b %d')}"]
        if closed:
            dr = sum(c["outcome_r"] for c in closed)
            dd = sum(c["dollar_pnl"] for c in closed)
            wn = sum(1 for c in closed if c["outcome_r"] > 0)
            lines.append(f"Closed: {len(closed)} ({wn}W/{len(closed)-wn}L) {rd(dr, dd)}")
            for c in closed:
                lines.append(f"  {c['ticker']} {c['side']} {rd(c['outcome_r'], c['dollar_pnl'])} ({c['exit_reason']})")
        else:
            lines.append("No ORB paper trades closed today.")
        if op:
            lines.append(f"Still open: {len(op)}")
        if st:
            lines.append(f"ORB track record: {st['n']} trades | win {st['wr']:.0f}% | "
                         f"exp {st['exp']:+.2f}R | total {st['total']:+.1f}R ({money(st['total_dollars'])})")
        RECAP_PATH.write_text("\n".join(lines))
        log.info("\n".join(lines))
    else:
        log.info(f"orb_paper_eval: {len(closed)} closed, {len(op)} open")


if __name__ == "__main__":
    main()
