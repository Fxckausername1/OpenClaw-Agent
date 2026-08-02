"""Durable, session-scoped entry-submission budget.

WHY. `Daemon._entry_submission_count` was process memory. Under
`Restart=always` a crash, an OOM kill, a reconnect loop or a reboot restores
the allowance, so a "one entry canary" could become two, three, or one per
restart. The counter was already incremented BEFORE network I/O -- the right
instinct -- but an in-memory latch cannot survive the very event it is meant
to protect against.

CONTRACT.
  * The budget is keyed on the ET TRADING SESSION DATE, not on process
    lifetime. It resets when a genuinely new session begins and at no other
    time.
  * Consumption is committed to SQLite BEFORE the POST. A request whose fate
    is unknown (transport timeout) has therefore already spent the
    allowance, because an unknown outcome may well be a live order.
  * `consume()` is a single atomic UPDATE guarded by `used < max_attempts`,
    so two racing callers cannot both win. The row is the lock.
  * Reconciliation can record which client_order_ids were spent, so broker
    truth can be checked before anything is permitted again.

The table is additive (`CREATE TABLE IF NOT EXISTS`) and deliberately does
NOT touch state.py's SCHEMA_VERSION or its required-tables integrity list --
adding a table must never make an older binary refuse to open its own DB.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import json
import logging
import sqlite3
from typing import Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger("smc.entry_budget")

ET = ZoneInfo("America/New_York")

_DDL = """
CREATE TABLE IF NOT EXISTS smc_entry_budget (
    session_date     TEXT PRIMARY KEY,
    max_attempts     INTEGER NOT NULL,
    used             INTEGER NOT NULL DEFAULT 0,
    first_used_ts    TEXT,
    last_used_ts     TEXT,
    client_order_ids TEXT NOT NULL DEFAULT '[]'
);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_DDL)


def session_date(now: Optional[dt.datetime] = None) -> str:
    """The ET calendar date of the trading session.

    Deliberately ET, not UTC: the box clock is UTC, and a UTC date would roll
    over at 20:00 ET mid-session, handing back a fresh allowance during the
    trading day."""
    now = now or dt.datetime.now(ET)
    if now.tzinfo is None:
        now = now.replace(tzinfo=ET)
    return now.astimezone(ET).date().isoformat()


@dataclasses.dataclass(frozen=True)
class BudgetState:
    session_date: str
    max_attempts: int
    used: int
    first_used_ts: Optional[str]
    last_used_ts: Optional[str]
    client_order_ids: tuple

    @property
    def remaining(self) -> int:
        return max(self.max_attempts - self.used, 0)

    @property
    def exhausted(self) -> bool:
        return self.used >= self.max_attempts

    def as_dict(self) -> dict:
        d = dataclasses.asdict(self)
        d["client_order_ids"] = list(self.client_order_ids)
        d["remaining"] = self.remaining
        d["exhausted"] = self.exhausted
        return d


class EntryBudget:
    """One row per session date. The row IS the latch."""

    def __init__(self, conn: sqlite3.Connection, max_attempts: int = 1):
        self.conn = conn
        self.max_attempts = int(max_attempts)
        ensure_schema(conn)

    # ------------------------------------------------------------- reading
    def state(self, day: Optional[str] = None) -> BudgetState:
        day = day or session_date()
        row = self.conn.execute(
            "SELECT * FROM smc_entry_budget WHERE session_date=?", (day,)).fetchone()
        if row is None:
            return BudgetState(day, self.max_attempts, 0, None, None, ())
        try:
            ids = tuple(json.loads(row["client_order_ids"] or "[]"))
        except (ValueError, TypeError):
            ids = ()
        return BudgetState(row["session_date"], int(row["max_attempts"]),
                           int(row["used"]), row["first_used_ts"],
                           row["last_used_ts"], ids)

    def can_submit(self, day: Optional[str] = None) -> bool:
        return not self.state(day).exhausted

    # ------------------------------------------------------------ writing
    def consume(self, client_order_id: str, day: Optional[str] = None) -> bool:
        """Atomically spend one attempt. Returns False if the session
        allowance is already gone.

        MUST be called before the POST. The commit happens inside this call,
        so a process killed immediately afterwards still finds the allowance
        spent on restart.

        Idempotent per client_order_id: re-consuming the SAME id (e.g. a
        retry of a submit whose response was lost) does not double-spend,
        because the deterministic client_order_id identifies the same
        intended order."""
        day = day or session_date()
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        with self.conn:
            self.conn.execute(
                "INSERT OR IGNORE INTO smc_entry_budget"
                "(session_date, max_attempts, used, client_order_ids) "
                "VALUES(?,?,0,'[]')", (day, self.max_attempts))
            row = self.conn.execute(
                "SELECT used, max_attempts, client_order_ids FROM smc_entry_budget "
                "WHERE session_date=?", (day,)).fetchone()
            try:
                ids = json.loads(row["client_order_ids"] or "[]")
            except (ValueError, TypeError):
                ids = []
            if client_order_id and client_order_id in ids:
                logger.warning("entry budget: %s already consumed this session; "
                               "not double-spending", client_order_id)
                return True
            if int(row["used"]) >= int(row["max_attempts"]):
                logger.error("entry budget EXHAUSTED for %s (%s/%s) -- refusing",
                             day, row["used"], row["max_attempts"])
                return False
            ids.append(client_order_id)
            cur = self.conn.execute(
                "UPDATE smc_entry_budget SET used = used + 1, "
                "first_used_ts = COALESCE(first_used_ts, ?), last_used_ts = ?, "
                "client_order_ids = ? "
                "WHERE session_date = ? AND used < max_attempts",
                (now, now, json.dumps(ids), day))
            if cur.rowcount != 1:
                # Lost the race to a concurrent consumer.
                logger.error("entry budget: lost the atomic UPDATE race for %s", day)
                return False
        logger.info("entry budget consumed for %s: %s", day, client_order_id)
        return True

    def set_max(self, max_attempts: int, day: Optional[str] = None) -> None:
        """Adjust the ceiling for a session. Never lowers `used`."""
        day = day or session_date()
        self.max_attempts = int(max_attempts)
        with self.conn:
            self.conn.execute(
                "INSERT INTO smc_entry_budget(session_date, max_attempts, used) "
                "VALUES(?,?,0) ON CONFLICT(session_date) DO UPDATE SET max_attempts=?",
                (day, int(max_attempts), int(max_attempts)))

    def history(self, limit: int = 30) -> list:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM smc_entry_budget ORDER BY session_date DESC LIMIT ?",
            (limit,))]
