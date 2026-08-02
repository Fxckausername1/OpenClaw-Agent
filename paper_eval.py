#!/usr/bin/env python3
"""Paper-trade logger/evaluator for the mean-reversion strategy.

Lifecycle (no real money, builds an out-of-sample track record):
1. Ingest TRIGGER records the scanner logs to data/mr_triggers_<date>.jsonl and
   open a paper position for each (data/paper_open.json).
2. Evaluate each open position against live 5-min bars AFTER its entry time:
   - SHORT: stop hit if a later bar's High >= stop (loss, -1R, checked first);
            target hit if a later bar's Low <= T1 (win, +planned R toward VWAP).
   - LONG : mirror.
3. With --eod, force-close any still-open positions at the session's last price
   and emit a recap + running track-record stats (wrapper sends it to Telegram).

Closed trades are appended permanently to data/paper_trades.csv.
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
log = get_logger("eval")  # shared with orb_paper_eval.py -> logs/eval.log

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
OPEN_PATH = DATA / "paper_open.json"
CSV_PATH = DATA / "paper_trades.csv"
RECAP_PATH = DATA / "paper_recap_latest.txt"
ET = ZoneInfo("America/New_York")

CSV_FIELDS = ["trade_id", "ticker", "side", "entry", "stop", "t1", "t2",
              "planned_rr", "entry_time", "exit_price", "exit_reason",
              "outcome_r", "close_time", "strategy",
              "shares", "risk_dollars", "dollar_pnl", "regime_ok"]


def money(d):
    return f"+${d:,.2f}" if d >= 0 else f"-${abs(d):,.2f}"


def rd(r, d):
    """Format an R-multiple with its realized dollars, e.g. '+1.50R (+$7.50)'."""
    return f"{r:+.2f}R ({money(d)})"


def realized_dollars(rec, outcome_r, entry, stop):
    """Realized $ P&L = R * risk_dollars (identically = shares * per-share price delta).
    Falls back to shares*|entry-stop| if a record predates the sizing fields, else $0."""
    risk_dollars = float(rec.get("risk_dollars") or 0)
    if not risk_dollars and rec.get("shares"):
        risk_dollars = int(rec["shares"]) * abs(entry - stop)
    return round(outcome_r * risk_dollars, 2)


def triggers_path(d):
    return DATA / f"mr_triggers_{d.isoformat()}.jsonl"


def load_open():
    if OPEN_PATH.exists():
        try:
            return json.loads(OPEN_PATH.read_text())
        except Exception:
            return {}
    return {}


def save_open(o):
    DATA.mkdir(parents=True, exist_ok=True)
    OPEN_PATH.write_text(json.dumps(o, indent=2))


def in_csv(tid):
    if not CSV_PATH.exists():
        return False
    try:
        with CSV_PATH.open() as f:
            return any(row.get("trade_id") == tid for row in csv.DictReader(f))
    except Exception:
        return False


def ingest(today, open_pos):
    p = triggers_path(today)
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
        if not tid or tid in open_pos or in_csv(tid):
            continue
        open_pos[tid] = rec


def get_today_5m(ticker, today):
    # Alpaca real-time (yfinance fallback) via the scanner's shared fetch, so paper
    # fills are evaluated on the SAME feed that produced the signal.
    try:
        df = mrs.get_5m_data(ticker, days=2)
        if df is None or df.empty:
            return None
        if df.index.tz is not None:
            df.index = df.index.tz_convert(ET).tz_localize(None)  # naive ET to match entry_time
        return df[df.index.date == today]
    except Exception:
        return None


def evaluate(today, open_pos, eod=False):
    closed = []
    for tid in list(open_pos.keys()):
        rec = open_pos[tid]
        df = get_today_5m(rec["ticker"], today)
        if df is None or df.empty:
            continue
        entry_ts = pd.Timestamp(rec["entry_time"])
        future = df[df.index > entry_ts]
        side = rec["side"]
        entry = float(rec["entry"]); stop = float(rec["stop"]); t1 = float(rec["t1"])
        risk = abs(entry - stop)
        outcome = reason = exit_px = None

        for _, b in future.iterrows():
            hi, lo = float(b["High"]), float(b["Low"])
            if side == "SHORT":
                if hi >= stop:
                    outcome, reason, exit_px = -1.0, "stop", stop
                    break
                if lo <= t1:
                    outcome, reason, exit_px = ((entry - t1) / risk if risk > 0 else 0), "t1", t1
                    break
            else:
                if lo <= stop:
                    outcome, reason, exit_px = -1.0, "stop", stop
                    break
                if hi >= t1:
                    outcome, reason, exit_px = ((t1 - entry) / risk if risk > 0 else 0), "t1", t1
                    break

        if outcome is None and eod and not future.empty:
            close = float(future["Close"].iloc[-1])
            exit_px, reason = close, "eod"
            outcome = ((entry - close) if side == "SHORT" else (close - entry)) / risk if risk > 0 else 0

        if outcome is not None:
            row = {k: rec.get(k, "") for k in
                   ["trade_id", "ticker", "side", "entry", "stop", "t1", "t2", "planned_rr", "entry_time"]}
            row.update(exit_price=round(exit_px, 2), exit_reason=reason,
                       outcome_r=round(outcome, 3),
                       close_time=datetime.now(ET).isoformat(timespec="seconds"),
                       strategy=rec.get("strategy", "mr_z2.0"),
                       shares=rec.get("shares", ""), risk_dollars=rec.get("risk_dollars", ""),
                       dollar_pnl=realized_dollars(rec, outcome, entry, stop),
                       regime_ok=rec.get("regime_ok", ""))
            closed.append(row)
            del open_pos[tid]

    if closed:
        append_csv(closed)
    return closed


def append_csv(rows):
    DATA.mkdir(parents=True, exist_ok=True)
    old_rows, migrate = [], False
    if CSV_PATH.exists():
        with CSV_PATH.open(newline="") as f:
            r = csv.DictReader(f)
            if r.fieldnames != CSV_FIELDS:   # old schema -> rewrite under new header
                migrate = True
                old_rows = list(r)
    if not CSV_PATH.exists() or migrate:
        with CSV_PATH.open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
            w.writeheader()
            for r in old_rows + rows:
                w.writerow({k: r.get(k, "") for k in CSV_FIELDS})
    else:
        with CSV_PATH.open("a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
            for r in rows:
                w.writerow({k: r.get(k, "") for k in CSV_FIELDS})


def _stats(rs, ds):
    if not rs:
        return None
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]
    pf = (sum(wins) / abs(sum(losses))) if losses and sum(losses) != 0 else float("inf")
    return dict(n=len(rs), wr=len(wins) / len(rs) * 100,
                exp=sum(rs) / len(rs), total=sum(rs), pf=pf,
                total_dollars=sum(ds), exp_dollars=sum(ds) / len(rs))


def track_record(strategy=None):
    """Overall track record (R + realized $), or one strategy cohort if given."""
    if not CSV_PATH.exists():
        return None
    rs, ds = [], []
    with CSV_PATH.open() as f:
        for row in csv.DictReader(f):
            if strategy is not None and (row.get("strategy") or "mr_z2.0") != strategy:
                continue
            try:
                rs.append(float(row["outcome_r"]))
                ds.append(float(row.get("dollar_pnl") or 0))
            except Exception:
                pass
    return _stats(rs, ds)


def strategies_seen():
    """Distinct strategy tags present in the closed-trade CSV (legacy rows -> mr_z2.0)."""
    if not CSV_PATH.exists():
        return []
    seen = []
    with CSV_PATH.open() as f:
        for row in csv.DictReader(f):
            s = row.get("strategy") or "mr_z2.0"
            if s not in seen:
                seen.append(s)
    return seen


def closed_today(today):
    """Trades whose close_time falls on `today` (ET), read from the CSV — so the EOD
    recap reflects the WHOLE day, not just the final run's closures (intraday runs
    close most trades before the --eod run, leaving its `closed` list empty)."""
    if not CSV_PATH.exists():
        return []
    out = []
    with CSV_PATH.open() as f:
        for row in csv.DictReader(f):
            try:
                if pd.Timestamp(row.get("close_time", "")).date() == today:
                    out.append(row)
            except Exception:
                continue
    return out


def regime_ab(strategy="mr_z1.5"):
    """Carry-both A/B: PLAIN = all `strategy` trades; REGIME = the subset whose
    regime_ok is true (passed the 1h-EMA rotational filter). Returns (plain, regime)
    stats so the month can compare quality-vs-volume on the SAME live triggers."""
    if not CSV_PATH.exists():
        return None, None
    p_r, p_d, g_r, g_d = [], [], [], []
    with CSV_PATH.open() as f:
        for row in csv.DictReader(f):
            if (row.get("strategy") or "mr_z2.0") != strategy:
                continue
            try:
                r = float(row["outcome_r"]); d = float(row.get("dollar_pnl") or 0)
            except Exception:
                continue
            p_r.append(r); p_d.append(d)
            if str(row.get("regime_ok", "")).strip().lower() in ("true", "1"):
                g_r.append(r); g_d.append(d)
    return _stats(p_r, p_d), _stats(g_r, g_d)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--eod", action="store_true")
    args = ap.parse_args()

    today = datetime.now(ET).date()
    open_pos = load_open()
    ingest(today, open_pos)
    closed = evaluate(today, open_pos, eod=args.eod)
    save_open(open_pos)

    if args.eod:
        st = track_record()
        lines = [f"\U0001F4D2 Paper-trade recap {today.strftime('%b %d')}"]
        # read the whole day's closes from the CSV (not just this run's `closed`),
        # so the recap is accurate even though intraday runs close most trades earlier
        ct_rows = closed_today(today)
        if ct_rows:
            day_r = sum(float(c["outcome_r"]) for c in ct_rows)
            day_d = sum(float(c.get("dollar_pnl") or 0) for c in ct_rows)
            w = sum(1 for c in ct_rows if float(c["outcome_r"]) > 0)
            lines.append(f"Closed today: {len(ct_rows)} ({w}W/{len(ct_rows) - w}L) {rd(day_r, day_d)}")
            for c in ct_rows:
                lines.append(f"  {c['ticker']} {c['side']} "
                             f"{rd(float(c['outcome_r']), float(c.get('dollar_pnl') or 0))} ({c['exit_reason']})")
        else:
            lines.append("No paper trades closed today.")
        if open_pos:
            lines.append(f"Still open (carried): {len(open_pos)}")
        if st:
            pf = "inf" if st["pf"] == float("inf") else f"{st['pf']:.2f}"
            lines.append(f"Track record (all): {st['n']} trades | win {st['wr']:.0f}% | "
                         f"exp {st['exp']:+.2f}R | total {st['total']:+.1f}R ({money(st['total_dollars'])}) | PF {pf}")
        # per-strategy split so the new z=1.5 cohort is visible on its own
        seen = strategies_seen()
        if len(seen) > 1:
            for sname in seen:
                ss = track_record(strategy=sname)
                if ss:
                    spf = "inf" if ss["pf"] == float("inf") else f"{ss['pf']:.2f}"
                    lines.append(f"  - {sname}: {ss['n']} tr | win {ss['wr']:.0f}% | "
                                 f"exp {ss['exp']:+.2f}R | total {ss['total']:+.1f}R ({money(ss['total_dollars'])}) | PF {spf}")
        # carry-both A/B: plain z=1.5 vs the regime-filtered subset (same live triggers)
        plain, regime = regime_ab("mr_z1.5")
        if plain and plain["n"] > 0:
            lines.append("⚖️ A/B carry-both (mr_z1.5):")
            lines.append(f"  PLAIN : {plain['n']} tr | exp {plain['exp']:+.2f}R | "
                         f"total {plain['total']:+.1f}R ({money(plain['total_dollars'])})")
            if regime and regime["n"] > 0:
                lines.append(f"  REGIME: {regime['n']} tr | exp {regime['exp']:+.2f}R | "
                             f"total {regime['total']:+.1f}R ({money(regime['total_dollars'])})")
            else:
                lines.append("  REGIME: 0 trades passed the filter yet")
        msg = "\n".join(lines)
        RECAP_PATH.write_text(msg)
        log.info(msg)
    else:
        log.info(f"paper_eval: {len(closed)} closed, {len(open_pos)} open")


if __name__ == "__main__":
    main()
