"""Real market calendar for SMC exit scheduling -- replaces the hard-coded
`dt.time(15, 30)` forced-close assumption.

Why this matters and isn't pedantry: the old exit manager treated 15:30 ET as
"the" forced-close time on every session. On a US equity-options early close
(1:00pm ET -- day after Thanksgiving, Christmas Eve, July 3rd when observed) a
15:30 forced close is 2.5 hours AFTER the market has already shut. A 0DTE long
option left open through an early close cannot be exited at all and expires
against whatever the settlement is -- an unbounded, silent loss path that the
15:30 constant would never have triggered on.

Source of truth is Alpaca's own `/v2/calendar` (it publishes real per-session
open/close times, including early closes), cached to disk so an API blip can't
leave the supervisor with no schedule. If BOTH the API and the cache are
unavailable, `session_close` FAILS CLOSED: it returns the conservative regular-
session close and flags `degraded=True`, and callers treat a degraded schedule
as a reason to flatten early rather than to assume a full session.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import logging
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
ROOT = Path(__file__).resolve().parent.parent
CACHE_PATH = ROOT / "data" / "live_heff_smc" / "market_calendar_cache.json"

REGULAR_CLOSE = dt.time(16, 0)
REGULAR_OPEN = dt.time(9, 30)
EARLY_CLOSE = dt.time(13, 0)

logger = logging.getLogger("smc.calendar")


@dataclasses.dataclass(frozen=True)
class SessionSchedule:
    session_date: dt.date
    is_trading_day: bool
    open_et: Optional[dt.datetime]
    close_et: Optional[dt.datetime]
    is_early_close: bool
    degraded: bool          # True = schedule could not be confirmed from a real source
    source: str

    def forced_close_at(self, buffer_minutes: int) -> Optional[dt.datetime]:
        """When the supervisor must have flattened by. `buffer_minutes` before
        the REAL close for this specific session."""
        if self.close_et is None:
            return None
        return self.close_et - dt.timedelta(minutes=buffer_minutes)


def _parse_cache() -> dict:
    if not CACHE_PATH.exists():
        return {}
    try:
        payload = json.loads(CACHE_PATH.read_text())
        return payload.get("sessions", {})
    except Exception as e:
        logger.warning("calendar cache unreadable (%s) -- treating as empty", e)
        return {}


def _write_cache(sessions: dict) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CACHE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps({"sessions": sessions}, indent=2, default=str))
    tmp.replace(CACHE_PATH)


def refresh_calendar(fetch_fn, start: dt.date, end: dt.date) -> dict:
    """`fetch_fn(start, end)` returns Alpaca-shaped rows:
    [{'date': 'YYYY-MM-DD', 'open': 'HH:MM', 'close': 'HH:MM'}, ...].
    Injected rather than imported so tests never touch the network."""
    rows = fetch_fn(start, end)
    sessions = _parse_cache()
    for row in rows or []:
        sessions[row["date"]] = {"open": row.get("open"), "close": row.get("close")}
    _write_cache(sessions)
    return sessions


def _combine(session_date: dt.date, hhmm: str) -> dt.datetime:
    hour, minute = (int(x) for x in hhmm.split(":")[:2])
    return dt.datetime.combine(session_date, dt.time(hour, minute), tzinfo=ET)


def session_schedule(session_date: dt.date, sessions: Optional[dict] = None) -> SessionSchedule:
    """Resolve one session. Never raises -- an unresolvable schedule comes back
    `degraded=True` so the caller can choose the safe behaviour explicitly."""
    sessions = _parse_cache() if sessions is None else sessions
    key = session_date.isoformat()
    row = sessions.get(key)

    if row and row.get("close"):
        close_et = _combine(session_date, row["close"])
        open_et = _combine(session_date, row.get("open") or "09:30")
        return SessionSchedule(
            session_date=session_date, is_trading_day=True, open_et=open_et, close_et=close_et,
            is_early_close=close_et.time() < REGULAR_CLOSE, degraded=False, source="alpaca_calendar",
        )

    if key in sessions and not (row or {}).get("close"):
        # Present in the calendar but with no close = explicitly a non-trading day.
        return SessionSchedule(session_date, False, None, None, False, False, "alpaca_calendar_holiday")

    if session_date.weekday() >= 5:
        return SessionSchedule(session_date, False, None, None, False, False, "weekend")

    # Unknown weekday: assume a trading day but mark DEGRADED. Callers must treat
    # degraded as "flatten early", never as "full session confirmed".
    return SessionSchedule(
        session_date=session_date, is_trading_day=True,
        open_et=dt.datetime.combine(session_date, REGULAR_OPEN, tzinfo=ET),
        close_et=dt.datetime.combine(session_date, REGULAR_CLOSE, tzinfo=ET),
        is_early_close=False, degraded=True, source="assumed_regular_session",
    )


def must_flatten(now_et: dt.datetime, schedule: SessionSchedule, config) -> tuple:
    """(should_flatten, reason). Independent of any per-position exit rule: this
    is the EOD/early-close watchdog, and it fires on schedule alone."""
    if not schedule.is_trading_day:
        return True, "not a trading session -- no venue to manage risk on"
    if schedule.close_et is None:
        return True, "session close unknown -- failing closed"

    buffer_min = config.forced_close_buffer_minutes
    if schedule.is_early_close:
        buffer_min = max(buffer_min, config.early_close_flatten_buffer_minutes)
    if schedule.degraded:
        # Unconfirmed schedule: flatten a full extra buffer earlier rather than
        # risk discovering the session already closed.
        buffer_min = buffer_min + config.early_close_flatten_buffer_minutes

    deadline = schedule.close_et - dt.timedelta(minutes=buffer_min)
    if now_et >= deadline:
        label = "early-close" if schedule.is_early_close else "regular-close"
        degraded_note = " (DEGRADED schedule, extra margin applied)" if schedule.degraded else ""
        return True, (
            f"{label} flatten deadline reached: now={now_et:%H:%M} "
            f"deadline={deadline:%H:%M} close={schedule.close_et:%H:%M}{degraded_note}"
        )
    return False, ""
