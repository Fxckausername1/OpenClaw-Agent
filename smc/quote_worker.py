"""Bounded, nonblocking dispatch for streaming option quotes.

The ThetaData WebSocket thread may parse, cache and enqueue a quote, but it
must never wait for SQLite, Alpaca, Telegram or dashboard I/O.  This worker
owns the slower exit-decision/submission side of that boundary.

The queue stores at most one pending quote per OCC.  A newer quote replaces
an older pending quote for the same contract, which is the useful meaning of
"coalescing" for threshold decisions.  When capacity is exhausted, quotes for
currently open positions displace non-position quotes first.
"""
from __future__ import annotations

import collections
import logging
import statistics
import threading
import time
from typing import Callable, Optional

logger = logging.getLogger("smc.quote_worker")


class QuoteExitWorker:
    def __init__(self, *, handle_quote: Callable,
                 is_protected_occ: Optional[Callable[[str], bool]] = None,
                 maxsize: int = 512, clock: Callable = time.monotonic):
        if maxsize < 2:
            raise ValueError("maxsize must be >= 2")
        self.handle_quote = handle_quote
        self.is_protected_occ = is_protected_occ or (lambda _occ: False)
        self.maxsize = int(maxsize)
        self.clock = clock

        self._cv = threading.Condition()
        self._pending = collections.OrderedDict()  # occ -> (quote, enqueued_mono)
        self._stop = False
        self._thread = None
        self._last_receipt = None

        self.received = 0
        self.processed = 0
        self.coalesced = 0
        self.dropped_non_position = 0
        self.evicted_non_position = 0
        self.evicted_protected = 0
        self.errors = 0
        self.depth_high_water = 0
        self.reader_callback_ms = collections.deque(maxlen=10000)
        self.receive_gap_ms = collections.deque(maxlen=10000)
        self.queue_wait_ms = collections.deque(maxlen=10000)
        self.handler_ms = collections.deque(maxlen=10000)

    def start(self) -> None:
        with self._cv:
            if self._thread is not None:
                return
            self._stop = False
            self._thread = threading.Thread(target=self._run, name="smc-exit-quotes",
                                            daemon=True)
            self._thread.start()

    def submit(self, quote) -> bool:
        """Constant-time, nonblocking reader-side handoff."""
        t0 = self.clock()
        occ = getattr(quote, "occ", None)
        if not occ:
            return False
        protected = bool(self.is_protected_occ(occ))
        accepted = True
        with self._cv:
            self.received += 1
            if self._last_receipt is not None:
                self.receive_gap_ms.append((t0 - self._last_receipt) * 1000.0)
            self._last_receipt = t0

            if occ in self._pending:
                self._pending[occ] = (quote, t0)
                self._pending.move_to_end(occ)
                self.coalesced += 1
            elif len(self._pending) < self.maxsize:
                self._pending[occ] = (quote, t0)
            elif protected:
                victim = next((key for key in self._pending
                               if not self.is_protected_occ(key)), None)
                if victim is None:
                    victim = next(iter(self._pending))
                    self.evicted_protected += 1
                else:
                    self.evicted_non_position += 1
                self._pending.pop(victim, None)
                self._pending[occ] = (quote, t0)
            else:
                self.dropped_non_position += 1
                accepted = False
            self.depth_high_water = max(self.depth_high_water, len(self._pending))
            if accepted:
                self._cv.notify()
        self.reader_callback_ms.append((self.clock() - t0) * 1000.0)
        return accepted

    def _run(self) -> None:
        while True:
            with self._cv:
                while not self._pending and not self._stop:
                    self._cv.wait(timeout=0.5)
                if self._stop and not self._pending:
                    return
                _occ, (quote, queued_at) = self._pending.popitem(last=False)
            started = self.clock()
            self.queue_wait_ms.append((started - queued_at) * 1000.0)
            try:
                self.handle_quote(quote)
            except Exception as exc:  # noqa: BLE001 -- worker must survive
                self.errors += 1
                logger.exception("exit quote handler failed: %s", exc)
            self.handler_ms.append((self.clock() - started) * 1000.0)
            self.processed += 1

    def stop(self, timeout: float = 10.0, drain: bool = True) -> None:
        with self._cv:
            if not drain:
                self._pending.clear()
            self._stop = True
            self._cv.notify_all()
            thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)
        with self._cv:
            self._thread = None

    @staticmethod
    def _dist(values) -> dict:
        xs = sorted(values)
        if not xs:
            return {"n": 0, "p50": None, "p95": None, "max": None}
        def pct(p):
            return round(xs[min(int(p * (len(xs) - 1)), len(xs) - 1)], 3)
        return {"n": len(xs), "p50": pct(0.50), "p95": pct(0.95),
                "max": round(xs[-1], 3), "mean": round(statistics.fmean(xs), 3)}

    def health(self) -> dict:
        with self._cv:
            depth = len(self._pending)
        return {
            "running": self._thread is not None and self._thread.is_alive(),
            "depth": depth, "depth_high_water": self.depth_high_water,
            "received": self.received, "processed": self.processed,
            "coalesced": self.coalesced,
            "dropped_non_position": self.dropped_non_position,
            "evicted_non_position": self.evicted_non_position,
            "evicted_protected": self.evicted_protected,
            "errors": self.errors,
            "reader_callback_ms": self._dist(self.reader_callback_ms),
            "receive_gap_ms": self._dist(self.receive_gap_ms),
            "queue_wait_ms": self._dist(self.queue_wait_ms),
            "handler_ms": self._dist(self.handler_ms),
        }
