"""ThetaData REST snapshot client -- DISCOVERY AND PERIODIC GREEKS REFRESH
ONLY (heff's explicit correction, 2026-08-01: "Do not implement REST
polling as the primary quote path... Use REST for initial contract/Greek
discovery and periodic refresh"). Real-time bid/ask/sizes for entry/exit
DECISIONS come from theta_stream.py's WebSocket cache instead -- this
module exists because the streaming quote protocol does not carry delta at
all, so delta has to come from a periodic REST Greeks pull regardless of
how fast the quote stream is. See candidate_universe.py for how the two
caches (this module's delta, theta_stream's live bid/ask) merge into one
selection-ready book.

option_snapshot_quote/option_snapshot_greeks_first_order both accept
strike='*', right='both' -- the whole candidate-DTE QQQ chain's quotes and
greeks come back in one bulk call each (verified live 2026-08-01: 408 rows
for a single expiration in each call). refresh() is meant to run on a loose
background interval (tens of seconds, not sub-second) since this is
explicitly NOT the hot path; contract selection and exit monitoring never
call refresh() themselves.

Book shape matches thetadata_pipeline.bt2_selector.build_point_in_time_book's
OUTPUT (strike, right, expiration, bid, ask, bid_size, ask_size, delta,
quote_age_seconds) so the already-tested evaluate_candidate/select_contract
selection algorithm (and selector_variant_b.py's debit-cap wrapper) run
UNCHANGED regardless of quote source. Delta comes directly from ThetaData's
own greeks endpoint, not a re-derived Black-Scholes solve.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import logging
import re
import threading
import time
from typing import Optional
from zoneinfo import ZoneInfo

import pandas as pd

from thetadata_pipeline.client import ThetaDataUnavailable, bounded_call, get_client
from thetadata_pipeline.schemas import normalize_right
from thetadata_pipeline.schemas import occ_symbol as _build_occ_symbol

ET = ZoneInfo("America/New_York")

FEED_THETADATA = "thetadata"

logger = logging.getLogger("smc.theta_market_data")

_OCC_RE = re.compile(r"^([A-Z]{1,6})(\d{6})([CP])(\d{8})$")

BOOK_COLUMNS = ["expiration", "strike", "right", "bid", "ask", "bid_size",
                "ask_size", "quote_ts", "delta", "underlying_price"]


def parse_occ_symbol(occ: str) -> dict:
    """Reverse of thetadata_pipeline.schemas.occ_symbol -- no parser existed
    in this codebase before now (occ_symbol() only builds one)."""
    m = _OCC_RE.match(occ)
    if not m:
        raise ValueError(f"not a well-formed OCC symbol: {occ!r}")
    root, yymmdd, right, strike_field = m.groups()
    expiration = dt.datetime.strptime(yymmdd, "%y%m%d").date()
    strike = int(strike_field) / 1000.0
    return {"root": root, "expiration": expiration, "right": right, "strike": strike}


def build_occ_symbol(root: str, expiration: dt.date, strike: float, right: str) -> str:
    return _build_occ_symbol(root, expiration, strike, right)


@dataclasses.dataclass
class CacheEntry:
    book: pd.DataFrame           # merged quote+greeks, one row per (strike, right)
    fetched_monotonic: float     # time.monotonic() at fetch completion -- cache-age basis
    fetched_wall: dt.datetime    # UTC wall clock at fetch completion
    expiration: dt.date
    n_quote_rows: int
    n_greeks_rows: int


class ThetaMarketDataCache:
    """Process-wide warm cache of one underlying's near-dated option chain.
    Call refresh() on a background interval (independent of signal
    arrival); get_book()/get_quote_row() only ever read what refresh()
    already fetched -- neither ever triggers a network call itself."""

    def __init__(self, symbol: str = "QQQ", dte_targets: tuple = (0, 1, 2)):
        self.symbol = symbol
        self.dte_targets = dte_targets
        self._entries: dict = {}   # expiration -> CacheEntry
        self._client = None
        self.last_discovery = None   # smc.expirations.ExpirationDiscovery
        # refresh() is intended to run on its own timer thread while the
        # signal path reads. Entries are rebound wholesale under this lock.
        self._lock = threading.RLock()

    def _client_lazy(self):
        if self._client is None:
            self._client = get_client()
        return self._client

    def target_expirations(self, today: Optional[dt.date] = None) -> list:
        """REAL listed expirations from ThetaData metadata, not calendar
        arithmetic.

        The previous version returned today+0/1/2 calendar days, so on a
        Friday it handed refresh() a Saturday and a Sunday -- dates that are
        not listed contracts, producing six "No data found" errors per
        refresh and letting a weekend date reach the cache. Discovery also
        confirms each expiration actually lists strikes before we load
        Greeks for it, so a hollow expiration cannot make the universe warm.

        Falls back to the old calendar guess ONLY if discovery itself
        errors, and records that it did so -- a degraded universe should be
        visible, not silently identical to a healthy one."""
        today = today or dt.datetime.now(ET).date()
        try:
            from smc.expirations import discover_expirations
            disc = discover_expirations(self._client_lazy(), self.symbol,
                                        dte_window=tuple(self.dte_targets),
                                        today=today)
            self.last_discovery = disc
            if disc.ok:
                return list(disc.selected)
            logger.warning("expiration discovery returned nothing usable: %s",
                           disc.as_dict().get("rejected"))
        except Exception as e:  # noqa: BLE001
            logger.error("expiration discovery failed, falling back to calendar: %s", e)
            self.last_discovery = None
        # Fallback: calendar guess, weekends excluded so we never probe them.
        return sorted({today + dt.timedelta(days=d) for d in self.dte_targets
                       if (today + dt.timedelta(days=d)).weekday() < 5})

    def refresh(self, today: Optional[dt.date] = None) -> dict:
        """Fetches quote+greeks snapshots for every target expiration and
        rebuilds the cache. Returns {expiration: n_rows} for observability.
        A single missing/empty expiration (weekend, holiday, no listed
        0DTE that day) does not raise -- only a client-level failure does,
        since ThetaDataUnavailable already distinguishes 'no data for this
        date' from 'the connection/auth is broken'."""
        client = self._client_lazy()
        results = {}
        for exp in self.target_expirations(today):
            try:
                quotes = bounded_call(client.option_snapshot_quote, symbol=self.symbol,
                                       expiration=exp, strike="*", right="both")
            except ThetaDataUnavailable:
                results[exp] = 0
                continue
            if quotes is None or quotes.empty:
                results[exp] = 0
                continue
            try:
                greeks = bounded_call(client.option_snapshot_greeks_first_order,
                                       symbol=self.symbol, expiration=exp,
                                       strike="*", right="both")
            except ThetaDataUnavailable:
                greeks = None

            book = self._merge(quotes, greeks)
            entry = CacheEntry(
                book=book, fetched_monotonic=time.monotonic(),
                fetched_wall=dt.datetime.now(dt.timezone.utc), expiration=exp,
                n_quote_rows=len(quotes), n_greeks_rows=0 if greeks is None else len(greeks),
            )
            # Single atomic rebind of one expiration's entry. CacheEntry is
            # never mutated in place, so a concurrent greek_snapshot() reader
            # always sees either the whole old entry or the whole new one --
            # never a half-rebuilt book.
            with self._lock:
                self._entries[exp] = entry
            results[exp] = len(book)
        return results

    @staticmethod
    def _merge(quotes: pd.DataFrame, greeks: Optional[pd.DataFrame]) -> pd.DataFrame:
        q = quotes.copy()
        q["right"] = q["right"].map(normalize_right)
        q = q.rename(columns={"timestamp": "quote_ts"})
        keep_q = ["expiration", "strike", "right", "bid", "ask", "bid_size", "ask_size", "quote_ts"]
        q = q[[c for c in keep_q if c in q.columns]]

        if greeks is not None and not greeks.empty:
            g = greeks.copy()
            g["right"] = g["right"].map(normalize_right)
            keep_g = ["expiration", "strike", "right", "delta", "underlying_price"]
            g = g[[c for c in keep_g if c in g.columns]]
            merged = q.merge(g, on=["expiration", "strike", "right"], how="left")
        else:
            merged = q.copy()
            merged["delta"] = None
            merged["underlying_price"] = None
        for col in BOOK_COLUMNS:
            if col not in merged.columns:
                merged[col] = None
        return merged

    def get_book(self, right: str, now: Optional[dt.datetime] = None) -> pd.DataFrame:
        """Point-in-time-book-shaped DataFrame across EVERY cached
        expiration for one right, with quote_age_seconds computed against
        `now` -- the same column contract
        bt2_selector.build_point_in_time_book produces, so select_contract()
        runs completely unmodified against it."""
        now = now or dt.datetime.now(dt.timezone.utc)
        right = normalize_right(right)
        frames = []
        for entry in self._entries.values():
            b = entry.book[entry.book["right"] == right].copy()
            if b.empty:
                continue
            qts = pd.to_datetime(b["quote_ts"], utc=True, errors="coerce")
            b["quote_age_seconds"] = (pd.Timestamp(now, tz="UTC") - qts).dt.total_seconds()
            frames.append(b)
        if not frames:
            return pd.DataFrame(columns=BOOK_COLUMNS + ["quote_age_seconds"])
        return pd.concat(frames, ignore_index=True)

    def get_quote_row(self, occ: str, now: Optional[dt.datetime] = None) -> Optional[dict]:
        """Single-contract lookup by OCC symbol (e.g. exit-monitoring an
        already-held position). Returns the SAME cached row get_book()
        would include -- never triggers a fresh network call."""
        parsed = parse_occ_symbol(occ)
        entry = self._entries.get(parsed["expiration"])
        if entry is None:
            return None
        b = entry.book
        row = b[(b["strike"] == parsed["strike"]) & (b["right"] == parsed["right"])]
        if row.empty:
            return None
        now = now or dt.datetime.now(dt.timezone.utc)
        r = row.iloc[0].to_dict()
        qts_raw = r.get("quote_ts")
        qts = pd.Timestamp(qts_raw, tz="UTC") if qts_raw is not None and not pd.isna(qts_raw) else None
        r["quote_age_seconds"] = (
            (pd.Timestamp(now, tz="UTC") - qts).total_seconds()
            if qts is not None else None
        )
        return r

    def greek_snapshot(self, occs) -> dict:
        """ONE atomic read of delta + its provenance for a set of OCC
        symbols, for candidate_universe's merge.

        Returns delta ONLY -- never bid/ask. REST quotes exist in this cache
        but must not reach a selection decision: streaming quotes govern the
        hot path (heff's explicit correction, 2026-08-01). Handing back a
        REST bid/ask here is precisely how this layer would silently regress
        to REST-as-primary, so the shape makes that impossible.

        Each delta carries the fetch timestamp of the CacheEntry it came
        from, so delta age is measured against when the data was actually
        fetched -- not when it was read."""
        out: dict = {}
        with self._lock:
            entries = dict(self._entries)
        now_m = time.monotonic()
        for occ in occs:
            try:
                parsed = parse_occ_symbol(occ)
            except ValueError:
                continue
            entry = entries.get(parsed["expiration"])
            if entry is None:
                continue
            b = entry.book
            row = b[(b["strike"] == parsed["strike"]) & (b["right"] == parsed["right"])]
            if row.empty:
                continue
            delta = row.iloc[0].to_dict().get("delta")
            out[occ] = {
                "delta": None if delta is None or pd.isna(delta) else float(delta),
                "source_monotonic": entry.fetched_monotonic,
                "source_wall": entry.fetched_wall,
            }
        return {"taken_monotonic": now_m,
                "taken_ts": dt.datetime.now(dt.timezone.utc),
                "deltas": out}

    def candidate_occs(self) -> tuple:
        """All real contracts in the latest discovered cache.

        This is the subscription source of truth. It is derived from rows
        ThetaData actually returned, never from calendar or strike guesses.
        """
        with self._lock:
            entries = tuple(self._entries.values())
        occs = set()
        for entry in entries:
            for row in entry.book.itertuples(index=False):
                try:
                    occs.add(build_occ_symbol(
                        self.symbol,
                        row.expiration if isinstance(row.expiration, dt.date)
                        else dt.date.fromisoformat(str(row.expiration)[:10]),
                        float(row.strike), normalize_right(row.right)))
                except (AttributeError, TypeError, ValueError):
                    continue
        return tuple(sorted(occs))

    def cache_age_seconds(self, expiration: dt.date) -> Optional[float]:
        entry = self._entries.get(expiration)
        if entry is None:
            return None
        return time.monotonic() - entry.fetched_monotonic

    def discovery_status(self) -> dict:
        """Provenance for the universe: source, retrieval timestamp,
        per-expiration contract counts and every rejection with its reason."""
        d = getattr(self, "last_discovery", None)
        return d.as_dict() if d is not None else {"ok": False,
                                                  "error": "no discovery recorded"}

    def cache_status(self) -> dict:
        """Observability snapshot for the dashboard/health check -- per
        expiration row counts and cache age, never triggers a fetch."""
        now_m = time.monotonic()
        return {
            str(exp): {
                "n_rows": len(entry.book), "n_quote_rows": entry.n_quote_rows,
                "n_greeks_rows": entry.n_greeks_rows,
                "age_seconds": round(now_m - entry.fetched_monotonic, 2),
                "fetched_wall": entry.fetched_wall.isoformat(),
            }
            for exp, entry in self._entries.items()
        }
