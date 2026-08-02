"""Cron-driven raw acquisition -- TD-STD Section 3's Collector responsibility.

DESIGN DEVIATION FROM TD-STD, DOCUMENTED: Section 3 describes a persistent
thetadata-client.service holding an open stream. This box is single-core
with 1.9GB RAM (see openclaw-server memory) and every other data pull in
this codebase (live_gex_rolling.py, wall_proximity_alert.py, the scanners)
is a periodic cron job, not a long-running daemon -- a persistent process
here would be a new, unbudgeted standing memory/CPU cost on a box that has
twice needed a power-cycle from resource exhaustion. The ThetaClient Python
library itself is REST-style (bounded history/snapshot calls, no
subscribe() method), so a cron-driven collector achieves the same
information (SPY/QQQ trade+quote+OI+Greeks, refreshed every few minutes)
without the daemon. Each run pulls only the window since its own last
cursor, so accuracy vs. a true stream is a matter of cadence, not
correctness.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import shutil
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import pandas as pd

from . import aggregate as agg
from . import contracts as contracts_mod
from .client import ThetaDataUnavailable, bounded_call, get_client
from .normalize import classify_trades, coverage_stats, dedup_trades
from .schemas import contract_id, normalize_right, source_health

ET = ZoneInfo("America/New_York")
ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "thetadata"
RAW_DIR = DATA / "raw"
CURSOR_DIR = DATA / "cursors"
for d in (DATA, RAW_DIR, CURSOR_DIR):
    d.mkdir(parents=True, exist_ok=True)

SESSION_OPEN = dt.time(9, 30)
SESSION_CLOSE = dt.time(16, 0)
OI_EARLIEST = dt.time(6, 35)
RAW_RETENTION_DAYS = 14  # conservative given the box's real 9.5GB free disk (measured 2026-07-25)
MIN_FREE_DISK_MB = 500  # Section 13: stop optional raw retention before corrupting current writes

logger = logging.getLogger("thetadata_pkg.collector")


def market_is_open(now_et: dt.datetime) -> bool:
    """Same convention as mean_reversion_scanner.py/wall_proximity_alert.py."""
    if now_et.weekday() >= 5:
        return False
    return SESSION_OPEN <= now_et.time() <= SESSION_CLOSE


def _atomic_write_json(path: Path, payload) -> None:
    tmp = path.with_suffix(f".tmp{os.getpid()}")
    tmp.write_text(json.dumps(payload, indent=2, default=str))
    os.replace(tmp, path)


def _free_disk_mb(path: Path) -> float:
    usage = shutil.disk_usage(path)
    return usage.free / (1024 * 1024)


def _cursor_path(symbol: str) -> Path:
    return CURSOR_DIR / f"{symbol}.json"


def load_cursor(symbol: str, today: dt.date) -> dt.time:
    path = _cursor_path(symbol)
    if path.exists():
        try:
            data = json.loads(path.read_text())
            if data.get("date") == today.isoformat():
                return dt.time.fromisoformat(data["last_end_time"])
        except Exception:
            pass
    return SESSION_OPEN


def save_cursor(symbol: str, today: dt.date, end_time: dt.time) -> None:
    _atomic_write_json(_cursor_path(symbol), {"date": today.isoformat(), "last_end_time": end_time.isoformat()})


def _cumulative_stats_path(symbol: str, today: dt.date) -> Path:
    return DATA / f"cumulative_stats_{symbol}_{today.isoformat()}.json"


def load_cumulative_stats(symbol: str, today: dt.date) -> dict:
    """Small persisted per-contract running totals (ask/bid/mid/total
    volume) -- see aggregate.accumulate_contract_stats()'s docstring for
    why this replaced a full-day raw re-read."""
    path = _cumulative_stats_path(symbol, today)
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            pass
    return {}


def save_cumulative_stats(symbol: str, today: dt.date, stats: dict) -> None:
    _atomic_write_json(_cumulative_stats_path(symbol, today), stats)


def _raw_partition_dir(symbol: str, today: dt.date) -> Path:
    d = RAW_DIR / symbol / today.isoformat()
    d.mkdir(parents=True, exist_ok=True)
    return d


def append_raw(symbol: str, today: dt.date, new_rows: pd.DataFrame) -> int:
    """TRUE append-only: writes new_rows as its own small part-file, never
    reads or rewrites prior parts. Dedup happens at READ time
    (read_raw_partition), not write time.

    REAL BUG this replaces, caught live: the original version read the
    ENTIRE day's accumulated partition back into memory on every single
    write, concatenated, deduped, and rewrote the whole file -- O(day_size)
    work per cycle, O(day_size^2) over a session. Live-tested against real
    SPY 2026-07-24 backfill data, this crashed with a pyarrow
    ArrowMemoryError (malloc failed) inside a deliberate 1.27GB ulimit
    fail-safe partway through a single day -- the fail-safe did its job
    (a clean exception instead of the box's documented OOM/power-cycle
    failure mode), but the underlying design was the real problem. Each
    write here is O(new_rows) only, regardless of how much history already
    exists for the day."""
    if new_rows.empty:
        return 0
    if _free_disk_mb(ROOT) < MIN_FREE_DISK_MB:
        logger.warning("disk pressure (<%dMB free) -- skipping raw retention this cycle", MIN_FREE_DISK_MB)
        return 0
    deduped = dedup_trades(new_rows)
    if deduped.empty:
        return 0
    part_dir = _raw_partition_dir(symbol, today)
    part_name = f"part-{dt.datetime.now(ET).strftime('%Y%m%dT%H%M%S%f')}-{os.getpid()}.parquet"
    tmp = part_dir / f".tmp{part_name}"
    deduped.to_parquet(tmp, index=False)
    os.replace(tmp, part_dir / part_name)
    return len(deduped)


def read_raw_partition(symbol: str, today: dt.date) -> pd.DataFrame:
    """Reads and dedupes every part-file written for (symbol, today).
    O(day_size) but only when actually needed for feature computation, not
    on every incremental write."""
    part_dir = _raw_partition_dir(symbol, today)
    parts = sorted(part_dir.glob("part-*.parquet"))
    if not parts:
        return pd.DataFrame()
    frames = [pd.read_parquet(p) for p in parts]
    return dedup_trades(pd.concat(frames, ignore_index=True))


def rotate_raw(retention_days: int = RAW_RETENTION_DAYS) -> list[str]:
    """Deletes raw partitions older than retention_days. Only ever touches
    RAW_DIR (this package's own data), never any other file on the box."""
    cutoff = dt.date.today() - dt.timedelta(days=retention_days)
    removed = []
    if not RAW_DIR.exists():
        return removed
    for symbol_dir in RAW_DIR.iterdir():
        if not symbol_dir.is_dir():
            continue
        for date_dir in symbol_dir.iterdir():
            try:
                partition_date = dt.date.fromisoformat(date_dir.name)
            except ValueError:
                continue
            if partition_date < cutoff:
                shutil.rmtree(date_dir, ignore_errors=True)
                removed.append(str(date_dir))
    return removed


def _strike_range_for_universe(universe: dict) -> Optional[int]:
    """Turn the persisted near-money strike window into a strike_range=n
    (the endpoint's own 'n strikes above/below spot' narrowing -- confirmed
    via docs.thetadata.us, NOT a dollar distance). Bounding the query
    server-side avoids ever pulling a full chain: a real live test showed
    strike='*' on SPY 0DTE returns ~163k rows for a single 30-minute
    window, which this single-core/1.9GB box has no business processing
    every few minutes."""
    strikes = sorted({c["strike"] for c in universe.get("contracts", [])})
    spot = universe.get("spot")
    if not strikes or spot is None:
        return None
    above = sum(1 for s in strikes if s >= spot)
    below = sum(1 for s in strikes if s < spot)
    return max(above, below) + 2  # small buffer for spot drift since the universe was built


TRADE_QUOTE_CHUNK_MINUTES = 15  # bounds peak memory per pull if the cursor is ever badly stale --
                                 # mirrors backfill.py's identical CHUNK_MINUTES discipline, needed
                                 # for the same reason. See collect_trade_quote's own docstring.
MIN_FREE_RAM_MB = 400  # same floor and reasoning as backfill.py's own guard


def _free_ram_mb() -> float:
    """/proc/meminfo's MemAvailable -- duplicated from backfill.py's
    identical check rather than imported, since backfill.py already
    imports FROM this module (importing back would be circular)."""
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) / 1024.0
    except Exception:
        pass
    return float("inf")


def _time_chunks(start: dt.time, end: dt.time, chunk_minutes: int = TRADE_QUOTE_CHUNK_MINUTES) -> list[tuple[dt.time, dt.time]]:
    """Splits [start, end] into chunk_minutes-wide (start, end) time pairs.
    Uses an arbitrary anchor date purely so dt.datetime arithmetic can
    handle the addition -- only the time-of-day values are used by the
    caller."""
    anchor = dt.date.today()
    cur = dt.datetime.combine(anchor, start)
    stop = dt.datetime.combine(anchor, end)
    chunks = []
    while cur < stop:
        nxt = min(cur + dt.timedelta(minutes=chunk_minutes), stop)
        chunks.append((cur.time(), nxt.time()))
        cur = nxt
    return chunks


def collect_trade_quote(symbol: str, universe: dict, today: dt.date, now_et: dt.datetime) -> pd.DataFrame:
    """Pull trade+quote history for every expiration in the active universe
    across the window [cursor, now], bounded server-side via strike_range
    to the near-money window (see _strike_range_for_universe), AND bounded
    client-side via TRADE_QUOTE_CHUNK_MINUTES-wide time slices with a
    cursor that advances after each successful chunk.

    REAL GAP this chunking closes, found 2026-07-26: this function used to
    pull the ENTIRE [cursor, now] window in one call per expiration. In
    real live-cron operation the window is always small (~5 minutes,
    matching the cron cadence), so this was invisible for weeks. But if the
    cursor is ever badly stale -- a multi-hour cron outage, or a first
    cycle of the day that happens to run late -- this pulled many hours of
    0DTE data in one unbounded call. Confirmed live: simulating exactly
    this (no cursor, `now`=14:00 ET) drove available RAM from ~1.2GB to
    376MB before being killed, well past the 400MB floor backfill.py
    already enforces for its own chunked pulls -- this function had no
    equivalent guard at all. The cursor now advances after each chunk
    (not only at the very end), so an aborted catch-up degrades to
    'finishes over more cron cycles' instead of risking an OOM in one
    shot -- same tradeoff backfill.py already makes for its own pulls."""
    start = load_cursor(symbol, today)
    end = min(now_et.time(), SESSION_CLOSE)
    if end <= start:
        return pd.DataFrame()

    client = get_client()
    strike_range = _strike_range_for_universe(universe)
    expirations = [dt.date.fromisoformat(e) for e in universe.get("expirations", [])]

    frames = []
    for chunk_start, chunk_end in _time_chunks(start, end):
        free_mb = _free_ram_mb()
        if free_mb < MIN_FREE_RAM_MB:
            logger.warning(
                "aborting collect_trade_quote for %s at chunk %s -- only %.0fMB RAM available "
                "(floor %dMB); cursor saved up to the last successful chunk, will resume next cycle",
                symbol, chunk_start, free_mb, MIN_FREE_RAM_MB,
            )
            break
        for exp in expirations:
            try:
                df = bounded_call(
                    client.option_history_trade_quote,
                    symbol=symbol, expiration=exp, date=today,
                    strike="*", right="both", strike_range=strike_range,
                    start_time=chunk_start.isoformat(), end_time=chunk_end.isoformat(),
                )
            except ThetaDataUnavailable as exc:
                logger.warning("trade_quote pull failed for %s %s (chunk %s-%s): %s",
                                symbol, exp, chunk_start, chunk_end, exc)
                continue
            if df is not None and not df.empty:
                frames.append(df)
        save_cursor(symbol, today, chunk_end)

    if not frames:
        return pd.DataFrame()
    combined = pd.concat(frames, ignore_index=True)
    # Belt-and-suspenders: strike_range is dynamically spot-relative at
    # query time and may drift slightly from the persisted universe built
    # earlier in the day, so still trim to the exact audited strike set.
    windowed_strikes = {c["strike"] for c in universe.get("contracts", [])}
    if windowed_strikes:
        combined = combined[combined["strike"].isin(windowed_strikes)].reset_index(drop=True)
    return combined


def collect_open_interest(symbol: str, universe: dict, today: dt.date) -> tuple[dict[str, float], Optional[str]]:
    """Once-per-day pull (Section 4/12: 'Load after 06:30 ET and preserve
    point-in-time date'). Returns {contract_id: oi} plus the real reported
    as-of session date (read from the response, never assumed)."""
    cache_path = DATA / f"oi_{symbol}_{today.isoformat()}.json"
    if cache_path.exists():
        try:
            cached = json.loads(cache_path.read_text())
            return cached["oi"], cached["oi_as_of_session"]
        except Exception:
            pass

    client = get_client()
    strike_range = _strike_range_for_universe(universe)
    oi_map: dict[str, float] = {}
    as_of_session: Optional[str] = None
    for exp_str in universe.get("expirations", []):
        exp = dt.date.fromisoformat(exp_str)
        try:
            df = bounded_call(
                client.option_history_open_interest,
                symbol=symbol, expiration=exp, date=today, strike="*", right="both",
                strike_range=strike_range,
            )
        except ThetaDataUnavailable as exc:
            logger.warning("OI pull failed for %s %s: %s", symbol, exp, exc)
            continue
        if df is None or df.empty:
            continue
        for row in df.itertuples():
            cid = contract_id(symbol, dt.date.fromisoformat(str(row.expiration)[:10]), row.strike, row.right)
            oi_map[cid] = float(row.open_interest)
            if as_of_session is None:
                as_of_session = pd.Timestamp(row.timestamp).date().isoformat()

    if oi_map:
        _atomic_write_json(cache_path, {"oi": oi_map, "oi_as_of_session": as_of_session})
    return oi_map, as_of_session


def collect_greeks(symbol: str, universe: dict) -> dict[str, dict]:
    """First-order Greeks snapshot -> per-contract {delta, implied_vol,
    strike, right, expiration}. Standard has no trade-level Greeks (CB-V4
    Section 5), so this is always the nearest prior snapshot -- callers
    must keep estimated_delta=True alongside any delta value sourced from
    here. implied_vol is the same response's already-returned field (CB-V4
    Phase 1: was being silently discarded -- no new API call needed to
    build IV/skew features from it)."""
    client = get_client()
    strike_range = _strike_range_for_universe(universe)
    greeks_map: dict[str, dict] = {}
    for exp_str in universe.get("expirations", []):
        exp = dt.date.fromisoformat(exp_str)
        try:
            df = bounded_call(
                client.option_snapshot_greeks_first_order,
                symbol=symbol, expiration=exp, strike="*", right="both",
                strike_range=strike_range,
            )
        except ThetaDataUnavailable as exc:
            logger.warning("Greeks pull failed for %s %s: %s", symbol, exp, exc)
            continue
        if df is None or df.empty:
            continue
        for row in df.itertuples():
            row_expiration = dt.date.fromisoformat(str(row.expiration)[:10])
            cid = contract_id(symbol, row_expiration, row.strike, row.right)
            greeks_map[cid] = {
                "delta": float(row.delta) if row.delta is not None else None,
                "implied_vol": float(row.implied_vol) if getattr(row, "implied_vol", None) is not None else None,
                "strike": float(row.strike),
                "right": normalize_right(row.right),
                "expiration": row_expiration.isoformat(),
            }
    return greeks_map


def run_cycle(symbols: tuple[str, ...] = ("SPY", "QQQ"), now: Optional[dt.datetime] = None) -> dict:
    """One full collector cycle for every symbol. Returns a dict of
    per-symbol raw results + health, consumed by snapshot.py to build
    microstructure_snapshot.json. Never writes to that snapshot itself --
    single responsibility, per Section 3's component table."""
    now = now or dt.datetime.now(ET)
    if not market_is_open(now):
        return {"skipped": "market_closed", "now": now.isoformat()}

    today = now.date()
    results = {}
    for symbol in symbols:
        universe = contracts_mod.ensure_active_universe(symbol, now)
        raw = collect_trade_quote(symbol, universe, today, now)
        appended = append_raw(symbol, today, raw) if not raw.empty else 0

        # This cycle's own small classified batch -- feeds minute_bars/CVD
        # (already correctly incremental via snapshot.py's persisted
        # cvd_state) and this cycle's own coverage reading.
        classified = classify_trades(raw) if not raw.empty else raw
        coverage = coverage_stats(classified)

        # V/OI needs cumulative session-to-date volume (TD-STD Section 6),
        # NOT this cycle's slice alone. A prior version re-read the whole
        # day's raw partition every cycle to get this -- live-tested
        # against real SPY 2026-07-24 data, a single day's near-money
        # trade+quote history came back as 1,235,319 rows, and
        # concatenating/deduping that every ~5 minutes crashed with a real
        # numpy ArrayMemoryError on this 1.9GB box. Fixed: fold just this
        # cycle's small batch into a persisted per-contract running-total
        # dict (a few ints per contract, not million-row DataFrames) --
        # see aggregate.accumulate_contract_stats().
        cumulative_stats = load_cumulative_stats(symbol, today)
        cumulative_stats = agg.accumulate_contract_stats(cumulative_stats, classified)
        save_cumulative_stats(symbol, today, cumulative_stats)

        oi_map, oi_as_of = ({}, None)
        if now.time() >= OI_EARLIEST:
            oi_map, oi_as_of = collect_open_interest(symbol, universe, today)

        greeks_map = collect_greeks(symbol, universe)

        health = source_health(
            provider="thetadata",
            observed_at=now.isoformat(),
            age_seconds=0.0,
            contracts_expected=universe.get("contract_count", 0),
            contracts_received=len(cumulative_stats),
            trade_classification_coverage=coverage.get("trade_classification_coverage"),
            ambiguous_trade_fraction=coverage.get("ambiguous_trade_fraction"),
            oi_as_of_session=oi_as_of,
            quality="FRESH" if not raw.empty or appended >= 0 else "DEGRADED",
        )

        results[symbol] = {
            "universe": universe,
            "classified_trades": classified,  # this cycle's small batch only -- feeds minute_bars/CVD
            "cumulative_stats": cumulative_stats,  # session-to-date per-contract totals -- feeds wall_aggregates/V-OI
            "rows_appended": appended,
            "oi": oi_map,
            "greeks": greeks_map,  # per-contract {delta, implied_vol, strike, right, expiration}
            "health": health,
        }

    return {"now": now.isoformat(), "results": results}
