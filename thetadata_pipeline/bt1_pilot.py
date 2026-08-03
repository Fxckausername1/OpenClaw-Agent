"""BT-1 pilot: a trustworthy, graded historical dataset for SPY/QQQ before
BT-2 (the simulator) is allowed to start. BOT_NEXUS_Options_Strategy_
Backtesting_Roadmap.pdf Section 3 / heff's BT-1 spec (2026-07-26).

Deliberately a SEPARATE pull from backfill.py's existing TD-5 calibration
backfill, not a reuse of backfill_symbol_date() itself: TD-5's backfill is
narrow-strike (3%, research-safety tradeoff, see its own docstring) and
discards the classified DataFrame per chunk (it only needs row counts). BT-1
needs the wider "Always on" strike window (6%, matching contracts.py's own
STRIKE_WINDOW_PCT -- "full relevant strike range, not only contracts that
later became profitable") AND per-chunk classification stats retained long
enough to grade the session, so this module has its own chunk loop. It
reuses every existing safety/primitive it can: backfill.py's RAM/disk
guards and chunk-boundary helper, normalize.py's classification/dedup,
collector.py's atomic-write pattern, client.py's retry/masking wrapper.

Isolation rule (same as the rest of this package): never touches
live_gex_snapshot.json, vex_history.json, iv_intraday_state.json, or
anything backfill.py/collector.py already own. Writes only under
data/thetadata/bt1_pilot/.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import time as time_module
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import pandas as pd
import requests

from .backfill import (
    MIN_FREE_BACKFILL_DISK_MB, MIN_FREE_RAM_MB, _free_ram_mb, _session_chunks,
)
from .bt1_manifest import build_session_manifest, core_hour_chunks, overall_summary
from .client import ThetaDataUnavailable, bounded_call, get_client
from .collector import _atomic_write_json, _free_disk_mb
from .contracts import _alpaca_headers, active_expirations, get_spot_price, strike_window
from .normalize import classify_trades, coverage_stats, dedup_trades
from .schemas import contract_id, normalize_right, occ_symbol, parse_expiration

ET = ZoneInfo("America/New_York")
ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "thetadata"
BT1_DIR = DATA / "bt1_pilot"
BT1_MANIFEST_PATH = BT1_DIR / "bt1_pilot_manifest.json"
BT1_RAW_DIR = BT1_DIR / "raw"

CALENDAR_URL = "https://api.alpaca.markets/v2/calendar"
BARS_URL = "https://data.alpaca.markets/v2/stocks/bars"
WIDE_STRIKE_PCT = 0.06  # matches contracts.py's STRIKE_WINDOW_PCT -- "full relevant strike range"

logger = logging.getLogger("thetadata_pkg.bt1_pilot")


def _thetadata_time(value: dt.time) -> str:
    """Millisecond precision, not microsecond -- the exact bug the journal
    worker's historical-Greeks pull hit and fixed (Gate B, 2026-07-26).
    Applied to every ThetaData time param in this module, not just Greeks,
    so this file can never reintroduce that class of bug even where a
    whole-minute chunk boundary happens to have no fractional seconds today."""
    return value.isoformat(timespec="milliseconds")


# --- Trading calendar (real, not a naive weekday skip) ---------------------

def fetch_trading_calendar(start: dt.date, end: dt.date) -> list[dict]:
    """Real session calendar via Alpaca's Trading API (api.alpaca.markets --
    a different host than the Market Data API used for bars). Each row:
    date, open, close, is_early_close. Returns [] on any failure; callers
    must never assume weekday == trading day, which is exactly the gap
    backfill.py's own `_trading_days_back` has (it only skips Sat/Sun, not
    holidays)."""
    try:
        resp = requests.get(
            CALENDAR_URL, headers=_alpaca_headers(),
            params={"start": start.isoformat(), "end": end.isoformat()}, timeout=15,
        )
        resp.raise_for_status()
        rows = resp.json()
    except Exception as exc:
        logger.warning("trading calendar fetch failed (%s to %s): %s", start, end, exc)
        return []
    out = []
    for row in rows:
        close = row.get("close", "16:00")
        out.append({
            "date": row["date"],
            "open": row.get("open", "09:30"),
            "close": close,
            "is_early_close": close < "16:00",
        })
    return out


def most_recent_complete_sessions(n: int, before: Optional[dt.date] = None) -> list[dict]:
    """The n most recent FULLY COMPLETED sessions strictly before `before`
    (today by default) -- via the real calendar, so a holiday week can never
    silently produce fewer usable sessions than requested without saying so.
    Looks back 3x the window (floor 14 calendar days) to comfortably clear
    holiday clusters."""
    before = before or dt.date.today()
    lookback_start = before - dt.timedelta(days=max(n * 3, 14))
    calendar = fetch_trading_calendar(lookback_start, before - dt.timedelta(days=1))
    sessions = [row for row in calendar if row["date"] < before.isoformat()]
    return sessions[-n:]


# --- Underlying bars ---------------------------------------------------

def _et_datetime(day: dt.date, time_str: str) -> dt.datetime:
    hour, minute = (int(x) for x in time_str.split(":")[:2])
    return dt.datetime(day.year, day.month, day.day, hour, minute, tzinfo=ET)


def fetch_underlying_bars(symbol: str, day: dt.date, session_open: str, session_close: str) -> pd.DataFrame:
    """1-min bars for exactly this session's real hours (respects early
    closes via session_open/session_close from the real calendar, never a
    hardcoded 09:30-16:00). Same feed/endpoint shape as wide_universe.py's
    already-proven fetch_bars_batch (free IEX feed, {"bars": {symbol: [...]}}
    response, next_page_token pagination) -- single symbol here since each
    BT-1 session is graded independently per symbol."""
    start_iso = _et_datetime(day, session_open).astimezone(dt.timezone.utc).isoformat()
    end_iso = _et_datetime(day, session_close).astimezone(dt.timezone.utc).isoformat()
    rows: list[dict] = []
    page_token = None
    while True:
        params = {
            "symbols": symbol, "timeframe": "1Min", "start": start_iso, "end": end_iso,
            "feed": "iex", "adjustment": "raw", "limit": 10000, "sort": "asc",
        }
        if page_token:
            params["page_token"] = page_token
        try:
            resp = requests.get(BARS_URL, headers=_alpaca_headers(), params=params, timeout=25)
            resp.raise_for_status()
            payload = resp.json()
        except Exception as exc:
            logger.warning("underlying bars fetch failed for %s %s: %s", symbol, day, exc)
            break
        rows.extend((payload.get("bars") or {}).get(symbol) or [])
        page_token = payload.get("next_page_token")
        if not page_token:
            break
    if not rows:
        return pd.DataFrame(columns=["t", "o", "h", "l", "c", "v"])
    df = pd.DataFrame(rows)
    df["t"] = pd.to_datetime(df["t"], utc=True)
    return df


def expected_bar_count(session_open: str, session_close: str) -> int:
    open_dt = _et_datetime(dt.date(2000, 1, 1), session_open)
    close_dt = _et_datetime(dt.date(2000, 1, 1), session_close)
    return max(int((close_dt - open_dt).total_seconds() // 60), 0)


# --- Per-session pull ----------------------------------------------------

def pull_bt1_session(symbol: str, session: dict, strike_pct: float = WIDE_STRIKE_PCT) -> dict:
    """One (symbol, session) unit of work. Pulls underlying bars, option
    trade_quote + point-in-time Greeks (chunked, same RAM/disk guards as
    backfill.py) + OI, retains only small per-chunk aggregates (never a
    session-long concatenated DataFrame) to grade the session, and returns
    every field bt1_manifest.build_session_manifest needs. Resumable at the
    chunk level via the shared backfill manifest's key namespace would be
    possible but is deliberately NOT done here -- a 5-session pilot is cheap
    enough to just re-run from scratch if interrupted, and skipping that
    complexity keeps this pull's own manifest simpler to audit."""
    day = dt.date.fromisoformat(session["date"])
    started_at = dt.datetime.now(dt.timezone.utc).isoformat()
    thetadata_calls = 0
    thetadata_errors = 0
    unrecoverable_errors: list[str] = []
    empty_core_chunks: list[str] = []

    client = get_client()
    expirations = [e for e in active_expirations(symbol, day) if e >= day][:1]
    trade_rows_total = 0
    greeks_rows_total = 0
    dup_rows_total = 0
    directional_total = 0
    ambiguous_total = 0
    crossed_total = 0
    classified_total = 0
    occ_symbols_seen: set[str] = set()

    if not expirations:
        unrecoverable_errors.append("no active expiration found for this session")
        exp = None
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
            free_disk_mb = _free_disk_mb(BT1_DIR if BT1_DIR.exists() else ROOT)
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
                del trades, deduped, classified

            try:
                thetadata_calls += 1
                greeks = bounded_call(
                    client.option_history_greeks_first_order,
                    symbol=symbol, expiration=exp, date=day,
                    strike="*", right="both", strike_range=strike_count, interval="1m",
                    start_time=_thetadata_time(chunk_start), end_time=_thetadata_time(chunk_end),
                )
                if greeks is not None and not greeks.empty:
                    greeks_rows_total += len(greeks)
                del greeks
            except ThetaDataUnavailable as exc:
                thetadata_errors += 1
                unrecoverable_errors.append(f"greeks chunk {label} failed: {exc}")

            if not chunk_had_trades:
                empty_core_chunks.append(label)

        empty_core_chunks = core_hour_chunks(empty_core_chunks)

    oi_contracts = 0
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
        except ThetaDataUnavailable as exc:
            thetadata_errors += 1
            unrecoverable_errors.append(f"open_interest failed: {exc}")

    bars = fetch_underlying_bars(symbol, day, session["open"], session["close"])
    expected_bars = expected_bar_count(session["open"], session["close"])

    completed_at = dt.datetime.now(dt.timezone.utc).isoformat()
    coverage = (directional_total / classified_total) if classified_total else None
    ambiguous_fraction = (ambiguous_total / classified_total) if classified_total else None
    crossed_fraction = (crossed_total / classified_total) if classified_total else None

    return build_session_manifest(
        symbol=symbol, date=session["date"],
        calendar=session,
        requested={
            "expiration": exp.isoformat() if exp else None,
            "strike_pct": strike_pct,
            "option_trade_quote_chunks": len(_session_chunks()),
            "option_greeks_chunks": len(_session_chunks()),
            "underlying_bars_timeframe": "1Min",
        },
        received={
            "option_trade_quote_rows": trade_rows_total,
            "option_greeks_rows": greeks_rows_total,
            "open_interest_contracts": oi_contracts,
            "underlying_bars_count": len(bars),
            "underlying_bars_expected": expected_bars,
            "distinct_occ_symbols": len(occ_symbols_seen),
        },
        missing={
            "empty_trade_quote_core_chunks": empty_core_chunks,
            "unrecoverable_errors": unrecoverable_errors,
        },
        rejected={
            "duplicate_trade_rows_dropped": dup_rows_total,
            "crossed_or_locked_quote_rows": crossed_total,
        },
        retrieval={"started_at": started_at, "completed_at": completed_at},
        response_metadata={"thetadata_calls": thetadata_calls, "thetadata_errors": thetadata_errors,
                            "alpaca_calls": 2},  # calendar (shared across sessions) + this session's bars call
        integrity={
            "trade_classification_coverage": coverage,
            "ambiguous_trade_fraction": ambiguous_fraction,
            "crossed_market_fraction": crossed_fraction,
            "timezone_normalized": True,  # all timestamps handled here are tz-aware (UTC or ET), never naive
        },
    )


def run_bt1_pilot(symbols: tuple[str, ...] = ("SPY", "QQQ"), sessions: int = 5) -> dict:
    """Top-level BT-1 pilot entry point. Sequential across symbols and
    sessions (single-core box, same discipline as run_backfill) -- never
    parallelized. Writes one consolidated manifest to
    data/thetadata/bt1_pilot/bt1_pilot_manifest.json and returns it."""
    BT1_DIR.mkdir(parents=True, exist_ok=True)
    target_sessions = most_recent_complete_sessions(sessions)
    if len(target_sessions) < sessions:
        logger.warning(
            "requested %d sessions but the calendar only yielded %d before today "
            "(holiday cluster or calendar fetch failure)", sessions, len(target_sessions),
        )

    session_manifests = []
    for symbol in symbols:
        for session in target_sessions:
            logger.info("BT-1 pilot: pulling %s %s", symbol, session["date"])
            session_manifests.append(pull_bt1_session(symbol, session))

    manifest = {
        "schema_version": "bt1-pilot-run-1.0",
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "pilot_scope": {"symbols": list(symbols), "sessions_requested": sessions,
                         "sessions_found": len(target_sessions)},
        "sessions": session_manifests,
        "overall": overall_summary(session_manifests),
    }
    _atomic_write_json(BT1_MANIFEST_PATH, manifest)
    return manifest


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    result = run_bt1_pilot()
    print(json.dumps(result["overall"], indent=2))
