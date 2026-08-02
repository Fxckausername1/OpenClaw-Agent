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
log = get_logger("eval")  # shared with orb_paper_eval.py -> logs/eval.log

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
OPEN_PATH = DATA / "paper_open.json"
LOCK_PATH = DATA / ".paper_eval.lock"
TERMINAL_PATH = DATA / "paper_terminal.jsonl"
CSV_PATH = DATA / "paper_trades.csv"
RECAP_PATH = DATA / "paper_recap_latest.txt"
ET = ZoneInfo("America/New_York")
REGIME_VALID_FROM = "2026-07-20"  # pre-fix tags are contaminated; plain cohort remains valid

CSV_FIELDS = ["trade_id", "ticker", "side", "entry", "stop", "t1", "t2",
              "planned_rr", "entry_time", "exit_price", "exit_reason",
              "outcome_r", "close_time", "strategy",
              "shares", "risk_dollars", "dollar_pnl", "regime_ok",
              "net_r_6bp", "net_r_12bp"]


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
    """Atomic. A plain write_text truncates first, so a concurrent reader
    could land in the gap, hit JSONDecodeError, and silently return {} --
    dropping every open position."""
    atomic_write_json(OPEN_PATH, o)


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
        if not tid or tid in open_pos or in_csv(tid) or tid in terminal_ids(TERMINAL_PATH):
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
    """Advance each resting order. A signal only becomes a trade once a LATER
    bar touches its limit -- previously this loop assumed the fill and tested
    stop/t1/EOD immediately, so it scored outcomes for orders Alpaca never
    filled."""
    closed, never = [], []
    for tid in list(open_pos.keys()):
        rec = open_pos[tid]
        df = get_today_5m(rec["ticker"], today)
        if df is None or df.empty:
            continue

        try:
            res = advance_boundary_limit(
                rec, df, eod=eod, target=float(rec["t1"]))
        except Exception as exc:  # noqa: BLE001 -- one bad record must not
            # abort the whole book; record and move on.
            log.error("evaluate failed for {}: {}", tid, exc)
            continue

        state = res["state"]
        if state == STATE_NEVER_FILLED:
            # Terminal, but NOT a trade. Recorded separately so a stale
            # trigger file cannot re-ingest and score it tomorrow.
            append_terminal(TERMINAL_PATH, rec, state,
                            datetime.now(ET).isoformat(timespec="seconds"))
            never.append(tid)
            del open_pos[tid]
            continue
        if state != STATE_FILLED_CLOSED:
            continue          # pending_entry or filled_open -- still working

        entry = float(rec["entry"]); stop = float(rec["stop"])
        outcome = round(float(res["outcome_r"]), 3)
        row = {k: rec.get(k, "") for k in
               ["trade_id", "ticker", "side", "entry", "stop", "t1", "t2",
                "planned_rr", "entry_time"]}
        row.update(exit_price=round(float(res["exit_price"]), 2),
                   exit_reason=res["exit_reason"],
                   outcome_r=outcome,
                   close_time=datetime.now(ET).isoformat(timespec="seconds"),
                   strategy=rec.get("strategy", "mr_z2.0"),
                   shares=rec.get("shares", ""), risk_dollars=rec.get("risk_dollars", ""),
                   dollar_pnl=realized_dollars(rec, outcome, entry, stop),
                   regime_ok=rec.get("regime_ok", ""),
                   net_r_6bp=net_r(outcome, entry, stop, 6),
                   net_r_12bp=net_r(outcome, entry, stop, 12))
        closed.append(row)
        del open_pos[tid]

    if closed:
        append_csv(closed)
    if never:
        log.info("paper_eval: {} never_filled (no limit touch)", len(never))
    return closed


def append_csv(rows):
    """Append, refusing any trade_id already in the ledger.

    The previous version had NO trade_id check, so a resurrected position
    could be closed and written a second time -- which is exactly how the 34
    duplicate rows (+34.779R) got there."""
    DATA.mkdir(parents=True, exist_ok=True)
    written = append_rows_dedup(CSV_PATH, CSV_FIELDS, rows)
    if written != len(rows):
        log.warning("append_csv: {}/{} rows already present, skipped",
                    len(rows) - written, len(rows))
    return written


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
            entry_date = str(row.get("entry_time") or "")[:10]
            if (entry_date >= REGIME_VALID_FROM and
                    str(row.get("regime_ok", "")).strip().lower() in ("true", "1")):
                g_r.append(r); g_d.append(d)
    return _stats(p_r, p_d), _stats(g_r, g_d)


def backfill_friction():
    """One-time migration (2026-07-19, Tier-2 audit finding #10): add net_r_6bp/net_r_12bp
    to existing CSV rows logged before those fields existed. Pure arithmetic on already-
    present columns (outcome_r, entry, stop) -- no API calls, no bar fetches needed. Backs
    up the CSV first (data/paper_trades.csv.bak_20260719_frictioncols, only if that backup
    doesn't already exist), then rewrites atomically (temp file + Path.replace), same
    discipline as scanner_outcome_tracker.py's backfill_early_checkpoints(). Idempotent:
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
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in CSV_FIELDS})
    tmp.replace(CSV_PATH)
    print(f"backfill complete: {updated}/{len(need)} rows computed, {len(rows)} total rows rewritten atomically")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--eod", action="store_true")
    ap.add_argument("--backfill-friction", action="store_true",
                    help="one-time migration: add net_r_6bp/net_r_12bp to existing "
                         "paper_trades.csv rows that predate these fields (2026-07-19, "
                         "Tier-2 audit finding #10). Pure arithmetic on outcome_r/entry/stop, "
                         "backs up the CSV first, rewrites atomically.")
    args = ap.parse_args()

    if args.backfill_friction:
        with evaluator_lock(LOCK_PATH):
            backfill_friction()
        return

    today = datetime.now(ET).date()
    # ONE lock around load->ingest->evaluate->save. Taken here rather than in
    # a wrapper because three cron paths reach this code under three
    # different wrapper locks, which is why they never actually excluded
    # each other.
    with evaluator_lock(LOCK_PATH):
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
