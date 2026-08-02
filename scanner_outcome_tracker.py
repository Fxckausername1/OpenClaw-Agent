#!/usr/bin/env python3
"""Outcome tracker for the discretionary scanners (2026-07-01) -- closes the loop on
premarket_scanner.py and continuation_scanner.py: for a given date's candidate output,
fetches what actually happened by end of day and appends one row per candidate to a
persistent ledger (data/scanner_outcomes.jsonl). This is the ONLY writer of that ledger
-- never hand-edit it, same discipline as paper_trades.csv (see the project's existing
paper-tracking-integrity rule).

Why this exists: both scanners are new and unvalidated (no stored dataset existed to
backtest against before shipping either one). Manually recalling "was the bias right"
doesn't scale and doesn't survive being forgotten between sessions -- this makes hit
rate a queryable fact instead of an anecdote.

For continuation candidates still in the "watching" bucket at scan time, this re-runs
continuation_scanner.detect_phase_d() (imported directly, not reimplemented) against
the FULL day's bars, so "did it eventually break out" uses the exact same definition
the live scanner uses -- not a looser proxy that would make the two disagree.

Run after market close for a stable EOD read:
    ./venv/bin/python scanner_outcome_tracker.py                 # track today
    ./venv/bin/python scanner_outcome_tracker.py --date 2026-07-01
    ./venv/bin/python scanner_outcome_tracker.py --summarize     # print aggregate stats, no tracking
"""
import json
import argparse
from pathlib import Path
from datetime import datetime, date as ddate, time as dtime
from zoneinfo import ZoneInfo

import continuation_scanner as cs
import wide_universe as wu
from log_setup import get_logger

log = get_logger("scanner_outcomes")

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
LEDGER = DATA / "scanner_outcomes.jsonl"
ET = ZoneInfo("America/New_York")
MARKET_CLOSE = dtime(16, 0)


def load_ledger_keys():
    """(date, scanner, ticker) tuples already logged -- re-running a date is safe, it
    only appends rows that aren't there yet."""
    keys = set()
    if LEDGER.exists():
        for line in LEDGER.read_text().splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
                keys.add((row["date"], row["scanner"], row["ticker"]))
            except Exception:
                continue
    return keys


def append_rows(rows):
    if not rows:
        return
    with LEDGER.open("a") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def track_premarket(target_date, bars_by_ticker, existing_keys):
    path = DATA / f"orb_premarket_{target_date.isoformat()}.json"
    if not path.exists():
        log.info(f"No premarket candidate file for {target_date} -- nothing to track.")
        return []
    candidates = json.loads(path.read_text())
    rows = []
    for c in candidates:
        tk = c["ticker"]
        key = (target_date.isoformat(), "premarket", tk)
        if key in existing_keys:
            continue
        df = bars_by_ticker.get(tk)
        if df is None:
            continue
        todays = df[df.index.date == target_date]
        if todays.empty:
            continue
        eod_close = float(todays["Close"].iloc[-1])
        pm_last = c.get("pm_last")
        if not pm_last:
            continue
        move_pct = (eod_close - pm_last) / pm_last * 100.0
        bias = c.get("bias")
        direction_correct = (eod_close > pm_last) if bias == "LONG" else (eod_close < pm_last)
        rows.append({
            "date": target_date.isoformat(), "scanner": "premarket", "ticker": tk,
            "bias": bias, "reference_price": pm_last, "eod_close": round(eod_close, 4),
            "move_pct": round(move_pct, 2), "direction_correct": bool(direction_correct),
            "gap_pct": c.get("gap_pct"), "pm_rvol": c.get("pm_rvol"),
            "gap_ratio": c.get("gap_ratio"), "rel_strength": c.get("rel_strength"),
        })
    return rows


def track_continuation(target_date, bars_by_ticker, existing_keys, params):
    path = DATA / f"continuation_{target_date.isoformat()}.json"
    if not path.exists():
        log.info(f"No continuation candidate file for {target_date} -- nothing to track.")
        return []
    data = json.loads(path.read_text())
    all_candidates = (data.get("watching") or []) + (data.get("fired") or [])
    rows = []
    for c in all_candidates:
        tk = c["ticker"]
        key = (target_date.isoformat(), "continuation", tk)
        if key in existing_keys:
            continue
        df = bars_by_ticker.get(tk)
        if df is None:
            continue
        todays = df[df.index.date == target_date]
        if todays.empty:
            continue
        eod_close = float(todays["Close"].iloc[-1])
        ref_price = c.get("last_price")

        already_fired = bool(c.get("phase_d_fired"))
        breakout_time, breakout_price = c.get("phase_d_time"), c.get("phase_d_price")
        if not already_fired and c.get("r_max") is not None:
            # Still consolidating as of the original scan -- check the FULL day to see if
            # it went on to break out later, using the scanner's own detection exactly.
            work = df.copy()
            work["Vol_Z"] = cs.compute_tod_vol_zscore(work, params["lookback_days_vol"])
            todays_full = work[work.index.date == target_date]
            post_morning = todays_full[todays_full.index.time > cs.PHASE_A_END]
            ts, px = cs.detect_phase_d(post_morning, c["r_max"], params)
            if ts is not None:
                already_fired, breakout_time, breakout_price = True, ts.strftime("%H:%M"), px

        move_pct = ((eod_close - ref_price) / ref_price * 100.0) if ref_price else None
        rows.append({
            "date": target_date.isoformat(), "scanner": "continuation", "ticker": tk,
            "bias": "LONG", "reference_price": ref_price, "eod_close": round(eod_close, 4),
            "move_pct": round(move_pct, 2) if move_pct is not None else None,
            "direction_correct": bool(eod_close > ref_price) if ref_price else None,
            "breakout_confirmed": already_fired, "breakout_time": breakout_time,
            "breakout_price": round(breakout_price, 4) if breakout_price is not None else None,
            "score": c.get("score"), "had_spring": c.get("has_spring"),
        })
    return rows


def summarize():
    if not LEDGER.exists():
        print("No outcomes logged yet.")
        return
    rows = [json.loads(l) for l in LEDGER.read_text().splitlines() if l.strip()]
    by_scanner = {}
    for r in rows:
        by_scanner.setdefault(r["scanner"], []).append(r)
    for scanner, rs in by_scanner.items():
        n = len(rs)
        correct = sum(1 for r in rs if r.get("direction_correct"))
        moves = [r["move_pct"] for r in rs if r.get("move_pct") is not None]
        avg_move = sum(moves) / len(moves) if moves else None
        line = f"{scanner}: n={n}  direction_correct={correct}/{n} ({correct / n * 100:.0f}%)"
        if avg_move is not None:
            line += f"  avg_move={avg_move:+.2f}%"
        print(line)
        if scanner == "continuation":
            fired = sum(1 for r in rs if r.get("breakout_confirmed"))
            print(f"  breakout eventually confirmed: {fired}/{n} ({fired / n * 100:.0f}%)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", type=str, default=None, help="YYYY-MM-DD; default = today")
    ap.add_argument("--summarize", action="store_true", help="print aggregate stats only, no tracking")
    args = ap.parse_args()

    if args.summarize:
        summarize()
        return

    now = datetime.now(ET)
    target_date = ddate.fromisoformat(args.date) if args.date else now.date()

    if target_date == now.date() and now.time() < MARKET_CLOSE:
        log.warning(f"Market hasn't closed yet ({now.strftime('%H:%M %Z')}) -- EOD close will "
                    f"reflect a still-moving price, not a settled one. Running anyway.")

    existing_keys = load_ledger_keys()

    pm_path = DATA / f"orb_premarket_{target_date.isoformat()}.json"
    cont_path = DATA / f"continuation_{target_date.isoformat()}.json"
    tickers = set()
    if pm_path.exists():
        tickers.update(c["ticker"] for c in json.loads(pm_path.read_text()))
    cont_data = None
    if cont_path.exists():
        cont_data = json.loads(cont_path.read_text())
        tickers.update(c["ticker"] for c in (cont_data.get("watching") or []) + (cont_data.get("fired") or []))

    if not tickers:
        log.info(f"No candidate files found for {target_date} -- nothing to track.")
        return

    params = cs.load_cont_params()
    # Needs the SAME lookback depth continuation_scanner.py itself uses for its 20-day
    # volume-Z baseline, regardless of how far back target_date is -- a small buffer past
    # target_date alone would starve compute_tod_vol_zscore of history and silently make
    # every Vol_Z (and therefore every re-checked breakout) NaN/undetected.
    fetch_days = max(params["fetch_calendar_days"], (now.date() - target_date).days + 5)
    log.info(f"Fetching {len(tickers)} tickers ({target_date}, {fetch_days}d window) for outcome tracking...")
    bars = wu.fetch_bars_batch(list(tickers), days=fetch_days)

    rows = track_premarket(target_date, bars, existing_keys)
    rows += track_continuation(target_date, bars, existing_keys, params)
    append_rows(rows)
    log.info(f"Logged {len(rows)} new outcome rows for {target_date} (ledger: {LEDGER}).")
    if rows:
        summarize()


if __name__ == "__main__":
    main()
