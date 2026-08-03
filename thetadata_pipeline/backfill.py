"""Bounded, resumable historical backfill for SPY/QQQ -- TD-STD Section 12.

Originally deliberately minimal for TD-1..TD-4 (trade_quote+OI only, enough
to validate the normalizer/aggregator/feature-engine logic against more
than one live session). TD-5 (2026-07-26) extended `backfill_symbol_date`
to also pull that day's EOD IV/Greeks (see the IV/Greeks block below) --
SPY/QQQ both list daily 0DTE expirations, confirmed live, so "that day's
front expiration" is simply the day itself, meaning IV history folds into
this same per-(symbol, date) unit of work rather than needing a separate
historical-expiration-enumeration pass. TD-5 also added a disk-space guard
to the per-chunk loop (reusing collector.py's existing `_free_disk_mb`,
just with a higher floor sized for a multi-day backfill rather than a
single live cycle). This still doesn't attempt the FULL BT-OPT Section 3
research architecture (point-in-time manifests, multi-expiration Greeks
history, etc.) -- that's BT-1 scope.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
from pathlib import Path
from typing import Optional

import pandas as pd

from .client import ThetaDataUnavailable, bounded_call, get_client
from .collector import _atomic_write_json, _free_disk_mb, append_raw
from .contracts import active_expirations, get_spot_price, strike_window
from .normalize import dedup_trades
from .schemas import contract_id, normalize_right

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "thetadata"
MANIFEST_PATH = DATA / "backfill_manifest.json"

logger = logging.getLogger("thetadata_pkg.backfill")


def _load_manifest() -> dict:
    if MANIFEST_PATH.exists():
        try:
            return json.loads(MANIFEST_PATH.read_text())
        except Exception:
            pass
    return {"completed": []}


def _save_manifest(manifest: dict) -> None:
    _atomic_write_json(MANIFEST_PATH, manifest)


def _trading_days_back(n: int, end: Optional[dt.date] = None) -> list[dt.date]:
    end = end or dt.date.today()
    days = []
    cursor = end - dt.timedelta(days=1)
    while len(days) < n:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor -= dt.timedelta(days=1)
    return sorted(days)


CHUNK_MINUTES = 15  # bounds peak memory per pull -- see the module-level warning below
DEFAULT_STRIKE_PCT = 0.03  # tighter than the live collector's 6% -- backfill exists to validate
                            # pipeline correctness (TD-2's replay-determinism gate), not to build a
                            # rich research dataset, so the narrower window is a deliberate safety
                            # tradeoff, not a scope compromise.
MIN_FREE_RAM_MB = 400  # abort (resumably) rather than risk this box's documented OOM/power-cycle failure mode
MIN_FREE_BACKFILL_DISK_MB = 1536  # TD-5's 30-day/2-symbol pass meaningfully grows disk usage on an
                                   # already 81%-full volume (9.4GB free as of 2026-07-26) -- a
                                   # deliberately higher floor than collector.py's own MIN_FREE_DISK_MB
                                   # (500MB, sized for a single live 5-min cycle), since this is a much
                                   # larger multi-day operation. Reuses collector._free_disk_mb() rather
                                   # than a second implementation of the same shutil.disk_usage check.


def _free_ram_mb() -> float:
    """/proc/meminfo's MemAvailable -- portable, no extra dependency, and
    the same 'available' figure `free -h` reports (accounts for reclaimable
    cache, unlike MemFree alone)."""
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) / 1024.0
    except Exception:
        pass
    return float("inf")  # fail open on non-Linux/unreadable -- never block on a check that can't run


def _session_chunks(chunk_minutes: int = CHUNK_MINUTES) -> list[tuple[dt.time, dt.time]]:
    start = dt.datetime.combine(dt.date.today(), dt.time(9, 30))
    end = dt.datetime.combine(dt.date.today(), dt.time(16, 0))
    chunks = []
    cur = start
    while cur < end:
        nxt = min(cur + dt.timedelta(minutes=chunk_minutes), end)
        chunks.append((cur.time(), nxt.time()))
        cur = nxt
    return chunks


def backfill_symbol_date(symbol: str, day: dt.date, strike_pct: float = DEFAULT_STRIKE_PCT) -> dict:
    """One (symbol, date) unit of work: discover that day's near-money
    strikes for the nearest expiration on/after `day`, pull trade_quote in
    CHUNK_MINUTES-wide windows (appending + discarding each chunk before
    pulling the next), then OI once for the day, then (TD-5) EOD IV/Greeks
    once for the day.

    REAL INCIDENT, fixed here: an earlier version pulled the entire
    09:30-16:00 session in one option_history_trade_quote call. Live-tested
    against real SPY 2026-07-24 data, RSS climbed past 1GB and swap usage
    approached 1GB on this box's 1.9GB total before the run was killed --
    a near-repeat of the documented power-cycle incidents from oversized
    pulls (see openclaw-server memory). Chunking to 30-minute windows (same
    order of magnitude as the live collector's own per-cycle pulls, which
    are known-safe) keeps peak memory bounded regardless of how many days
    get backfilled."""
    client = get_client()
    manifest = _load_manifest()
    key = f"{symbol}:{day.isoformat()}"
    if key in manifest["completed"]:
        return {"symbol": symbol, "date": day.isoformat(), "status": "already_done"}

    expirations = [e for e in active_expirations(symbol, day) if e >= day][:1]
    if not expirations:
        manifest["completed"].append(key)
        _save_manifest(manifest)
        return {"symbol": symbol, "date": day.isoformat(), "status": "no_expiration"}
    exp = expirations[0]

    spot = get_spot_price(symbol)  # best-effort "current" spot; historical spot isn't queried here (out of scope)
    strikes = strike_window(symbol, exp, spot, pct=strike_pct)
    strike_count = max(len(strikes) // 2, 1) + 2

    chunk_key_prefix = f"{key}:chunk:"
    appended = 0
    for chunk_start, chunk_end in _session_chunks():
        chunk_key = f"{chunk_key_prefix}{chunk_start.isoformat()}"
        if chunk_key in manifest["completed"]:
            continue
        free_mb = _free_ram_mb()
        if free_mb < MIN_FREE_RAM_MB:
            logger.warning(
                "aborting backfill for %s %s at chunk %s -- only %.0fMB RAM available (floor %dMB); "
                "resumable, re-run once load subsides", symbol, day, chunk_start, free_mb, MIN_FREE_RAM_MB,
            )
            return {"symbol": symbol, "date": day.isoformat(), "status": "aborted_low_memory",
                    "free_mb": round(free_mb, 1), "resume_at_chunk": chunk_start.isoformat(),
                    "rows_appended_so_far": appended}
        free_disk_mb = _free_disk_mb(DATA)
        if free_disk_mb < MIN_FREE_BACKFILL_DISK_MB:
            logger.warning(
                "aborting backfill for %s %s at chunk %s -- only %.0fMB disk available (floor %dMB); "
                "resumable, re-run once space is freed", symbol, day, chunk_start, free_disk_mb, MIN_FREE_BACKFILL_DISK_MB,
            )
            return {"symbol": symbol, "date": day.isoformat(), "status": "aborted_low_disk",
                    "free_disk_mb": round(free_disk_mb, 1), "resume_at_chunk": chunk_start.isoformat(),
                    "rows_appended_so_far": appended}
        try:
            trades = bounded_call(
                client.option_history_trade_quote,
                symbol=symbol, expiration=exp, date=day,
                strike="*", right="both", strike_range=strike_count,
                start_time=chunk_start.isoformat(), end_time=chunk_end.isoformat(),
            )
        except ThetaDataUnavailable as exc:
            return {"symbol": symbol, "date": day.isoformat(), "status": "error", "error": str(exc),
                    "failed_chunk": chunk_start.isoformat()}
        if trades is not None and not trades.empty:
            appended += append_raw(symbol, day, dedup_trades(trades))
        del trades
        manifest["completed"].append(chunk_key)
        _save_manifest(manifest)

    oi_map = {}
    try:
        oi_df = bounded_call(
            client.option_history_open_interest,
            symbol=symbol, expiration=exp, date=day, strike="*", right="both",
            strike_range=strike_count,
        )
        if oi_df is not None and not oi_df.empty:
            for row in oi_df.itertuples():
                cid = contract_id(symbol, dt.date.fromisoformat(str(row.expiration)[:10]), row.strike, row.right)
                oi_map[cid] = float(row.open_interest)
    except ThetaDataUnavailable as exc:
        logger.warning("backfill OI pull failed for %s %s: %s", symbol, day, exc)

    if oi_map:
        _atomic_write_json(DATA / f"oi_{symbol}_{day.isoformat()}.json", {"oi": oi_map, "oi_as_of_session": day.isoformat()})

    # TD-5: EOD IV/Greeks history for the same day/expiration -- SPY and QQQ
    # both list daily (0DTE) expirations (confirmed live), so "that day's
    # front expiration" is simply `day` itself. This is EOD data (one
    # observation per contract per day, not intraday), so folding it into
    # this same per-day unit of work costs one extra bounded_call, not a
    # separate historical-expiration-enumeration pass.
    iv_rows = []
    try:
        iv_df = bounded_call(
            client.option_history_greeks_eod,
            symbol=symbol, expiration=exp, start_date=day, end_date=day,
            strike="*", right="both", strike_range=strike_count,
        )
        if iv_df is not None and not iv_df.empty:
            for row in iv_df.itertuples():
                iv_rows.append({
                    "strike": float(row.strike),
                    "right": normalize_right(row.right),
                    "implied_vol": float(row.implied_vol) if row.implied_vol is not None else None,
                    "delta": float(row.delta) if row.delta is not None else None,
                    "underlying_price": float(row.underlying_price) if row.underlying_price is not None else None,
                })
    except ThetaDataUnavailable as exc:
        logger.warning("backfill IV/Greeks pull failed for %s %s: %s", symbol, day, exc)

    if iv_rows:
        _atomic_write_json(DATA / f"iv_{symbol}_{day.isoformat()}.json", {"rows": iv_rows, "session_date": day.isoformat()})

    manifest["completed"].append(key)
    _save_manifest(manifest)
    return {"symbol": symbol, "date": day.isoformat(), "status": "ok", "rows_appended": appended,
            "oi_contracts": len(oi_map), "iv_rows": len(iv_rows)}


def run_backfill(symbols: tuple[str, ...] = ("SPY", "QQQ"), days: int = 5) -> list[dict]:
    """Sequential, resumable -- never runs two symbols/days concurrently
    (single-core box constraint). Safe to interrupt and re-run; already
    completed (symbol, date) units are skipped via the manifest."""
    results = []
    for symbol in symbols:
        for day in _trading_days_back(days):
            results.append(backfill_symbol_date(symbol, day))
    return results


def _merge_write_oi(symbol: str, day: dt.date, new_oi: dict[str, float]) -> None:
    """Merges new_oi into the existing oi_{symbol}_{day}.json rather than
    overwriting it. backfill_symbol_date() already writes that file for the
    day's own 0DTE contracts; a fixed-expiration pull (see
    backfill_symbol_expiration_day) must not clobber that existing data --
    both expirations' OI need to coexist for the same calendar day."""
    path = DATA / f"oi_{symbol}_{day.isoformat()}.json"
    existing: dict[str, float] = {}
    if path.exists():
        try:
            existing = json.loads(path.read_text()).get("oi") or {}
        except Exception:
            existing = {}
    existing.update(new_oi)
    _atomic_write_json(path, {"oi": existing, "oi_as_of_session": day.isoformat()})


def backfill_symbol_expiration_day(
    symbol: str, day: dt.date, expiration: dt.date, strike_pct: float = DEFAULT_STRIKE_PCT,
) -> dict:
    """TD-5 follow-up (2026-07-26): one (symbol, date) unit of work for a
    FIXED, caller-chosen expiration rather than that day's own 0DTE.

    Real root cause this exists to fix: backfill_symbol_date() always
    tracks a DIFFERENT contract every single day (SPY/QQQ's own same-day
    0DTE expiration), which structurally can never support Ghost Wall's
    next-day-OI confirmation (TD-STD Section 6) -- a 0DTE contract has
    already expired by the next session, so there is never a "next day" OI
    reading for the SAME contract_id to compare against. Confirmed via a
    real 56-day/2-symbol calibration run: zero ghost-candidate confirmation
    events, not from insufficient sample size, but because the check could
    structurally never fire. This function instead tracks ONE expiration
    across MANY consecutive days, so the same contract_ids persist
    day-over-day and next-day OI comparison becomes possible.

    Deliberately skips the EOD IV/Greeks pull backfill_symbol_date() does --
    Ghost Wall's V/OI and bid/ask-fraction math needs trade_quote+OI only,
    never Greeks/IV, so there's no reason to spend the extra API call here.

    OI is MERGED (via _merge_write_oi), not overwritten, since
    backfill_symbol_date() may have already populated oi_{symbol}_{day}.json
    with that day's own 0DTE contracts -- both expirations' trades coexist
    naturally in the same raw partition too (different contract_ids), so
    classify_trades/wall_aggregates already handle a mixed-expiration day
    correctly with no special-casing needed."""
    client = get_client()
    manifest = _load_manifest()
    key = f"{symbol}:{day.isoformat()}:exp:{expiration.isoformat()}"
    if key in manifest["completed"]:
        return {"symbol": symbol, "date": day.isoformat(), "expiration": expiration.isoformat(), "status": "already_done"}

    spot = get_spot_price(symbol)
    strikes = strike_window(symbol, expiration, spot, pct=strike_pct)
    strike_count = max(len(strikes) // 2, 1) + 2

    chunk_key_prefix = f"{key}:chunk:"
    appended = 0
    for chunk_start, chunk_end in _session_chunks():
        chunk_key = f"{chunk_key_prefix}{chunk_start.isoformat()}"
        if chunk_key in manifest["completed"]:
            continue
        free_mb = _free_ram_mb()
        if free_mb < MIN_FREE_RAM_MB:
            logger.warning(
                "aborting expiration-backfill for %s %s (exp %s) at chunk %s -- only %.0fMB RAM "
                "available (floor %dMB); resumable, re-run once load subsides",
                symbol, day, expiration, chunk_start, free_mb, MIN_FREE_RAM_MB,
            )
            return {"symbol": symbol, "date": day.isoformat(), "expiration": expiration.isoformat(),
                    "status": "aborted_low_memory", "free_mb": round(free_mb, 1),
                    "resume_at_chunk": chunk_start.isoformat(), "rows_appended_so_far": appended}
        free_disk_mb = _free_disk_mb(DATA)
        if free_disk_mb < MIN_FREE_BACKFILL_DISK_MB:
            logger.warning(
                "aborting expiration-backfill for %s %s (exp %s) at chunk %s -- only %.0fMB disk "
                "available (floor %dMB); resumable, re-run once space is freed",
                symbol, day, expiration, chunk_start, free_disk_mb, MIN_FREE_BACKFILL_DISK_MB,
            )
            return {"symbol": symbol, "date": day.isoformat(), "expiration": expiration.isoformat(),
                    "status": "aborted_low_disk", "free_disk_mb": round(free_disk_mb, 1),
                    "resume_at_chunk": chunk_start.isoformat(), "rows_appended_so_far": appended}
        try:
            trades = bounded_call(
                client.option_history_trade_quote,
                symbol=symbol, expiration=expiration, date=day,
                strike="*", right="both", strike_range=strike_count,
                start_time=chunk_start.isoformat(), end_time=chunk_end.isoformat(),
            )
        except ThetaDataUnavailable as exc:
            return {"symbol": symbol, "date": day.isoformat(), "expiration": expiration.isoformat(),
                    "status": "error", "error": str(exc), "failed_chunk": chunk_start.isoformat()}
        if trades is not None and not trades.empty:
            appended += append_raw(symbol, day, dedup_trades(trades))
        del trades
        manifest["completed"].append(chunk_key)
        _save_manifest(manifest)

    oi_map: dict[str, float] = {}
    try:
        oi_df = bounded_call(
            client.option_history_open_interest,
            symbol=symbol, expiration=expiration, date=day, strike="*", right="both",
            strike_range=strike_count,
        )
        if oi_df is not None and not oi_df.empty:
            for row in oi_df.itertuples():
                cid = contract_id(symbol, dt.date.fromisoformat(str(row.expiration)[:10]), row.strike, row.right)
                oi_map[cid] = float(row.open_interest)
    except ThetaDataUnavailable as exc:
        logger.warning("expiration-backfill OI pull failed for %s %s (exp %s): %s", symbol, day, expiration, exc)

    if oi_map:
        _merge_write_oi(symbol, day, oi_map)

    manifest["completed"].append(key)
    _save_manifest(manifest)
    return {"symbol": symbol, "date": day.isoformat(), "expiration": expiration.isoformat(),
            "status": "ok", "rows_appended": appended, "oi_contracts": len(oi_map)}


def run_expiration_backfill(symbols: tuple[str, ...], expiration: dt.date, days: list[dt.date]) -> list[dict]:
    """Sequential, resumable, same single-core concurrency discipline as
    run_backfill -- tracks ONE fixed expiration across an explicit list of
    days (the caller picks a specific window to extend, not "N trading
    days back from today")."""
    results = []
    for symbol in symbols:
        for day in days:
            if day > expiration:
                continue  # can't query a day after the contract's own expiration
            results.append(backfill_symbol_expiration_day(symbol, day, expiration))
    return results
