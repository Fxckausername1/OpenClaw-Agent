"""Entry-order lifecycle: immediate submit, bounded lifetime, race-safe.

Implements the order policy heff froze for VARIANT_B_NO_SWEEP's PAPER
window. The 20-second TTL is a MAXIMUM LIFETIME, never an intentional delay:
submission happens the instant a signal produces a contract, and the TTL only
governs how long an unfilled order is allowed to rest.

A tickable state machine rather than a sleep loop, for two reasons: the
daemon can tick it as fast as it likes (the hot path is the venue round
trip, not this object), and every timing case -- cancel/fill races included
-- is deterministic under test instead of raced.

    SUBMITTED ─fill──────────────────────────────► FILLED
        │
        └─TTL──► CANCEL_REQUESTED ─canceled──────► CANCELLED
                        │
                        └─fill───────────────────► FILLED_AFTER_CANCEL_REQUEST

The bottom-right transition is the one that matters and the one naive code
gets wrong. **"Cancel requested" is NEVER "cancel confirmed."** A DELETE
returning 2xx means the venue ACCEPTED the request; the order can still fill
microseconds later. Until a terminal `canceled` event arrives, this object
reports the attempt as still live, and a fill arriving in that window
produces a REAL POSITION that must be managed -- not an error, not a discard.
Treating a requested cancel as final is how an unmanaged position appears,
which is exactly the 2026-07-31 failure class.

Marketability is sampled continuously against the live ThetaData stream
while the order rests, so "the quote moved away from our limit" is recorded
evidence rather than an inference drawn afterwards from two logs.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
from typing import Callable, Optional

# States
SUBMITTED = "SUBMITTED"
CANCEL_REQUESTED = "CANCEL_REQUESTED"
FILLED = "FILLED"
CANCELLED = "CANCELLED"
FILLED_AFTER_CANCEL_REQUEST = "FILLED_AFTER_CANCEL_REQUEST"
REJECTED = "REJECTED"

TERMINAL_STATES = frozenset({FILLED, CANCELLED, FILLED_AFTER_CANCEL_REQUEST, REJECTED})
# States in which a real position exists and MUST be managed.
POSITION_STATES = frozenset({FILLED, FILLED_AFTER_CANCEL_REQUEST})

# Fill-latency buckets. The 2026-07-31 entries were bimodal (eight fills in
# 0-2s, then 66s and 437s), so the boundaries are drawn to make that split
# visible rather than averaged away.
FILL_LATENCY_BUCKETS = (
    ("0-1s", 0.0, 1.0),
    ("1-2s", 1.0, 2.0),
    ("2-5s", 2.0, 5.0),
    ("5-20s", 5.0, 20.0),
    ("over-20s", 20.0, float("inf")),
)


def fill_latency_bucket(seconds: Optional[float]) -> Optional[str]:
    if seconds is None:
        return None
    for label, lo, hi in FILL_LATENCY_BUCKETS:
        if lo <= seconds < hi:
            return label
    return FILL_LATENCY_BUCKETS[-1][0]


@dataclasses.dataclass(frozen=True)
class QuoteSample:
    """One observation of the market while our order rested."""
    offset_seconds: float
    bid: Optional[float]
    ask: Optional[float]
    marketable: bool          # is our buy limit still >= the ask?


@dataclasses.dataclass
class EntryAttempt:
    client_order_id: str
    occ: str
    limit_price: float
    quantity: int
    ttl_seconds: float
    submitted_monotonic: float
    submitted_ts: dt.datetime
    submit_latency_ms: Optional[float] = None
    broker_order_id: Optional[str] = None
    stage_latency: dict = dataclasses.field(default_factory=dict)

    state: str = SUBMITTED
    fill_price: Optional[float] = None
    filled_qty: float = 0.0
    fill_monotonic: Optional[float] = None
    cancel_requested_monotonic: Optional[float] = None
    cancel_request_accepted: Optional[bool] = None
    reject_reason: Optional[str] = None
    samples: list = dataclasses.field(default_factory=list)

    # ------------------------------------------------------------- queries
    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    @property
    def has_position(self) -> bool:
        """True when a real position exists and must be managed -- including
        the late-fill-after-cancel-request case."""
        return self.state in POSITION_STATES and self.filled_qty > 0

    @property
    def fill_latency_seconds(self) -> Optional[float]:
        if self.fill_monotonic is None:
            return None
        return round(self.fill_monotonic - self.submitted_monotonic, 4)

    @property
    def fill_latency_bucket(self) -> Optional[str]:
        return fill_latency_bucket(self.fill_latency_seconds)

    def age_seconds(self, now_monotonic: float) -> float:
        return now_monotonic - self.submitted_monotonic

    def ttl_expired(self, now_monotonic: float) -> bool:
        return self.age_seconds(now_monotonic) >= self.ttl_seconds

    # ------------------------------------------------- marketability sampling
    def sample_quote(self, now_monotonic: float, quote) -> Optional[QuoteSample]:
        """Records where the market was while we rested. Called on every tick;
        cheap by design (no I/O -- the quote comes from the in-memory stream
        cache)."""
        if self.terminal:
            return None
        bid = getattr(quote, "bid", None)
        ask = getattr(quote, "ask", None)
        marketable = ask is not None and self.limit_price >= ask
        s = QuoteSample(offset_seconds=round(self.age_seconds(now_monotonic), 4),
                        bid=bid, ask=ask, marketable=marketable)
        self.samples.append(s)
        return s

    @property
    def still_marketable(self) -> Optional[bool]:
        return self.samples[-1].marketable if self.samples else None

    def quote_movement(self) -> dict:
        """Summary of what the market did during the attempt -- reported per
        attempt so a non-fill is explainable ('the ask moved 6c away') rather
        than merely recorded as a miss."""
        asks = [s.ask for s in self.samples if s.ask is not None]
        if not asks:
            return {"samples": len(self.samples), "ask_first": None, "ask_last": None,
                    "ask_max_adverse": None, "ticks_ever_unmarketable": 0}
        first = asks[0]
        return {
            "samples": len(self.samples),
            "ask_first": first,
            "ask_last": asks[-1],
            "ask_max_adverse": round(max(asks) - first, 4),
            "ticks_ever_unmarketable": sum(1 for s in self.samples if not s.marketable),
        }

    # --------------------------------------------------------- transitions
    def on_trade_update(self, event: str, *, price=None, filled_qty=None,
                        broker_order_id=None, now_monotonic: float,
                        reason: str = "") -> str:
        """Applies an Alpaca trade_updates event. trade_updates is the PRIMARY
        source of order truth; REST reconciliation only backfills what the
        stream missed."""
        if broker_order_id and not self.broker_order_id:
            self.broker_order_id = broker_order_id

        if event in ("fill", "partial_fill"):
            if price is not None:
                self.fill_price = float(price)
            if filled_qty is not None:
                self.filled_qty = float(filled_qty)
            if event == "fill":
                self.fill_monotonic = now_monotonic
                # THE race: a fill landing after we asked to cancel is a real
                # position, not an error to swallow.
                self.state = (FILLED_AFTER_CANCEL_REQUEST
                              if self.state == CANCEL_REQUESTED else FILLED)
        elif event == "canceled":
            if self.filled_qty > 0:
                # Partial then cancelled: a position exists for the filled part.
                self.fill_monotonic = self.fill_monotonic or now_monotonic
                self.state = (FILLED_AFTER_CANCEL_REQUEST
                              if self.state == CANCEL_REQUESTED else FILLED)
            else:
                self.state = CANCELLED
        elif event in ("rejected", "expired"):
            self.reject_reason = reason or event
            self.state = REJECTED if event == "rejected" else CANCELLED
        return self.state

    def request_cancel(self, cancel_fn: Callable, now_monotonic: float) -> bool:
        """Asks the venue to cancel. Records that we ASKED -- never that it
        happened. Returns whether the request was accepted, which is not the
        same question as whether the order is dead."""
        if self.terminal or self.state == CANCEL_REQUESTED:
            return False
        self.cancel_requested_monotonic = now_monotonic
        self.state = CANCEL_REQUESTED
        try:
            call = cancel_fn(self.broker_order_id)
            self.cancel_request_accepted = bool(getattr(call, "ok", call))
        except Exception:  # noqa: BLE001 -- a failed cancel REQUEST must not
            # terminate the attempt; the order may still be live and must keep
            # being watched.
            self.cancel_request_accepted = False
        return bool(self.cancel_request_accepted)

    def tick(self, now_monotonic: float, quote=None,
             cancel_fn: Optional[Callable] = None) -> str:
        """One high-frequency step: sample the market, and cancel once the
        TTL is exhausted. Never sleeps, never blocks, never re-prices."""
        if self.terminal:
            return self.state
        if quote is not None:
            self.sample_quote(now_monotonic, quote)
        if self.state == SUBMITTED and self.ttl_expired(now_monotonic) and cancel_fn:
            self.request_cancel(cancel_fn, now_monotonic)
        return self.state

    # ------------------------------------------------------------ reporting
    def summary(self) -> dict:
        return {
            "client_order_id": self.client_order_id,
            "occ": self.occ,
            "state": self.state,
            "has_position": self.has_position,
            "limit_price": self.limit_price,
            "fill_price": self.fill_price,
            "filled_qty": self.filled_qty,
            "submit_latency_ms": self.submit_latency_ms,
            "stage_latency": dict(self.stage_latency),
            "fill_latency_seconds": self.fill_latency_seconds,
            "fill_latency_bucket": self.fill_latency_bucket,
            "cancel_requested": self.cancel_requested_monotonic is not None,
            "cancel_request_accepted": self.cancel_request_accepted,
            "cancel_confirmed": self.state == CANCELLED,
            "reject_reason": self.reject_reason,
            "quote_movement": self.quote_movement(),
        }
