"""Confirmed-minute scheduling and bar-availability measurement.

The scheduler emits at most one detector request for each completed market
minute.  The cycle fetches only today's Alpaca IEX bars, waits briefly for the
just-closed bar to become visible, ingests it, then runs the unchanged replay.
Both pieces are independent background threads; neither runs on the quote or
order event loops.
"""
from __future__ import annotations

import datetime as dt
import logging
import statistics
import threading
import time
from typing import Callable
from zoneinfo import ZoneInfo

import pandas as pd

from smc import calendar as smc_calendar

logger = logging.getLogger("smc.live_detector_clock")
ET = ZoneInfo("America/New_York")


class LiveDetectorCycle:
    def __init__(self, detector, *, availability_timeout: float = 2.5,
                 poll_seconds: float = 0.10, monotonic: Callable = time.monotonic,
                 now_utc: Callable = lambda: dt.datetime.now(dt.timezone.utc),
                 sleep: Callable = time.sleep):
        self.detector = detector
        self.availability_timeout = float(availability_timeout)
        self.poll_seconds = float(poll_seconds)
        self.monotonic = monotonic
        self.now_utc = now_utc
        self.sleep = sleep
        self.history = []

    def __call__(self):
        started = self.monotonic()
        now = self.now_utc()
        session = now.astimezone(ET).date().isoformat()
        expected_open = now.replace(second=0, microsecond=0) - dt.timedelta(minutes=1)
        expected_close = expected_open + dt.timedelta(minutes=1)
        fetch_ms = 0.0
        polls = 0
        appended = 0
        available_at = None
        error = None
        session_rollover = False

        try:
            current_session = getattr(self.detector, "_today_session", None)
            if current_session and current_session != session:
                known = set(getattr(self.detector, "_history", {}).keys())
                if current_session < session:
                    known.add(current_session)
                prior = sorted(s for s in known if s < session)[-5:]
                # Rebuild once at the session boundary. Without this, a
                # daemon kept overnight rejects every new day's bar because
                # ingest_today still validates against yesterday.
                self.detector.warmup(session=session, prior_sessions=prior)
                session_rollover = True
            while self.monotonic() - started <= self.availability_timeout:
                t0 = self.monotonic()
                frame = self.detector.fetch(session)
                fetch_ms += (self.monotonic() - t0) * 1000.0
                polls += 1
                result = self.detector.ingest_today(frame)
                appended += int(result.get("appended", 0))
                if frame is not None and not frame.empty:
                    newest = pd.to_datetime(frame["t"], utc=True).max().to_pydatetime()
                    if newest >= expected_open:
                        available_at = self.monotonic()
                        break
                self.sleep(self.poll_seconds)
            signals = self.detector.detect() if available_at is not None else []
        except Exception as exc:
            signals = []
            error = repr(exc)
            logger.exception("live detector cycle failed: %s", exc)

        finished = self.monotonic()
        metric = {
            "session": session,
            "bar_open_utc": expected_open.isoformat(),
            "bar_close_utc": expected_close.isoformat(),
            "polls": polls, "fetch_ms": round(fetch_ms, 3),
            "availability_ms": (round((available_at - started) * 1000.0, 3)
                                if available_at is not None else None),
            "cycle_ms": round((finished - started) * 1000.0, 3),
            "replay_ms": getattr(self.detector, "last_runtime_ms", None),
            "appended": appended, "signals": len(signals), "error": error,
            "session_rollover": session_rollover,
        }
        self.history.append(metric)
        self.history = self.history[-500:]
        return signals

    @staticmethod
    def _dist(values):
        xs = sorted(v for v in values if v is not None)
        if not xs:
            return {"n": 0, "p50": None, "p95": None, "max": None}
        return {"n": len(xs), "p50": round(xs[int(.50 * (len(xs) - 1))], 3),
                "p95": round(xs[int(.95 * (len(xs) - 1))], 3),
                "max": round(xs[-1], 3), "mean": round(statistics.fmean(xs), 3)}

    def health(self):
        return {
            "cycles": len(self.history),
            "bar_availability_ms": self._dist(
                [m["availability_ms"] for m in self.history]),
            "fetch_ms": self._dist([m["fetch_ms"] for m in self.history]),
            "cycle_ms": self._dist([m["cycle_ms"] for m in self.history]),
            "timeouts": sum(m["availability_ms"] is None for m in self.history),
            "errors": sum(m["error"] is not None for m in self.history),
            "last": self.history[-1] if self.history else None,
        }


class ConfirmedMinuteScheduler:
    """Requests one replay for each newly completed regular-session minute."""
    def __init__(self, request: Callable, *, release_delay_seconds: float = 0.05,
                 now_et: Callable = lambda: dt.datetime.now(ET),
                 tick_seconds: float = 0.02):
        self.request = request
        self.release_delay_seconds = float(release_delay_seconds)
        self.now_et = now_et
        self.tick_seconds = float(tick_seconds)
        self._stop = threading.Event()
        self._thread = None
        self._last_key = None
        self.requests = 0
        self.last_requested_close = None

    def start(self):
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="smc-minute-clock",
                                        daemon=True)
        self._thread.start()

    def _run(self):
        while not self._stop.wait(self.tick_seconds):
            now = self.now_et()
            if now.second + now.microsecond / 1e6 < self.release_delay_seconds:
                continue
            completed_open = now.replace(second=0, microsecond=0) - dt.timedelta(minutes=1)
            try:
                schedule = smc_calendar.session_schedule(now.date())
                market_minute = (getattr(schedule, "is_trading_day", False)
                                 and schedule.open_et <= completed_open < schedule.close_et)
            except Exception:
                market_minute = False
            key = completed_open.isoformat()
            if market_minute and key != self._last_key:
                self._last_key = key
                self.last_requested_close = (completed_open + dt.timedelta(minutes=1)).isoformat()
                self.requests += 1
                self.request(now.date().isoformat(), reason="confirmed_bar")

    def stop(self, timeout: float = 2.0):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        self._thread = None

    def health(self):
        return {"running": self._thread is not None and self._thread.is_alive(),
                "requests": self.requests,
                "last_requested_close": self.last_requested_close}
