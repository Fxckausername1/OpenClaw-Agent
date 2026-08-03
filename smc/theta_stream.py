"""Persistent ThetaData Terminal WebSocket client for live quote streaming
(heff's explicit instruction, 2026-08-01: WebSocket streaming governs entry
and exit decisions; REST is discovery/periodic-refresh only, never the hot
path).

Runs in its own background thread with its own asyncio event loop, so the
rest of this codebase's synchronous smc/ package (pipeline.py, lifecycle.py)
needs zero asyncio changes -- it reads this module's thread-safe in-memory
cache like a dict lookup, never blocking on network I/O.

One connection only, per ThetaData's own documented guidance that only one
connection should be made to the local events endpoint -- this module is a
process-wide singleton, same convention as thetadata_pipeline.client's
ThetaClient singleton.

Streaming quote messages carry bid/ask/sizes/timestamp but NOT delta --
delta comes from theta_market_data.py's periodic REST Greeks refresh
instead (see candidate_universe.py for how the two caches merge into one
selection-ready book).
"""
from __future__ import annotations

import asyncio
import collections
import dataclasses
import datetime as dt
import json
import logging
import threading
import time
from typing import Optional
from zoneinfo import ZoneInfo

import websockets

from thetadata_pipeline.schemas import normalize_right, occ_symbol
from .theta_market_data import parse_occ_symbol

logger = logging.getLogger("smc.theta_stream")

ET = ZoneInfo("America/New_York")
FEED_THETADATA = "thetadata"
WS_URI = "ws://127.0.0.1:25520/v1/events"
RECONNECT_BACKOFF_SECONDS = (1, 2, 5, 10, 15)  # capped escalating backoff
RECONCILE_INTERVAL_SECONDS = 5.0
RECV_TIMEOUT_SECONDS = 5.0


@dataclasses.dataclass(frozen=True)
class StreamQuote:
    occ: str
    root: str
    expiration: Optional[dt.date]
    strike: float
    right: str
    bid: Optional[float]
    ask: Optional[float]
    bid_size: Optional[float]
    ask_size: Optional[float]
    exchange_ts: Optional[dt.datetime]   # reconstructed from the message's date+ms_of_day
    receipt_ts: dt.datetime              # local wall clock when this process received it
    receipt_monotonic: float             # time.monotonic() at receipt -- age basis
    # Which physical connection delivered this quote. ThetaStreamClient
    # increments its generation on every successful connect, so a quote
    # received before a reconnect can never be mistaken for a live one
    # afterward: candidate_universe requires quote.generation ==
    # client.current_generation(). Defaults to 0, a sentinel that no live
    # connection ever uses (generations start at 1), so a hand-constructed
    # StreamQuote is never accidentally treated as post-reconnect fresh.
    generation: int = 0

    def age_seconds(self, now_monotonic: Optional[float] = None) -> float:
        now_monotonic = now_monotonic if now_monotonic is not None else time.monotonic()
        return now_monotonic - self.receipt_monotonic

    @property
    def two_sided(self) -> bool:
        return (self.bid is not None and self.ask is not None
                and self.bid > 0 and self.ask > 0 and self.bid < self.ask)


def _ms_of_day_to_utc(date_int: int, ms_of_day: int) -> dt.datetime:
    """ThetaData quote messages carry `date` (YYYYMMDD) and `ms_of_day`
    (milliseconds since ET midnight) -- reconstruct a real UTC timestamp so
    exchange_ts is directly comparable to receipt_ts."""
    y, m, d = date_int // 10000, (date_int // 100) % 100, date_int % 100
    midnight_et = dt.datetime(y, m, d, tzinfo=ET)
    return (midnight_et + dt.timedelta(milliseconds=ms_of_day)).astimezone(dt.timezone.utc)


def _yyyymmdd_to_date(value) -> Optional[dt.date]:
    try:
        if isinstance(value, str) and "-" in value:
            return dt.date.fromisoformat(value)
        value = int(value)
        return dt.date(value // 10000, (value // 100) % 100, value % 100)
    except (ValueError, TypeError):
        return None


def _occ_from_contract(contract: dict) -> Optional[str]:
    try:
        exp = _yyyymmdd_to_date(contract["expiration"])
        if exp is None:
            return None
        strike = float(contract["strike"]) / 1000.0
        right = normalize_right(contract["right"])
        return occ_symbol(contract["root"], exp, strike, right)
    except (KeyError, ValueError, TypeError):
        return None


class ThetaStreamClient:
    """Process-wide singleton. start() launches a background thread running
    its own asyncio loop; every public method is safe to call from the main
    (synchronous) thread -- all shared state is behind self._lock."""

    def __init__(self, uri: str = WS_URI):
        self.uri = uri
        self._lock = threading.RLock()
        self._cache: dict = {}          # occ -> StreamQuote
        self._desired: set = set()      # occ set the caller wants subscribed
        self._pinned: set = set()       # held contracts; never dropped by a refresh
        self._subscribed: set = set()   # occ set actually confirmed by Terminal
        self._connected = False
        self._last_message_monotonic: Optional[float] = None
        self._reconnect_count = 0
        self._dropped_count = 0
        self._malformed_count = 0
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._stop_event = threading.Event()
        self._callback_ms = collections.deque(maxlen=10000)
        self._callback_errors = 0
        # req_id -> occ for subscribe requests still awaiting an ack. The
        # server's SUBSCRIBED ack carries no `contract` field (verified
        # live, 2026-08-01 -- the public API docs example is wrong on this
        # point), only `req_id` -- this is the only way to resolve an ack
        # back to which contract it confirms. Reset on every reconnect
        # (fresh connection, fresh req_id space); NOT reset between
        # reconcile calls within one connection, or a late-arriving ack
        # for an old req_id could resolve to whatever occ has since reused
        # that number.
        self._pending_subs: dict = {}
        self._req_id_counter = 0
        # Monotonically increasing physical-connection counter. 0 = never
        # connected; the first successful connect makes it 1. Every quote is
        # stamped with the generation that delivered it.
        self._generation = 0
        # Optional constant-time consumer installed by run_daemon. It is
        # invoked only after the immutable quote is cached and the cache lock
        # is released, so downstream work cannot corrupt reader state.
        self.on_quote = None

    # ------------------------------------------------------------- lifecycle
    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, name="theta-stream", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=10)
        self._thread = None
        self._loop = None

    def _run_loop(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        try:
            loop.run_until_complete(self._connect_forever())
        finally:
            loop.close()

    # --------------------------------------------------------- subscription
    def set_desired_universe(self, occs: set) -> None:
        """Caller (candidate_universe.py, driven by the periodic REST
        refresh) sets the FULL desired set. The background loop diffs
        against what's actually subscribed and sends only the incremental
        add/remove requests -- never a full unsubscribe/resubscribe unless
        reconnecting.

        Pinned contracts are unioned in and cannot be removed by a universe
        refresh -- see set_pinned_occs."""
        with self._lock:
            self._desired = set(occs) | self._pinned

    def set_pinned_occs(self, occs: set) -> None:
        """Contracts that must stay subscribed regardless of the candidate
        universe: the ones we actually HOLD.

        A held contract is not necessarily a candidate. The next session's
        candidate set is built from fresh Greeks and can easily exclude
        yesterday's strike, and `set_desired_universe` replaces the whole set
        -- so the periodic refresh would unsubscribe the one quote the exit
        monitor needs to evaluate a stop. Pinning is what stops an open
        position from going quote-blind after a routine universe refresh.
        """
        with self._lock:
            self._pinned = set(occs)
            self._desired |= self._pinned

    def pinned_occs(self) -> set:
        with self._lock:
            return set(self._pinned)

    # -------------------------------------------------------------- reading
    def get_quote(self, occ: str) -> Optional[StreamQuote]:
        with self._lock:
            return self._cache.get(occ)

    def is_connected(self) -> bool:
        with self._lock:
            return self._connected

    def current_generation(self) -> int:
        with self._lock:
            return self._generation

    def is_subscribed(self, occ: str) -> bool:
        with self._lock:
            return occ in self._subscribed

    def snapshot(self, occs) -> dict:
        """ONE atomic read of everything a selection decision depends on.

        Taking generation, connected-state, the subscribed set and the
        quotes under a SINGLE lock acquisition is the whole point: reading
        them in separate calls would let a reconnect land in between, so a
        decision could mix a pre-reconnect quote with a post-reconnect
        generation and wrongly conclude the quote was fresh. Callers must
        build a book from this and nothing else.

        Returns a plain dict of immutable/copied values -- once it returns,
        nothing the background thread does can mutate what the caller holds,
        so a quote or Greek arriving mid-selection cannot change the
        decision partway through."""
        with self._lock:
            requested = set(occs)
            return {
                "generation": self._generation,
                "connected": self._connected,
                "taken_monotonic": time.monotonic(),
                "taken_ts": dt.datetime.now(dt.timezone.utc),
                # StreamQuote is a frozen dataclass, so handing out the
                # object itself is safe -- the background thread replaces
                # cache entries wholesale, never mutates one in place.
                "quotes": {occ: self._cache.get(occ) for occ in requested},
                "subscribed": {occ: (occ in self._subscribed) for occ in requested},
            }

    def wait_for_subscriptions(self, timeout: float = 30.0,
                               min_fraction: float = 1.0) -> bool:
        """Block until the desired universe is acknowledged, or timeout.

        Startup previously relied on the 5s reconcile tick to notice a
        universe set after connect. With the universe set BEFORE start(),
        _resubscribe_all covers it on connect and this returns almost
        immediately; the barrier exists so startup can PROVE it rather than
        assume the ordering held.

        min_fraction < 1.0 tolerates a venue refusing a few contracts
        without blocking the daemon forever."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                desired = len(self._desired)
                subscribed = len(self._subscribed)
                connected = self._connected
            if desired == 0:
                return True          # nothing to wait for
            if connected and subscribed >= desired * min_fraction:
                logger.info("subscriptions acknowledged: %d/%d", subscribed, desired)
                return True
            time.sleep(0.1)
        with self._lock:
            logger.warning("subscription wait timed out: %d/%d after %.1fs",
                           len(self._subscribed), len(self._desired), timeout)
        return False

    def health(self) -> dict:
        with self._lock:
            now_m = time.monotonic()
            callbacks = sorted(self._callback_ms)
            callback_dist = {
                "n": len(callbacks),
                "p50": (round(callbacks[int(.50 * (len(callbacks) - 1))], 3)
                        if callbacks else None),
                "p95": (round(callbacks[int(.95 * (len(callbacks) - 1))], 3)
                        if callbacks else None),
                "max": round(callbacks[-1], 3) if callbacks else None,
            }
            return {
                "connected": self._connected,
                "n_cached_quotes": len(self._cache),
                "n_desired": len(self._desired),
                "n_pinned": len(self._pinned),
                "pinned_unsubscribed": sorted(self._pinned - self._subscribed),
                "n_subscribed": len(self._subscribed),
                "generation": self._generation,
                "reconnect_count": self._reconnect_count,
                "dropped_count": self._dropped_count,
                "malformed_count": self._malformed_count,
                "reader_callback_ms": callback_dist,
                "reader_callback_errors": self._callback_errors,
                "seconds_since_last_message": (
                    round(now_m - self._last_message_monotonic, 2)
                    if self._last_message_monotonic is not None else None
                ),
            }

    # ------------------------------------------------------ background loop
    async def _connect_forever(self) -> None:
        backoff_idx = 0
        while not self._stop_event.is_set():
            try:
                async with websockets.connect(self.uri) as ws:
                    with self._lock:
                        self._connected = True
                        self._subscribed = set()
                        self._pending_subs = {}
                        self._req_id_counter = 0
                        # New physical connection => new generation. Quotes
                        # cached from the PREVIOUS connection deliberately
                        # stay in _cache (so health/diagnostics can still
                        # show what went stale and when) but they now carry
                        # an older generation, and candidate_universe treats
                        # a generation mismatch as hard-ineligible. That is
                        # what enforces "a fresh post-reconnect quote is
                        # required" even though the Terminal re-acks every
                        # subscription immediately on reconnect.
                        self._generation += 1
                    backoff_idx = 0
                    logger.info("theta_stream connected")
                    await self._resubscribe_all(ws)
                    await self._recv_loop(ws)
            except (OSError, asyncio.TimeoutError) as e:
                logger.warning("theta_stream disconnected: %s", e)
                with self._lock:
                    self._dropped_count += 1
            except Exception as e:  # noqa: BLE001 -- must never kill the reconnect loop
                logger.exception("theta_stream unexpected error: %s", e)
                with self._lock:
                    self._dropped_count += 1
            finally:
                with self._lock:
                    self._connected = False
            if self._stop_event.is_set():
                break
            with self._lock:
                self._reconnect_count += 1
            delay = RECONNECT_BACKOFF_SECONDS[min(backoff_idx, len(RECONNECT_BACKOFF_SECONDS) - 1)]
            backoff_idx += 1
            await asyncio.sleep(delay)

    def _next_req_id(self) -> int:
        with self._lock:
            self._req_id_counter += 1
            return self._req_id_counter

    async def _send_subscribe(self, ws, occ: str, add: bool) -> None:
        parsed = parse_occ_symbol(occ)
        req_id = self._next_req_id()
        if add:
            with self._lock:
                self._pending_subs[req_id] = occ
        sub = {
            "msg_type": "STREAM", "sec_type": "OPTION", "req_type": "QUOTE",
            "add": add, "id": req_id,
            "contract": {
                "root": parsed["root"],
                "expiration": int(parsed["expiration"].strftime("%Y%m%d")),
                "strike": int(round(parsed["strike"] * 1000)),
                "right": parsed["right"],
            },
        }
        await ws.send(json.dumps(sub))

    async def _resubscribe_all(self, ws) -> None:
        with self._lock:
            desired = set(self._desired)
        for occ in desired:
            await self._send_subscribe(ws, occ, add=True)

    async def _recv_loop(self, ws) -> None:
        last_reconcile = time.monotonic()
        while not self._stop_event.is_set():
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=RECV_TIMEOUT_SECONDS)
            except asyncio.TimeoutError:
                raw = None
            if raw is not None:
                with self._lock:
                    self._last_message_monotonic = time.monotonic()
                self._handle_message(raw)
            if time.monotonic() - last_reconcile > RECONCILE_INTERVAL_SECONDS:
                await self._reconcile_subscriptions(ws)
                last_reconcile = time.monotonic()

    async def _reconcile_subscriptions(self, ws) -> None:
        """Adds/removes subscriptions as the candidate universe changes,
        without a full reconnect -- mission requirement #8 ('Add/remove
        quote subscriptions as contracts enter or leave the candidate
        universe')."""
        with self._lock:
            desired = set(self._desired)
            subscribed = set(self._subscribed)
        to_add = desired - subscribed
        to_remove = subscribed - desired
        for occ in to_add:
            await self._send_subscribe(ws, occ, add=True)
        for occ in to_remove:
            await self._send_subscribe(ws, occ, add=False)
            with self._lock:
                self._subscribed.discard(occ)
                self._cache.pop(occ, None)

    def _handle_message(self, raw) -> None:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            with self._lock:
                self._malformed_count += 1
            logger.warning("theta_stream malformed message: %s", str(raw)[:200])
            return

        header = msg.get("header", {})
        htype = header.get("type")
        if htype == "REQ_RESPONSE":
            if header.get("response") == "SUBSCRIBED":
                # Verified live, 2026-08-01: this ack carries NO `contract`
                # field, only `req_id` -- resolve back to the occ via the
                # pending-subscribe map populated when the request was sent.
                req_id = header.get("req_id")
                with self._lock:
                    occ = self._pending_subs.pop(req_id, None)
                    if occ:
                        self._subscribed.add(occ)
            return
        if htype == "STATUS":
            return  # heartbeat, nothing to cache

        contract = msg.get("contract")
        quote = msg.get("quote")
        if not contract or not quote:
            with self._lock:
                self._malformed_count += 1
            return
        occ = _occ_from_contract(contract)
        if occ is None:
            with self._lock:
                self._malformed_count += 1
            return

        exchange_ts = None
        if quote.get("date") is not None and quote.get("ms_of_day") is not None:
            try:
                exchange_ts = _ms_of_day_to_utc(int(quote["date"]), int(quote["ms_of_day"]))
            except (ValueError, TypeError):
                exchange_ts = None

        exp = _yyyymmdd_to_date(contract.get("expiration"))
        with self._lock:
            gen = self._generation
        sq = StreamQuote(
            occ=occ, root=contract.get("root"), expiration=exp,
            strike=float(contract.get("strike") or 0) / 1000.0,
            right=normalize_right(contract.get("right", "C")),
            bid=quote.get("bid"), ask=quote.get("ask"),
            bid_size=quote.get("bid_size"), ask_size=quote.get("ask_size"),
            exchange_ts=exchange_ts,
            receipt_ts=dt.datetime.now(dt.timezone.utc),
            receipt_monotonic=time.monotonic(),
            generation=gen,
        )
        with self._lock:
            self._cache[occ] = sq
            self._subscribed.add(occ)
        callback = self.on_quote
        if callback is not None:
            callback_started = time.monotonic()
            try:
                callback(sq)
            except Exception as e:  # noqa: BLE001 -- reader must stay alive
                logger.exception("theta_stream on_quote callback failed: %s", e)
                with self._lock:
                    self._callback_errors += 1
            finally:
                with self._lock:
                    self._callback_ms.append((time.monotonic() - callback_started) * 1000.0)
