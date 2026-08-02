#!/usr/bin/env python3
"""Paper-trade evaluator for the live ORB signaler.

Ingests ORB triggers, opens paper positions, and evaluates them: stop hit ->
-1R; otherwise (momentum hold, no target) exit at the session close with --eod.
Appends to data/orb_paper_trades.csv (strategy-tagged). Mirrors paper_eval.py
but with ORB's exit rule. --eod also prints a recap (wrapper sends to Telegram).
"""
import csv
import json
import argparse
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

import mean_reversion_scanner as mrs  # shared Alpaca-first (yfinance fallback) data fetch

from log_setup import get_logger
log = get_logger("eval")  # shared with paper_eval.py -> logs/eval.log

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
OPEN_PATH = DATA / "orb_paper_open.json"
CSV_PATH = DATA / "orb_paper_trades.csv"
RECAP_PATH = DATA / "orb_recap_latest.txt"
ET = ZoneInfo("America/New_York")
FIELDS = ["trade_id", "strategy", "ticker", "side", "entry", "stop",
          "entry_time", "exit_price", "exit_reason", "outcome_r", "close_time",
          "shares", "risk_dollars", "dollar_pnl"]


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
        if tid and tid not in op and not in_csv(tid):
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
    closed = []
    for tid in list(op.keys()):
        rec = op[tid]
        df = today_5m(rec["ticker"], today)
        if df is None or df.empty:
            continue
        fut = df[df.index > pd.Timestamp(rec["entry_time"])]
        side, entry, stop = rec["side"], float(rec["entry"]), float(rec["stop"])
        risk = abs(entry - stop)
        outcome = reason = exitpx = None
        for _, b in fut.iterrows():
            if side == "LONG" and float(b["Low"]) <= stop:
                outcome, reason, exitpx = -1.0, "stop", stop; break
            if side == "SHORT" and float(b["High"]) >= stop:
                outcome, reason, exitpx = -1.0, "stop", stop; break
        if outcome is None and eod and not fut.empty:
            close = float(fut["Close"].iloc[-1])
            exitpx, reason = close, "eod"
            outcome = (close - entry) / risk if side == "LONG" else (entry - close) / risk if risk > 0 else 0
        if outcome is not None:
            row = {k: rec.get(k, "") for k in ["trade_id", "strategy", "ticker", "side", "entry", "stop", "entry_time"]}
            row.update(exit_price=round(exitpx, 2), exit_reason=reason,
                       outcome_r=round(outcome, 3),
                       close_time=datetime.now(ET).isoformat(timespec="seconds"),
                       shares=rec.get("shares", ""), risk_dollars=rec.get("risk_dollars", ""),
                       dollar_pnl=realized_dollars(rec, outcome, entry, stop))
            closed.append(row); del op[tid]
    if closed:
        append_csv(closed)
    return closed


def append_csv(rows):
    DATA.mkdir(parents=True, exist_ok=True)
    old_rows, migrate = [], False
    if CSV_PATH.exists():
        with CSV_PATH.open(newline="") as f:
            r = csv.DictReader(f)
            if r.fieldnames != FIELDS:   # old schema -> rewrite under new header
                migrate = True
                old_rows = list(r)
    if not CSV_PATH.exists() or migrate:
        with CSV_PATH.open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS)
            w.writeheader()
            for r in old_rows + rows:
                w.writerow({k: r.get(k, "") for k in FIELDS})
    else:
        with CSV_PATH.open("a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS)
            for r in rows:
                w.writerow({k: r.get(k, "") for k in FIELDS})


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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--eod", action="store_true")
    args = ap.parse_args()
    today = datetime.now(ET).date()
    op = load_open()
    ingest(today, op)
    closed = evaluate(today, op, eod=args.eod)
    DATA.mkdir(parents=True, exist_ok=True)
    OPEN_PATH.write_text(json.dumps(op, indent=2))
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
