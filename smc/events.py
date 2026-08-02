"""Canonical event envelope and the daemon's event-driven bus.

NOT a tick loop. This box has ONE core shared with the GEX engine, the
scanners and the nightly jobs; a spin loop calling tick() would burn that
core to poll for something that already knows how to announce itself.

The bus is a `threading.Condition` over a priority heap. A consumer blocks
in `Condition.wait(timeout)` where the timeout is computed as *exactly* the
interval until the next scheduled deadline. So the loop wakes for one of
five reasons and no others:

    * a ThetaData quote event was published
    * an Alpaca trade_updates event was published
    * a monotonic deadline came due (entry TTL, reconcile interval, EOD)
    * a reconnect / reconciliation event was published
    * shutdown

An entry TTL therefore costs ONE scheduled wakeup, not thousands of clock
reads. Idle cost is a thread parked in a futex.

DROP POLICY. Critical events are never dropped, never coalesced, and ignore
maxsize entirely -- the bound exists to stop informational chatter from
growing without limit, and applying it to a fill would be the bug it is
meant to prevent. Never droppable:

    signals, order acknowledgements, partial/full fills, cancel and reject
    events, exit triggers, reconciliation events

Informational events (health, heartbeat, stream status) may be dropped or
coalesced when the bus is saturated, and both are counted.

Every envelope carries the timestamps needed to prove latency after the
fact: source, receipt, queued, processing-start -- from which queue delay,
handler duration and event-loop lag are derived rather than guessed.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import heapq
import itertools
import logging
import threading
import time
import uuid
from typing import Any, Optional

logger = logging.getLogger("smc.events")

# ---- event types ------------------------------------------------------
EV_SIGNAL = "signal"
EV_QUOTE = "quote"
EV_ORDER_ACK = "order_ack"
EV_PARTIAL_FILL = "partial_fill"
EV_FILL = "fill"
EV_CANCEL = "cancel"
EV_REJECT = "reject"
EV_EXIT_TRIGGER = "exit_trigger"
EV_RECONCILE = "reconcile"
EV_RECONNECT = "reconnect"
EV_DEADLINE = "deadline"
EV_HEALTH = "health"
EV_SHUTDOWN = "shutdown"

# Never dropped, never coalesced, never bounded.
CRITICAL_TYPES = frozenset({
    EV_SIGNAL, EV_ORDER_ACK, EV_PARTIAL_FILL, EV_FILL, EV_CANCEL, EV_REJECT,
    EV_EXIT_TRIGGER, EV_RECONCILE, EV_RECONNECT, EV_DEADLINE, EV_SHUTDOWN,
})
# May be dropped or coalesced under saturation.
INFORMATIONAL_TYPES = frozenset({EV_HEALTH, EV_QUOTE})

# Lower drains first.
PRIORITY = {
    EV_SHUTDOWN: 0,
    EV_FILL: 1, EV_PARTIAL_FILL: 1, EV_CANCEL: 1, EV_REJECT: 1, EV_ORDER_ACK: 1,
    EV_EXIT_TRIGGER: 1,
    EV_DEADLINE: 2,
    EV_SIGNAL: 3,
    EV_RECONCILE: 4, EV_RECONNECT: 4,
    EV_QUOTE: 6,
    EV_HEALTH: 9,
}
DEFAULT_PRIORITY = 5
DEFAULT_MAXSIZE = 2000


def _now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


@dataclasses.dataclass(frozen=True)
class Event:
    """The one canonical envelope. Every field the daemon needs to correlate
    a decision with the data it was made from, and to prove how long each
    hop took."""
    event_id: str
    event_type: str
    priority: int
    critical: bool
    # correlation
    signal_id: Optional[str] = None
    trade_id: Optional[str] = None
    order_id: Optional[str] = None
    position_id: Optional[str] = None
    client_order_id: Optional[str] = None
    # timing
    source_ts: Optional[dt.datetime] = None       # when the origin says it happened
    receipt_ts: Optional[dt.datetime] = None      # when this process first saw it
    receipt_monotonic: Optional[float] = None
    queued_monotonic: Optional[float] = None      # when it entered the bus
    # provenance
    stream_generation: Optional[int] = None
    quote: Any = None                             # StreamQuote snapshot, if relevant
    payload: Any = None

    def queue_delay_seconds(self, processing_monotonic: float) -> Optional[float]:
        if self.queued_monotonic is None:
            return None
        return round(processing_monotonic - self.queued_monotonic, 6)


def make_event(event_type: str, **kw) -> Event:
    now_m = time.monotonic()
    return Event(
        event_id=kw.pop("event_id", None) or f"ev-{uuid.uuid4().hex[:16]}",
        event_type=event_type,
        priority=kw.pop("priority", PRIORITY.get(event_type, DEFAULT_PRIORITY)),
        critical=kw.pop("critical", event_type in CRITICAL_TYPES),
        receipt_ts=kw.pop("receipt_ts", None) or _now_utc(),
        receipt_monotonic=kw.pop("receipt_monotonic", None) or now_m,
        **kw,
    )


@dataclasses.dataclass
class BusMetrics:
    published: int = 0
    delivered: int = 0
    dropped_informational: int = 0
    coalesced_informational: int = 0
    depth_high_water: int = 0
    max_queue_delay_ms: float = 0.0
    max_handler_ms: float = 0.0
    max_loop_lag_ms: float = 0.0
    total_handler_ms: float = 0.0
    late_wakeups: int = 0

    def as_dict(self, depth: int, scheduled: int) -> dict:
        return {
            "published": self.published, "delivered": self.delivered,
            "dropped_informational": self.dropped_informational,
            "coalesced_informational": self.coalesced_informational,
            "depth": depth, "depth_high_water": self.depth_high_water,
            "scheduled_deadlines": scheduled,
            "max_queue_delay_ms": round(self.max_queue_delay_ms, 3),
            "max_handler_ms": round(self.max_handler_ms, 3),
            "max_loop_lag_ms": round(self.max_loop_lag_ms, 3),
            "total_handler_ms": round(self.total_handler_ms, 3),
            "late_wakeups": self.late_wakeups,
        }


class EventBus:
    """Priority queue + deadline scheduler behind one Condition."""

    def __init__(self, maxsize: int = DEFAULT_MAXSIZE, clock=time.monotonic):
        self._cv = threading.Condition()
        self._heap: list = []          # (priority, seq, Event)
        self._deadlines: list = []     # (due_monotonic, seq, Event)
        self._seq = itertools.count()
        self._maxsize = maxsize
        self._clock = clock
        self._closed = False
        self.metrics = BusMetrics()
        # Coalescing key -> heap seq, so a superseded health update can be
        # replaced rather than queued twice.
        self._coalesce_index: dict = {}

    # ------------------------------------------------------------ publish
    def publish(self, event: Event, coalesce_key: Optional[str] = None) -> bool:
        """Never blocks. Returns False only when an INFORMATIONAL event was
        dropped; a critical event is always accepted."""
        with self._cv:
            if self._closed:
                return False
            if not event.critical and len(self._heap) >= self._maxsize:
                self.metrics.dropped_informational += 1
                return False
            if coalesce_key is not None and not event.critical:
                existing = self._coalesce_index.get(coalesce_key)
                if existing is not None:
                    for i, (p, s, _e) in enumerate(self._heap):
                        if s == existing:
                            self._heap[i] = (p, s, dataclasses.replace(
                                event, queued_monotonic=self._clock()))
                            heapq.heapify(self._heap)
                            self.metrics.coalesced_informational += 1
                            self._cv.notify()
                            return True
            seq = next(self._seq)
            queued = dataclasses.replace(event, queued_monotonic=self._clock())
            heapq.heappush(self._heap, (queued.priority, seq, queued))
            if coalesce_key is not None and not queued.critical:
                self._coalesce_index[coalesce_key] = seq
            self.metrics.published += 1
            self.metrics.depth_high_water = max(self.metrics.depth_high_water,
                                                len(self._heap))
            self._cv.notify()
            return True

    def schedule(self, due_monotonic: float, event: Event) -> None:
        """Register a monotonic deadline. Costs ONE wakeup at the due time --
        no clock polling. This is how entry TTLs, reconcile intervals and the
        EOD flatten are driven."""
        with self._cv:
            if self._closed:
                return
            heapq.heappush(self._deadlines, (due_monotonic, next(self._seq), event))
            self._cv.notify()

    def cancel_scheduled(self, event_id: str) -> bool:
        """Withdraw a deadline whose reason disappeared (e.g. the order
        filled before its TTL)."""
        with self._cv:
            for i, (due, seq, ev) in enumerate(self._deadlines):
                if ev.event_id == event_id:
                    self._deadlines.pop(i)
                    heapq.heapify(self._deadlines)
                    return True
        return False

    # --------------------------------------------------------------- read
    def _due_deadlines_locked(self, now: float) -> None:
        while self._deadlines and self._deadlines[0][0] <= now:
            due, _seq, ev = heapq.heappop(self._deadlines)
            lateness_ms = (now - due) * 1000.0
            if lateness_ms > 50.0:
                self.metrics.late_wakeups += 1
            self.metrics.max_loop_lag_ms = max(self.metrics.max_loop_lag_ms, lateness_ms)
            seq = next(self._seq)
            heapq.heappush(self._heap, (ev.priority, seq,
                                        dataclasses.replace(ev, queued_monotonic=due)))
            self.metrics.published += 1

    def get(self, timeout: Optional[float] = None) -> Optional[Event]:
        """Blocks until an event is available, a deadline comes due, or
        `timeout` elapses. The wait interval is the exact time to the next
        deadline, so the thread parks rather than spins."""
        deadline_wall = None if timeout is None else self._clock() + timeout
        with self._cv:
            while True:
                now = self._clock()
                self._due_deadlines_locked(now)
                if self._heap:
                    _p, seq, ev = heapq.heappop(self._heap)
                    for k, v in list(self._coalesce_index.items()):
                        if v == seq:
                            del self._coalesce_index[k]
                    self.metrics.delivered += 1
                    return ev
                if self._closed:
                    return None
                waits = []
                if self._deadlines:
                    waits.append(max(self._deadlines[0][0] - now, 0.0))
                if deadline_wall is not None:
                    waits.append(max(deadline_wall - now, 0.0))
                wait_for = min(waits) if waits else None
                if wait_for is not None and wait_for <= 0:
                    if deadline_wall is not None and now >= deadline_wall and not self._deadlines:
                        return None
                self._cv.wait(timeout=wait_for)
                if (deadline_wall is not None and self._clock() >= deadline_wall
                        and not self._heap and not self._deadlines):
                    return None

    def record_handled(self, event: Event, processing_monotonic: float,
                       handler_seconds: float) -> None:
        """Feeds the latency instrumentation. Called by the loop, not by
        handlers, so no handler can forget to."""
        with self._cv:
            qd = event.queue_delay_seconds(processing_monotonic)
            if qd is not None:
                self.metrics.max_queue_delay_ms = max(
                    self.metrics.max_queue_delay_ms, qd * 1000.0)
            ms = handler_seconds * 1000.0
            self.metrics.max_handler_ms = max(self.metrics.max_handler_ms, ms)
            self.metrics.total_handler_ms += ms

    def close(self) -> None:
        with self._cv:
            self._closed = True
            self._cv.notify_all()

    def health(self) -> dict:
        with self._cv:
            return self.metrics.as_dict(len(self._heap), len(self._deadlines))
