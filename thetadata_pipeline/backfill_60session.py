"""BT-2 60-session real SPY/QQQ backfill -- BT0_CHARTER.md Section 6's
scaling trigger ("freezing the validation/test date-range split... once
the development window reaches a real, pre-agreed size, proposed: 60 real
PASS-gated trading sessions").

Reuses bt1_pilot.py's exact proven pull mechanics (wide 6% strike window,
calendar-aware session selection via the real Alpaca trading calendar,
manifest grading via bt1_manifest.py) at 12x the pilot's scale, with two
real additions bt1_pilot.py deliberately did NOT need for a disposable
5-session pilot:

1. Persistence: bt1_pilot.py grades a pull's QUALITY but discards the
   actual classified trade rows per chunk (it only needs counts/stats to
   grade a session -- "resumable at the chunk level would be possible but
   is deliberately NOT done here"). BT-2's simulator needs the real
   trade-level data to run against, so this module persists each chunk's
   classified rows as an append-only parquet partition -- same atomic-
   write pattern as collector.append_raw, but under this module's OWN
   data/thetadata/backfill_60session/raw/ directory, never collector.py's
   shared RAW_DIR (that directory has its own 14-day rotation policy,
   which would be free to delete data this backfill needs to keep).

2. Session-level resumability: a 60-session x 2-symbol pull (120 units of
   work, each involving dozens of real ThetaData calls) is not the "cheap
   enough to just re-run from scratch" case bt1_pilot.py's own docstring
   describes for its 5-session pilot -- a real network interruption
   partway through should resume from the next incomplete unit, not
   restart from session 1.

Isolation rule (same as the rest of this package): never touches
live_gex_snapshot.json, vex_history.json, iv_intraday_state.json, or
anything backfill.py/collector.py already own.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
from pathlib import Path
from typing import Optional

import pandas as pd

from .backfill import MIN_FREE_BACKFILL_DISK_MB, MIN_FREE_RAM_MB, _free_ram_mb, _session_chunks
from .bt1_manifest import build_session_manifest, core_hour_chunks, overall_summary
from .bt1_pilot import WIDE_STRIKE_PCT, _thetadata_time, expected_bar_count, fetch_underlying_bars, most_recent_complete_sessions
from .client import ThetaDataUnavailable, bounded_call, get_client
from .collector import _atomic_write_json, _free_disk_mb
from .contracts import active_expirations, get_spot_price, strike_window
from .normalize import classify_trades, coverage_stats, dedup_trades
from .schemas import contract_id, normalize_right, occ_symbol

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "thetadata"
BACKFILL_DIR = DATA / "backfill_60session"
RAW_DIR = BACKFILL_DIR / "raw"
MANIFEST_PATH = BACKFILL_DIR / "backfill_60session_manifest.json"
PROGRESS_PATH = BACKFILL_DIR / "backfill_60session_progress.json"

logger = logging.getLogger("thetadata_pkg.backfill_60session")


def _load_progress(path: Path = PROGRESS_PATH) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            pass
    return {"completed": []}


def _save_progress(progress: dict, path: Path = PROGRESS_PATH) -> None:
    _atomic_write_json(path, progress)


def _raw_partition_dir(symbol: str, day: dt.date, raw_dir: Path = RAW_DIR) -> Path:
    d = raw_dir / symbol / day.isoformat()
    d.mkdir(parents=True, exist_ok=True)
    return d


def _append_raw_chunk(symbol: str, day: dt.date, classified: pd.DataFrame, raw_dir: Path = RAW_DIR) -> int:
    """Same append-only atomic part-file pattern as collector.append_raw,
    scoped to this module's own directory (never collector.py's shared
    RAW_DIR -- see module docstring)."""
    if classified is None or classified.empty:
        return 0
    if _free_disk_mb(ROOT) < MIN_FREE_BACKFILL_DISK_MB:
        logger.warning("disk pressure (<%dMB free) -- skipping raw persistence this chunk", MIN_FREE_BACKFILL_DISK_MB)
        return 0
    part_dir = _raw_partition_dir(symbol, day, raw_dir)
    part_name = f"part-{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S%f')}-{os.getpid()}.parquet"
    tmp = part_dir / f".tmp{part_name}"
    classified.to_parquet(tmp, index=False)
    os.replace(tmp, part_dir / part_name)
    return len(classified)


def pull_and_persist_session(
    symbol: str, session: dict, strike_pct: float = WIDE_STRIKE_PCT, raw_dir: Path = RAW_DIR,
    backfill_dir: Path = BACKFILL_DIR,
) -> dict:
    """Same per-chunk pull/grade loop as bt1_pilot.pull_bt1_session, with
    real persistence added: each chunk's classified rows are appended to
    disk (never discarded), and EOD Greeks/OI are persisted per session
    (matching backfill.py's own EOD-Greeks convention, TD-5) rather than
    only counted."""
    day = dt.date.fromisoformat(session["date"])
    started_at = dt.datetime.now(dt.timezone.utc).isoformat()
    thetadata_calls = 0
    thetadata_errors = 0
    unrecoverable_errors: list = []
    empty_core_chunks: list = []

    client = get_client()
    expirations = [e for e in active_expirations(symbol, day) if e >= day][:1]
    trade_rows_total = 0
    greeks_rows_total = 0
    dup_rows_total = 0
    directional_total = 0
    ambiguous_total = 0
    crossed_total = 0
    classified_total = 0
    occ_symbols_seen: set = set()
    persisted_rows = 0
    exp = None
    strike_count = 1

    if not expirations:
        unrecoverable_errors.append("no active expiration found for this session")
    else:
        exp = expirations[0]
        spot = get_spot_price(symbol)
        strikes = strike_window(symbol, exp, spot, pct=strike_pct)
        strike_count = max(len(strikes) // 2, 1) + 2

        for chunk_start, chunk_end in _session_chunks():
            label = f"{chunk_start.isoformat(timespec='minutes')}-{chunk_end.isoformat(timespec='minutes')}"
            free_mb = _free_ram_mb()
            if free_mb < MIN_FREE_RAM_MB:
                unrecoverable_errors.append(f"aborted at chunk {label}: only {free_mb:.0f}MB RAM free")
                break
            free_disk_mb = _free_disk_mb(backfill_dir if backfill_dir.exists() else ROOT)
            if free_disk_mb < MIN_FREE_BACKFILL_DISK_MB:
                unrecoverable_errors.append(f"aborted at chunk {label}: only {free_disk_mb:.0f}MB disk free")
                break

            chunk_had_trades = False
            try:
                thetadata_calls += 1
                trades = bounded_call(
                    client.option_history_trade_quote,
                    symbol=symbol, expiration=exp, date=day,
                    strike="*", right="both", strike_range=strike_count,
                    start_time=_thetadata_time(chunk_start), end_time=_thetadata_time(chunk_end),
                )
            except ThetaDataUnavailable as exc:
                thetadata_errors += 1
                unrecoverable_errors.append(f"trade_quote chunk {label} failed: {exc}")
                trades = None

            if trades is not None and not trades.empty:
                chunk_had_trades = True
                before = len(trades)
                deduped = dedup_trades(trades)
                dup_rows_total += before - len(deduped)
                classified = classify_trades(deduped)
                trade_rows_total += len(classified)
                classified_total += len(classified)
                stats = coverage_stats(classified)
                if stats["trade_classification_coverage"] is not None:
                    directional_total += round(stats["trade_classification_coverage"] * len(classified))
                if stats["ambiguous_trade_fraction"] is not None:
                    ambiguous_total += round(stats["ambiguous_trade_fraction"] * len(classified))
                crossed_total += int((classified["excluded_reason"] == "crossed_or_locked_market").sum())
                for row in classified.itertuples():
                    occ_symbols_seen.add(occ_symbol(symbol, exp, row.strike, row.right))
                persisted_rows += _append_raw_chunk(symbol, day, classified, raw_dir)
                del trades, deduped, classified

            if not chunk_had_trades:
                empty_core_chunks.append(label)

        empty_core_chunks = core_hour_chunks(empty_core_chunks)

    oi_contracts = 0
    oi_map: dict = {}
    if exp is not None:
        try:
            thetadata_calls += 1
            oi_df = bounded_call(
                client.option_history_open_interest,
                symbol=symbol, expiration=exp, date=day, strike="*", right="both",
                strike_range=strike_count,
            )
            if oi_df is not None and not oi_df.empty:
                oi_contracts = len(oi_df)
                for row in oi_df.itertuples():
                    cid = contract_id(symbol, dt.date.fromisoformat(str(row.expiration)[:10]), row.strike, row.right)
                    oi_map[cid] = float(row.open_interest)
        except ThetaDataUnavailable as exc:
            thetadata_errors += 1
            unrecoverable_errors.append(f"open_interest failed: {exc}")

    if oi_map:
        backfill_dir.mkdir(parents=True, exist_ok=True)
        _atomic_write_json(backfill_dir / f"oi_{symbol}_{day.isoformat()}.json",
                            {"oi": oi_map, "oi_as_of_session": day.isoformat()})

    iv_rows: list = []
    if exp is not None:
        try:
            thetadata_calls += 1
            iv_df = bounded_call(
                client.option_history_greeks_eod,
                symbol=symbol, expiration=exp, start_date=day, end_date=day,
                strike="*", right="both", strike_range=strike_count,
            )
            if iv_df is not None and not iv_df.empty:
                greeks_rows_total = len(iv_df)
                for row in iv_df.itertuples():
                    iv_rows.append({
                        "strike": float(row.strike), "right": normalize_right(row.right),
                        "implied_vol": float(row.implied_vol) if row.implied_vol is not None else None,
                        "delta": float(row.delta) if row.delta is not None else None,
                    })
        except ThetaDataUnavailable as exc:
            thetadata_errors += 1
            unrecoverable_errors.append(f"EOD greeks pull failed: {exc}")

    if iv_rows:
        backfill_dir.mkdir(parents=True, exist_ok=True)
        _atomic_write_json(backfill_dir / f"iv_{symbol}_{day.isoformat()}.json",
                            {"rows": iv_rows, "session_date": day.isoformat()})

    bars = fetch_underlying_bars(symbol, day, session["open"], session["close"])
    expected_bars = expected_bar_count(session["open"], session["close"])

    completed_at = dt.datetime.now(dt.timezone.utc).isoformat()
    coverage = (directional_total / classified_total) if classified_total else None
    ambiguous_fraction = (ambiguous_total / classified_total) if classified_total else None
    crossed_fraction = (crossed_total / classified_total) if classified_total else None

    manifest_row = build_session_manifest(
        symbol=symbol, date=session["date"], calendar=session,
        requested={"expiration": exp.isoformat() if exp else None, "strike_pct": strike_pct,
                   "option_trade_quote_chunks": len(_session_chunks())},
        received={"option_trade_quote_rows": trade_rows_total, "option_greeks_rows": greeks_rows_total,
                  "open_interest_contracts": oi_contracts, "underlying_bars_count": len(bars),
                  "underlying_bars_expected": expected_bars, "distinct_occ_symbols": len(occ_symbols_seen)},
        missing={"empty_trade_quote_core_chunks": empty_core_chunks, "unrecoverable_errors": unrecoverable_errors},
        rejected={"duplicate_trade_rows_dropped": dup_rows_total, "crossed_or_locked_quote_rows": crossed_total},
        retrieval={"started_at": started_at, "completed_at": completed_at},
        response_metadata={"thetadata_calls": thetadata_calls, "thetadata_errors": thetadata_errors, "alpaca_calls": 2},
        integrity={"trade_classification_coverage": coverage, "ambiguous_trade_fraction": ambiguous_fraction,
                   "crossed_market_fraction": crossed_fraction, "timezone_normalized": True},
    )
    manifest_row["persisted_raw_rows"] = persisted_rows
    return manifest_row


def run_60session_backfill(
    symbols: tuple = ("SPY", "QQQ"), sessions: int = 60,
    manifest_path: Path = MANIFEST_PATH, progress_path: Path = PROGRESS_PATH, raw_dir: Path = RAW_DIR,
    before: Optional[dt.date] = None,
) -> dict:
    """Resumable across (symbol, session) units -- a real interruption
    resumes from the next incomplete unit, never restarts from session 1.
    Writes one consolidated, appendable manifest after EVERY unit (not
    just at the end), so a killed process still leaves a manifest
    reflecting real progress.

    `before` (2026-07-29 addition): defaults to None, which preserves the
    original behavior exactly -- most_recent_complete_sessions(sessions)
    resolves to "the N most recent complete sessions as of right now."
    Every existing caller/test that doesn't pass `before` keeps working
    unchanged. Passing an explicit date instead selects "the N real trading
    sessions immediately before `before`" -- e.g. before=date(2026, 4, 29)
    extends the real dataset BACKWARD from the current earliest session,
    rather than forward from today. This closes a real gap found live: this
    same script, re-launched on a later day with no `before`, silently
    shifted its target window FORWARD (picking up new recent sessions)
    instead of ever reaching further back, because most_recent_complete_
    sessions() has no memory of what a previous run already covered."""
    backfill_dir = manifest_path.parent
    backfill_dir.mkdir(parents=True, exist_ok=True)
    raw_dir.mkdir(parents=True, exist_ok=True)
    target_sessions = most_recent_complete_sessions(sessions, before=before)
    if len(target_sessions) < sessions:
        logger.warning("requested %d sessions but the calendar only yielded %d", sessions, len(target_sessions))

    progress = _load_progress(progress_path)
    existing_manifest: dict = {"sessions": []}
    if manifest_path.exists():
        try:
            existing_manifest = json.loads(manifest_path.read_text())
        except Exception:
            pass
    session_manifests = existing_manifest.get("sessions", [])
    manifest = existing_manifest

    for symbol in symbols:
        for session in target_sessions:
            key = f"{symbol}:{session['date']}"
            if key in progress["completed"]:
                continue
            logger.info("60-session backfill: pulling %s %s", symbol, session["date"])
            row = pull_and_persist_session(symbol, session, raw_dir=raw_dir, backfill_dir=backfill_dir)
            # 2026-07-29: a retried (symbol, date) -- e.g. re-pulling a session after removing
            # it from progress["completed"] to fix a RAM-guard FAIL -- must REPLACE its stale
            # entry here, not accumulate alongside it. Found live: two real RAM-guard FAILs
            # were retried and passed, but the manifest kept both the old FAIL and new PASS
            # rows for the same session, so overall_summary() kept counting the dead FAIL.
            session_manifests = [s for s in session_manifests
                                  if not (s.get("symbol") == symbol and s.get("date") == session["date"])]
            session_manifests.append(row)
            progress["completed"].append(key)
            _save_progress(progress, progress_path)
            manifest = {
                "schema_version": "bt2-backfill-60session-1.0",
                "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "backfill_scope": {"symbols": list(symbols), "sessions_requested": sessions,
                                   "sessions_found": len(target_sessions),
                                   "before": before.isoformat() if before else None},
                "sessions": session_manifests,
                "overall": overall_summary(session_manifests),
            }
            _atomic_write_json(manifest_path, manifest)

    return manifest


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default="SPY,QQQ", help="comma-separated, e.g. QQQ or SPY,QQQ")
    ap.add_argument("--sessions", type=int, default=60)
    ap.add_argument("--before", default=None, help="YYYY-MM-DD; sessions pulled are the N most recent "
                     "complete sessions strictly before this date. Omit for the original 'most recent "
                     "N as of today' behavior.")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO)
    result = run_60session_backfill(
        symbols=tuple(s.strip() for s in args.symbols.split(",") if s.strip()),
        sessions=args.sessions,
        before=dt.date.fromisoformat(args.before) if args.before else None,
    )
    print(json.dumps(result["overall"], indent=2))
