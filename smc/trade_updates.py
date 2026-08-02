"""Alpaca PAPER `trade_updates` WebSocket client.

Replaces smc/lifecycle.py's REST polling of get_order_by_client_id as the
PRIMARY source of order lifecycle truth. REST stays as reconciliation on
startup and after every reconnect -- never as the hot path.

Mirrors smc/theta_stream.py's proven structure deliberately: background
thread owning its own asyncio loop, all shared state behind one RLock, a
physical-connection generation stamped on every event, escalating reconnect
backoff, and a health() dict. A reader who understands theta_stream.py
already understands this file.

Two things this adds that theta_stream.py does not need:

1. Every event carries the CLOSEST ThetaData NBBO at receipt, captured
   synchronously in the receive path from the streaming quote cache. That is
   what makes fill-vs-NBBO slippage measurable per broker event rather than
   reconstructed afterwards from two logs with different clocks.

2. A paper-only guard on the socket URL itself. The REST guard in
   paper_guard.py does not cover this socket, and a live trade_updates
   stream would be a silent authorization breach even though this client
   never submits anything.

PROTOCOL CAVEAT, stated up front because this bit us once already: the
ThetaData subscribe-ack turned out NOT to match its published example (no
`contract` field, only `req_id`), and synthetic tests built on the docs
happily passed while the real thing never confirmed a subscription. The
same risk applies here. Every field read below is read defensively via
.get(), unknown events are counted rather than dropped silently, and the
raw payload is retained on every TradeUpdate. **The message shape is NOT
yet verified against a real Alpaca paper socket -- that is an explicit
Monday gate item, not an assumption this file is entitled to make.**
"""
from __future__ import annotations

import asyncio
import dataclasses
import datetime as dt
import json
import logging
import threading
import time
from typing import Callable, Optional
from urllib.parse import urlparse

import websockets

from smc.paper_guard import PAPER_HOST, PaperGuardViolation
from smc.redact import retain_order_fields

logger = logging.getLogger("smc.trade_updates")

RECONNECT_BACKOFF_SECONDS = (1, 2, 5, 10, 15)
RECV_TIMEOUT_SECONDS = 5.0

# Alpaca lifecycle events we act on. Anything else is retained and counted
# but never silently treated as terminal.
EVENT_NEW = "new"
EVENT_FILL = "fill"
EVENT_PARTIAL_FILL = "partial_fill"
EVENT_CANCELED = "canceled"
EVENT_REJECTED = "rejected"
EVENT_EXPIRED = "expired"
EVENT_REPLACED = "replaced"
EVENT_DONE_FOR_DAY = "done_for_day"

TERMINAL_EVENTS = frozenset({EVENT_FILL, EVENT_CANCELED, EVENT_REJECTED,
                             EVENT_EXPIRED, EVENT_REPLACED, EVENT_DONE_FOR_DAY})
ACCEPTED_EVENTS = frozenset({EVENT_NEW})


def paper_stream_url(base_url: str) -> str:
    """https://paper-api.alpaca.markets -> wss://paper-api.alpaca.markets/stream

    Refuses anything that is not exactly the paper host. This is a separate
    check from paper_guard.assert_paper_endpoint because the socket is a
    separate egress path; sharing the constant, not the code path."""
    parsed = urlparse((base_url or "").strip())
    host = (parsed.hostname or "").lower()
    if host != PAPER_HOST:
        raise PaperGuardViolation(
            f"REFUSING to open a trade_updates socket to {host!r}; only "
            f"{PAPER_HOST!r} is authorized.")
    return f"wss://{host}/stream"


@dataclasses.dataclass(frozen=True)
class TradeUpdate:
    event: str
    client_order_id: Optional[str]
    broker_order_id: Optional[str]
    occ: Optional[str]
    side: Optional[str]
    order_status: Optional[str]
    order_type: Optional[str]
    limit_price: Optional[float]
    qty: Optional[float]
    filled_qty: Optional[float]
    filled_avg_price: Optional[float]
    event_price: Optional[float]
    event_qty: Optional[float]
    position_qty: Optional[float]
    broker_ts: Optional[str]
    receipt_ts: dt.datetime
    receipt_monotonic: float
    generation: int
    # Closest ThetaData NBBO at the instant this event was received.
    theta_bid: Optional[float] = None
    theta_ask: Optional[float] = None
    theta_quote_age_seconds: Optional[float] = None
    theta_exchange_ts: Optional[dt.datetime] = None
    theta_generation: Optional[int] = None
    raw: Optional[dict] = None

    @property
    def is_terminal(self) -> bool:
        return self.event in TERMINAL_EVENTS

    def slippage_vs_theta_mid(self) -> Optional[float]:
        """Signed fill-minus-ThetaData-mid, in premium units. Positive means
        we paid above / sold below the NBBO midpoint. None when either side
        is unavailable -- never silently zero."""
        if self.event_price is None or self.theta_bid is None or self.theta_ask is None:
            return None
        mid = (self.theta_bid + self.theta_ask) / 2.0
        if self.side == "sell":
            return round(mid - self.event_price, 6)
        return round(self.event_price - mid, 6)


def _f(value):
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


class AlpacaTradeUpdatesClient:
    """Process-wide singleton. start() launches a background thread; every
    public method is safe to call from the synchronous main thread."""

    def __init__(self, base_url: str, key: str, secret: str,
                 quote_source: Optional[Callable] = None,
                 on_event: Optional[Callable] = None):
        self.url = paper_stream_url(base_url)   # raises on a non-paper host
        self._key = key
        self._secret = secret
        # Callable occ -> StreamQuote (theta_stream.ThetaStreamClient.get_quote).
        self._quote_source = quote_source
        # Called synchronously in the receive path AFTER the event is cached.
        # Must be fast and must never raise; exceptions are caught and counted.
        self._on_event = on_event

        self._lock = threading.RLock()
        self._by_coid: dict = {}      # client_order_id -> [TradeUpdate, ...]
        self._latest: dict = {}       # client_order_id -> TradeUpdate
        self._connected = False
        self._authenticated = False
        self._generation = 0
        self._event_count = 0
        self._unknown_event_count = 0
        self._malformed_count = 0
        self._reconnect_count = 0
        self._callback_error_count = 0
        self._last_message_monotonic: Optional[float] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        # Set on every successful (re)connect so the runner can force a REST
        # reconciliation pass; cleared by the runner once it has done so.
        self._needs_reconcile = threading.Event()

    # ------------------------------------------------------------- lifecycle
    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop,
                                        name="alpaca-trade-updates", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=10)
        self._thread = None

    def _run_loop(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._connect_forever())
        finally:
            loop.close()

    # -------------------------------------------------------------- reading
    def is_connected(self) -> bool:
        with self._lock:
            return self._connected and self._authenticated

    def current_generation(self) -> int:
        with self._lock:
            return self._generation

    def needs_reconcile(self) -> bool:
        return self._needs_reconcile.is_set()

    def clear_needs_reconcile(self) -> None:
        self._needs_reconcile.clear()

    def latest(self, client_order_id: str) -> Optional[TradeUpdate]:
        with self._lock:
            return self._latest.get(client_order_id)

    def events_for(self, client_order_id: str) -> list:
        with self._lock:
            return list(self._by_coid.get(client_order_id, ()))

    def health(self) -> dict:
        with self._lock:
            now_m = time.monotonic()
            return {
                "connected": self._connected,
                "authenticated": self._authenticated,
                "generation": self._generation,
                "tracked_orders": len(self._by_coid),
                "event_count": self._event_count,
                "unknown_event_count": self._unknown_event_count,
                "malformed_count": self._malformed_count,
                "reconnect_count": self._reconnect_count,
                "callback_error_count": self._callback_error_count,
                "needs_reconcile": self._needs_reconcile.is_set(),
                "seconds_since_last_message": (
                    round(now_m - self._last_message_monotonic, 2)
                    if self._last_message_monotonic is not None else None),
            }

    # ------------------------------------------------------ background loop
    async def _connect_forever(self) -> None:
        backoff_idx = 0
        while not self._stop_event.is_set():
            try:
                async with websockets.connect(self.url) as ws:
                    with self._lock:
                        self._connected = True
                        self._authenticated = False
                        self._generation += 1
                    # Any reconnect may have hidden events; force the runner
                    # to reconcile against REST before trusting stream state.
                    self._needs_reconcile.set()
                    await self._authenticate(ws)
                    backoff_idx = 0
                    await self._recv_loop(ws)
            except (OSError, asyncio.TimeoutError) as e:
                logger.warning("trade_updates disconnected: %s", e)
            except Exception as e:  # noqa: BLE001 -- must never kill the loop
                logger.exception("trade_updates unexpected error: %s", e)
            finally:
                with self._lock:
                    self._connected = False
                    self._authenticated = False
            if self._stop_event.is_set():
                break
            with self._lock:
                self._reconnect_count += 1
            delay = RECONNECT_BACKOFF_SECONDS[min(backoff_idx,
                                                  len(RECONNECT_BACKOFF_SECONDS) - 1)]
            backoff_idx += 1
            await asyncio.sleep(delay)

    async def _authenticate(self, ws) -> None:
        await ws.send(json.dumps({
            "action": "authenticate",
            "data": {"key_id": self._key, "secret_key": self._secret},
        }))
        await ws.send(json.dumps({
            "action": "listen", "data": {"streams": ["trade_updates"]},
        }))

    async def _recv_loop(self, ws) -> None:
        while not self._stop_event.is_set():
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=RECV_TIMEOUT_SECONDS)
            except asyncio.TimeoutError:
                continue
            with self._lock:
                self._last_message_monotonic = time.monotonic()
            self._handle_message(raw)

    # ------------------------------------------------------------- messages
    def _handle_message(self, raw) -> None:
        try:
            msg = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            with self._lock:
                self._malformed_count += 1
            logger.warning("trade_updates malformed message: %.200s", str(raw))
            return

        stream = msg.get("stream")
        data = msg.get("data") or {}

        if stream in ("authorization", "listening"):
            status = (data.get("status") or "").lower()
            action = data.get("action")
            if status == "authorized" or action == "authenticate":
                with self._lock:
                    self._authenticated = True
                logger.info("trade_updates authenticated")
            elif status and status != "authorized":
                logger.error("trade_updates authorization FAILED: %s", data)
            return

        if stream != "trade_updates":
            return

        event = data.get("event")
        order = data.get("order") or {}
        if not event or not order:
            with self._lock:
                self._malformed_count += 1
            return

        coid = order.get("client_order_id")
        occ = order.get("symbol")
        theta = self._quote_source(occ) if (self._quote_source and occ) else None

        now_m = time.monotonic()
        with self._lock:
            gen = self._generation

        upd = TradeUpdate(
            event=event,
            client_order_id=coid,
            broker_order_id=order.get("id"),
            occ=occ,
            side=order.get("side"),
            order_status=order.get("status"),
            order_type=order.get("type"),
            limit_price=_f(order.get("limit_price")),
            qty=_f(order.get("qty")),
            filled_qty=_f(order.get("filled_qty")),
            filled_avg_price=_f(order.get("filled_avg_price")),
            event_price=_f(data.get("price")),
            event_qty=_f(data.get("qty")),
            position_qty=_f(data.get("position_qty")),
            broker_ts=data.get("timestamp"),
            receipt_ts=dt.datetime.now(dt.timezone.utc),
            receipt_monotonic=now_m,
            generation=gen,
            theta_bid=getattr(theta, "bid", None),
            theta_ask=getattr(theta, "ask", None),
            theta_quote_age_seconds=(
                round(now_m - theta.receipt_monotonic, 3) if theta is not None else None),
            theta_exchange_ts=getattr(theta, "exchange_ts", None),
            theta_generation=getattr(theta, "generation", None),
            # Allowlist-projected AND redacted at the source: this dict
            # reaches logs, the dashboard, Telegram and report files, so it
            # must never carry a credential-shaped value. Projection (drop
            # everything unlisted) is the primary control; redaction is the
            # backstop for anything Alpaca adds later.
            raw=retain_order_fields(msg),
        )

        with self._lock:
            self._event_count += 1
            if event not in TERMINAL_EVENTS and event not in ACCEPTED_EVENTS:
                # Retained, not dropped: an unrecognized event must be visible
                # rather than silently discarded (the ThetaData ack lesson).
                self._unknown_event_count += 1
            if coid:
                self._by_coid.setdefault(coid, []).append(upd)
                self._latest[coid] = upd

        if self._on_event is not None:
            try:
                self._on_event(upd)
            except Exception as e:  # noqa: BLE001 -- a callback must never kill the stream
                with self._lock:
                    self._callback_error_count += 1
                logger.exception("trade_updates on_event callback failed: %s", e)
