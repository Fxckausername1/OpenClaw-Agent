"""Asynchronous Telegram delivery: durable outbox + circuit breaker.

Layering, and why each layer exists:

  smc/notify.py         one detached fire-and-forget send (no waiting, ever)
  smc/notify_outbox.py  the DURABLE ledger, anchored on smc_events.id
  this module           breaker + worker that drains the ledger

The critical change from the first version: **the in-memory queue is no
longer the source of truth.** It is a wakeup hint. Every publish commits the
canonical event and its delivery obligation to SQLite FIRST; only then is an
event id offered to the worker. A full queue, an open breaker, a wedged CLI
or a process restart therefore delays delivery without losing it -- the row
is still `pending` in the ledger and gets swept up on the next pass.

The earlier drop-oldest behavior survives only where it is correct: repeated
health/heartbeat rows may be coalesced. A fill, an exit, a rejection or a
reconciliation mismatch is never dropped, never coalesced, and never
abandoned no matter how many attempts fail -- it stays an outstanding
obligation and is surfaced as one.

Telegram still cannot delay trading. `publish()` does one local SQLite
commit and one non-blocking queue hint; every network-ish action (spawning
the openclaw CLI) happens on the worker thread.

Threading: the worker owns its OWN sqlite connection, because a
sqlite3.Connection must not be shared across threads. Callers publish on
their own connection. Two connections to one file is exactly what SQLite's
locking is for.
"""
from __future__ import annotations

import logging
import queue
import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional

from smc import notify
from smc.notify_outbox import (
    STATE_ABANDONED, STATE_DELIVERED, STATE_FAILED, STATE_PENDING, NotifyOutbox,
)

logger = logging.getLogger("smc.notify_queue")

PAPER_PREFIX = "[ALPACA PAPER - NOT LIVE]"
DEFAULT_MAXSIZE = 500
DEFAULT_FAILURE_THRESHOLD = 5
DEFAULT_COOLDOWN_SECONDS = 300.0
DEFAULT_SWEEP_SECONDS = 15.0

STATE_CLOSED = "closed"
STATE_OPEN = "open"
STATE_HALF_OPEN = "half_open"


def with_paper_prefix(message: str) -> str:
    """Idempotent, so a defensively-prefixing caller cannot double the banner."""
    message = (message or "").strip()
    if message.startswith(PAPER_PREFIX):
        return message
    return f"{PAPER_PREFIX} {message}"


def _connect(db_path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=10.0)
    conn.row_factory = sqlite3.Row
    return conn


class NotifyQueue:
    def __init__(self, config, outbox: NotifyOutbox, db_path: Optional[Path] = None,
                 maxsize: int = DEFAULT_MAXSIZE,
                 failure_threshold: int = DEFAULT_FAILURE_THRESHOLD,
                 cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS,
                 sweep_seconds: float = DEFAULT_SWEEP_SECONDS,
                 sender=None, clock=time.monotonic):
        self._config = config
        self._outbox = outbox                 # publisher-side (caller's thread)
        self._db_path = db_path
        self._q: queue.Queue = queue.Queue(maxsize=maxsize)
        self._failure_threshold = failure_threshold
        self._cooldown = cooldown_seconds
        self._sweep_seconds = sweep_seconds
        self._send = sender or notify.send_direct
        self._clock = clock

        self._lock = threading.RLock()
        self._consecutive_failures = 0
        self._opened_at: Optional[float] = None
        self._sent = 0
        self._failed = 0
        self._suppressed = 0
        self._hint_deferred = 0     # queue was full; row remains durable+pending
        self._coalesced = 0
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    # --------------------------------------------------------- publishing
    def publish(self, kind: str, message: str, detail=None,
                position_id: Optional[str] = None,
                client_order_id: Optional[str] = None) -> Optional[int]:
        """Durable-first. Commits the canonical event and its delivery
        obligation, then hints the worker. Returns the canonical event id.

        NEVER blocks and NEVER raises. Returns None only if the durable
        commit itself failed, which is logged and still cannot break the
        caller's risk action."""
        event_id = self._outbox.commit_event(
            kind, with_paper_prefix(message), detail=detail,
            position_id=position_id, client_order_id=client_order_id)
        if event_id is None:
            return None
        try:
            self._q.put_nowait(event_id)
        except queue.Full:
            # NOT a drop: the obligation is already durable and pending, and
            # the periodic sweep will pick it up.
            with self._lock:
                self._hint_deferred += 1
        except Exception as e:  # noqa: BLE001 -- notification must never propagate
            logger.warning("notify hint failed (ignored, row still pending): %s", e)
        return event_id

    def notify_after_commit(self, state_committed: bool, kind: str, message: str,
                            **kw) -> Optional[int]:
        """Same commit-ordering guard as notify.notify_after_commit."""
        if not state_committed:
            logger.error("REFUSING to publish before state commit: %.120s", message)
            return None
        return self.publish(kind, message, **kw)

    # ------------------------------------------------------------- breaker
    def _state_locked(self) -> str:
        if self._consecutive_failures < self._failure_threshold:
            return STATE_CLOSED
        if self._opened_at is None:
            return STATE_OPEN
        if self._clock() - self._opened_at >= self._cooldown:
            return STATE_HALF_OPEN
        return STATE_OPEN

    def state(self) -> str:
        with self._lock:
            return self._state_locked()

    def _record_success(self) -> None:
        with self._lock:
            self._consecutive_failures = 0
            self._opened_at = None
            self._sent += 1

    def _record_failure(self) -> None:
        with self._lock:
            self._consecutive_failures += 1
            self._failed += 1
            if self._consecutive_failures >= self._failure_threshold:
                self._opened_at = self._clock()
                if self._consecutive_failures == self._failure_threshold:
                    logger.error(
                        "telegram circuit breaker OPEN after %d consecutive failures; "
                        "pausing %.0fs. Obligations remain durable. Trading UNAFFECTED.",
                        self._failure_threshold, self._cooldown)

    # ------------------------------------------------------------ draining
    def drain_once(self, outbox: NotifyOutbox, limit: int = 25) -> int:
        """Attempts pending obligations, most urgent first. Returns delivered
        count. Honors the breaker: when OPEN nothing is attempted and rows
        simply stay pending."""
        with self._lock:
            if self._state_locked() == STATE_OPEN:
                self._suppressed += 1
                return 0
        delivered = 0
        for row in outbox.pending(limit=limit):
            with self._lock:
                if self._state_locked() == STATE_OPEN:
                    break        # breaker tripped mid-batch; leave the rest pending
            try:
                ok = bool(self._send(row["message"], self._config))
                err = "" if ok else "send returned False"
            except Exception as e:  # noqa: BLE001
                ok, err = False, repr(e)
            if ok:
                outbox.mark_delivered(row["event_id"])
                self._record_success()
                delivered += 1
            else:
                outbox.mark_failed(row["event_id"], err)
                self._record_failure()
        return delivered

    # -------------------------------------------------------------- worker
    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="smc-notify", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        self._thread = None

    def _run(self) -> None:
        conn = _connect(self._db_path) if self._db_path else None
        outbox = NotifyOutbox(conn) if conn is not None else self._outbox
        last_sweep = 0.0
        try:
            while not self._stop.is_set():
                try:
                    self._q.get(timeout=0.5)     # hint only; the ledger is authoritative
                except queue.Empty:
                    pass
                now = self._clock()
                if now - last_sweep >= self._sweep_seconds or not self._q.empty():
                    last_sweep = now
                    try:
                        self._coalesced += outbox.coalesce_informational()
                        self.drain_once(outbox)
                    except Exception as e:  # noqa: BLE001 -- worker must never die
                        logger.exception("notify worker pass failed: %s", e)
        finally:
            if conn is not None:
                conn.close()

    # -------------------------------------------------------------- status
    def health(self) -> dict:
        with self._lock:
            base = {
                "state": self._state_locked(),
                "hint_queue_depth": self._q.qsize(),
                "sent": self._sent,
                "failed": self._failed,
                "suppressed": self._suppressed,
                "hint_deferred": self._hint_deferred,
                "coalesced": self._coalesced,
                "consecutive_failures": self._consecutive_failures,
            }
        try:
            base["outbox"] = self._outbox.counts()
            base["undelivered_critical"] = len(self._outbox.undelivered_critical())
        except sqlite3.Error:
            base["outbox"] = None
            base["undelivered_critical"] = None
        base["direct_transport"] = notify.direct_health()
        return base
