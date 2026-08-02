"""Detector worker: runs the ~440 ms replay OFF the event loop, instrumented.

Measured (bench_persistent_worker.py): running the replay as a handler
inside the event loop delays a critical event by p95 425 ms and a stop by up
to 288 ms. On a worker thread that falls to p95 0.11 ms, and on a persistent
worker process to 0.89 ms. Thread wins on this single-core box because the
replay is numpy/pandas-bound and releases the GIL; the process pays IPC for
no parallelism. Thread it is -- with a watchdog, because that result depends
on the replay STAYING numeric-heavy.

NO OVERLAPPING REPLAYS. If a confirmed bar arrives while a replay is still
running, exactly ONE coalesced follow-up task is queued -- never a growing
pile. Two concurrent replays on one core would make both late and neither
more correct, and an unbounded queue would turn a slow patch into an
ever-growing backlog.

LATENCY CEILING. `runtime_ceiling_ms` is frozen. Exceeding it does NOT stop
the daemon: it marks the detector degraded, which blocks NEW ENTRIES while
exits, fills and reconciliation continue untouched. Detecting a new entry is
the only thing worth sacrificing; managing an open position never is.

Every timestamp the checkpoint asks for is captured: requested, worker
start, worker finish, runtime, result-queued, daemon receipt -- so delivery
delay is measured rather than inferred.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import logging
import queue
import statistics
import threading
import time
from typing import Callable, Optional

logger = logging.getLogger("smc.detector_worker")

DEFAULT_RUNTIME_CEILING_MS = 900.0     # frozen; replay measured ~440 ms
DEFAULT_DELIVERY_CEILING_MS = 250.0


@dataclasses.dataclass
class DetectorTask:
    task_id: str
    session: str
    reason: str
    requested_monotonic: float
    requested_ts: str
    started_monotonic: Optional[float] = None
    finished_monotonic: Optional[float] = None
    result_queued_monotonic: Optional[float] = None
    received_monotonic: Optional[float] = None
    n_signals: int = 0
    error: Optional[str] = None

    @property
    def runtime_ms(self) -> Optional[float]:
        if self.started_monotonic is None or self.finished_monotonic is None:
            return None
        return round((self.finished_monotonic - self.started_monotonic) * 1000.0, 3)

    @property
    def queue_wait_ms(self) -> Optional[float]:
        if self.started_monotonic is None:
            return None
        return round((self.started_monotonic - self.requested_monotonic) * 1000.0, 3)

    @property
    def delivery_delay_ms(self) -> Optional[float]:
        """result queued -> daemon actually received it."""
        if self.result_queued_monotonic is None or self.received_monotonic is None:
            return None
        return round((self.received_monotonic - self.result_queued_monotonic) * 1000.0, 3)

    @property
    def total_ms(self) -> Optional[float]:
        if self.received_monotonic is None:
            return None
        return round((self.received_monotonic - self.requested_monotonic) * 1000.0, 3)

    def as_dict(self) -> dict:
        d = dataclasses.asdict(self)
        d.update(runtime_ms=self.runtime_ms, queue_wait_ms=self.queue_wait_ms,
                 delivery_delay_ms=self.delivery_delay_ms, total_ms=self.total_ms)
        return d


class DetectorWorker:
    """Owns a single background thread. `run_detect()` is injected (normally
    PersistentDetector.detect) so this is testable without bars."""

    def __init__(self, run_detect: Callable, *, on_result: Optional[Callable] = None,
                 runtime_ceiling_ms: float = DEFAULT_RUNTIME_CEILING_MS,
                 delivery_ceiling_ms: float = DEFAULT_DELIVERY_CEILING_MS,
                 clock: Callable = time.monotonic):
        self.run_detect = run_detect
        self.on_result = on_result
        self.runtime_ceiling_ms = runtime_ceiling_ms
        self.delivery_ceiling_ms = delivery_ceiling_ms
        self.clock = clock

        self._lock = threading.RLock()
        self._running: Optional[DetectorTask] = None
        self._pending: Optional[DetectorTask] = None   # AT MOST ONE, coalesced
        self._results: queue.Queue = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        self._wake = threading.Event()
        self._stop = threading.Event()

        self.history: list = []
        self.ceiling_violations = 0
        self.coalesced = 0
        self._seq = 0

    # ------------------------------------------------------------ submit
    def request(self, session: str, reason: str = "bar_close") -> DetectorTask:
        """Queues a replay. If one is running, exactly ONE follow-up is held;
        a further request coalesces into it rather than stacking."""
        with self._lock:
            self._seq += 1
            task = DetectorTask(
                task_id=f"det-{self._seq}", session=session, reason=reason,
                requested_monotonic=self.clock(),
                requested_ts=dt.datetime.now(dt.timezone.utc).isoformat())
            if self._running is None and self._pending is None:
                self._pending = task
            else:
                if self._pending is not None:
                    self.coalesced += 1
                # Keep the NEWEST request: it covers the newest bar, and the
                # replay is over the whole window anyway, so the older task
                # would compute a strict subset of the same work.
                self._pending = task
        self._wake.set()
        return task

    # ------------------------------------------------------------ worker
    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="smc-detector",
                                        daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        self._thread = None

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(timeout=0.25)
            self._wake.clear()
            while not self._stop.is_set():
                with self._lock:
                    task = self._pending
                    self._pending = None
                    self._running = task
                if task is None:
                    break
                self._execute(task)

    def _execute(self, task: DetectorTask) -> None:
        task.started_monotonic = self.clock()
        try:
            signals = self.run_detect()
            task.n_signals = len(signals or [])
        except Exception as e:  # noqa: BLE001 -- a bad replay must not kill the worker
            signals = []
            task.error = repr(e)
            logger.exception("detector replay failed: %s", e)
        task.finished_monotonic = self.clock()
        task.result_queued_monotonic = self.clock()

        with self._lock:
            self._running = None
            self.history.append(task)
            self.history = self.history[-200:]
            if (task.runtime_ms or 0) > self.runtime_ceiling_ms:
                self.ceiling_violations += 1
                logger.error(
                    "detector runtime %.1f ms exceeds frozen ceiling %.1f ms -- "
                    "entries degraded; exits and reconciliation UNAFFECTED",
                    task.runtime_ms, self.runtime_ceiling_ms)

        self._results.put((task, signals))
        if self.on_result is not None:
            try:
                self.on_result(task, signals)
            except Exception as e:  # noqa: BLE001
                logger.exception("detector on_result callback failed: %s", e)

    # ------------------------------------------------------------ collect
    def poll(self, timeout: float = 0.0):
        """Called from the event loop. Stamps daemon receipt time so delivery
        delay is measured, not inferred."""
        try:
            task, signals = self._results.get(timeout=timeout) if timeout \
                else self._results.get_nowait()
        except queue.Empty:
            return None
        task.received_monotonic = self.clock()
        return task, signals

    # ------------------------------------------------------------- status
    @property
    def backlog_depth(self) -> int:
        with self._lock:
            return (1 if self._pending is not None else 0) + \
                   (1 if self._running is not None else 0)

    @property
    def oldest_task_age_s(self) -> float:
        with self._lock:
            now = self.clock()
            ages = [now - t.requested_monotonic
                    for t in (self._running, self._pending) if t is not None]
        return round(max(ages), 3) if ages else 0.0

    @property
    def degraded(self) -> bool:
        """True when the detector is too slow or too backed up to be trusted
        for NEW entries. Never gates exits."""
        recent = [t.runtime_ms for t in self.history[-10:] if t.runtime_ms is not None]
        slow = bool(recent and recent[-1] > self.runtime_ceiling_ms)
        return slow or self.backlog_depth > 1

    def _pct(self, values, p):
        if not values:
            return None
        xs = sorted(values)
        return round(xs[min(int(p * (len(xs) - 1)), len(xs) - 1)], 3)

    def health(self) -> dict:
        with self._lock:
            runtimes = [t.runtime_ms for t in self.history if t.runtime_ms is not None]
            delivery = [t.delivery_delay_ms for t in self.history
                        if t.delivery_delay_ms is not None]
            last = self.history[-1] if self.history else None
        return {
            "tasks_completed": len(self.history),
            "backlog_depth": self.backlog_depth,
            "oldest_task_age_s": self.oldest_task_age_s,
            "coalesced_requests": self.coalesced,
            "runtime_ceiling_ms": self.runtime_ceiling_ms,
            "ceiling_violations": self.ceiling_violations,
            "degraded": self.degraded,
            "runtime_p50_ms": self._pct(runtimes, 0.50),
            "runtime_p95_ms": self._pct(runtimes, 0.95),
            "runtime_p99_ms": self._pct(runtimes, 0.99),
            "runtime_mean_ms": round(statistics.fmean(runtimes), 3) if runtimes else None,
            "delivery_p50_ms": self._pct(delivery, 0.50),
            "delivery_p95_ms": self._pct(delivery, 0.95),
            "delivery_p99_ms": self._pct(delivery, 0.99),
            "last_task": last.as_dict() if last else None,
        }
