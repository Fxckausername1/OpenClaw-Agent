"""Durable notification outbox anchored to the canonical event store.

The bounded in-memory queue alone was not sufficient: it dropped the oldest
message under pressure, which is acceptable for a health heartbeat and
unacceptable for a fill. This makes the DURABLE record the source of truth
and the in-memory queue merely a delivery hint.

Ordering guarantee, in one sentence: an event is committed to SQLite BEFORE
anything is queued, so a full queue, a wedged CLI, an open circuit breaker,
or a process restart can delay delivery but can never lose the event.

Anchored on `smc_events.id`, the canonical append-only trail the P0 repair
already established. That id is this table's PRIMARY KEY, which gives
deduplication by canonical event ID for free -- a retry, a restart, or two
callers racing on the same event cannot produce two Telegram messages,
because the second INSERT is an OR IGNORE against the same key.

Drop policy, per heff's requirement 9. Critical events are NEVER dropped or
coalesced under any pressure:
    order submitted / rejected / partial fill / fill /
    stop / target / exit submitted / exit filled / reconciliation mismatch
Informational health updates MAY be coalesced or abandoned when they pile
up, because a stale heartbeat has no value and a stale fill notice does.

Telegram failure still cannot delay trading: every method here is a local
SQLite write on the caller's own connection, and the delivery attempt itself
happens on the notify worker thread, never on the trading path.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import sqlite3
from typing import Optional

from smc import lifecycle_events as lc

logger = logging.getLogger("smc.notify_outbox")

STATE_PENDING = "pending"
STATE_DELIVERED = "delivered"
STATE_FAILED = "failed"
STATE_ABANDONED = "abandoned"

DEFAULT_MAX_ATTEMPTS = 8

# Priority: LOWER is more urgent. Drained in this order.
#
# Every trade-lifecycle kind shares PRIORITY_ORDERED, which is what makes
# delivery order equal commit order within the chain: with priority tied, the
# secondary sort is event_id. Splitting the chain across two priorities is
# precisely what used to deliver a fill ahead of the signal that caused it.
PRIORITY_ORDERED = 0
PRIORITY_CRITICAL = PRIORITY_ORDERED     # retained name; same rank
PRIORITY_LIFECYCLE = 1                   # incidents and other non-chain events
PRIORITY_INFO = 5

# Ordered, never coalesced: the whole chain (smc/lifecycle_events.py).
ORDERED_KINDS = lc.LIFECYCLE_KINDS
# Additionally retried forever and never abandoned: the money-touching subset.
CRITICAL_KINDS = lc.CRITICAL_KINDS
# Kinds that may be coalesced when they pile up.
COALESCIBLE_KINDS = frozenset({"health", "heartbeat", "stream_status", "cache_status"})

_DDL = """
CREATE TABLE IF NOT EXISTS smc_notifications (
    event_id        INTEGER PRIMARY KEY,
    created_ts      TEXT NOT NULL,
    kind            TEXT NOT NULL,
    priority        INTEGER NOT NULL,
    critical        INTEGER NOT NULL,
    message         TEXT NOT NULL,
    delivery_state  TEXT NOT NULL
        CHECK(delivery_state IN ('pending','delivered','failed','abandoned')),
    attempts        INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT,
    last_attempt_ts TEXT,
    delivered_ts    TEXT
);
CREATE INDEX IF NOT EXISTS idx_notif_drain
    ON smc_notifications(delivery_state, priority, event_id);
CREATE INDEX IF NOT EXISTS idx_notif_kind ON smc_notifications(kind);
"""


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Additive only: CREATE TABLE IF NOT EXISTS against the existing SMC
    state DB. Deliberately does NOT touch state.py's SCHEMA_VERSION or its
    required-tables integrity list -- adding a table must not make an older
    binary refuse to open its own database."""
    conn.executescript(_DDL)


def classify(kind: str) -> tuple:
    """(priority, critical) for an event kind.

    Note that ORDERED and CRITICAL are separate claims. Every chain event is
    ordered (priority 0); only the money-touching ones are also critical
    (retry forever, never abandon). See smc/lifecycle_events.py.
    """
    if kind in ORDERED_KINDS:
        return PRIORITY_ORDERED, (1 if kind in CRITICAL_KINDS else 0)
    if kind in CRITICAL_KINDS:
        return PRIORITY_ORDERED, 1
    if kind in COALESCIBLE_KINDS:
        return PRIORITY_INFO, 0
    return PRIORITY_LIFECYCLE, 0


def is_ordered(kind: str) -> bool:
    """True when this kind participates in strict commit-order delivery."""
    return kind in ORDERED_KINDS


class NotifyOutbox:
    """Durable delivery ledger. All writes are on the caller's connection so
    an event and its outbox row commit atomically together."""

    def __init__(self, conn: sqlite3.Connection, max_attempts: int = DEFAULT_MAX_ATTEMPTS):
        self.conn = conn
        self.max_attempts = max_attempts
        ensure_schema(conn)

    # ------------------------------------------------------------- writing
    def commit_event(self, kind: str, message: str, detail=None,
                     position_id: Optional[str] = None,
                     client_order_id: Optional[str] = None) -> Optional[int]:
        """Writes the canonical event AND its outbox row in ONE transaction,
        returning the canonical event id.

        This is the only correct entry point for anything that must be both
        audited and announced: it makes 'durably recorded' and 'queued for
        delivery' the same commit, so there is no window in which a message
        is queued for an event that was never recorded, or an event recorded
        with no delivery obligation."""
        priority, critical = classify(kind)
        try:
            with self.conn:
                cur = self.conn.execute(
                    "INSERT INTO smc_events(ts, position_id, client_order_id, kind, detail) "
                    "VALUES(?,?,?,?,?)",
                    (_now(), position_id, client_order_id, kind,
                     json.dumps(detail, default=str) if detail is not None else None),
                )
                event_id = cur.lastrowid
                self.conn.execute(
                    "INSERT OR IGNORE INTO smc_notifications"
                    "(event_id, created_ts, kind, priority, critical, message, "
                    " delivery_state, attempts) VALUES(?,?,?,?,?,?,?,0)",
                    (event_id, _now(), kind, priority, critical, message, STATE_PENDING),
                )
            return event_id
        except sqlite3.Error as e:
            # A notification-ledger failure must never break a risk action.
            logger.error("outbox commit failed for kind=%s (non-fatal): %s", kind, e)
            return None

    # ------------------------------------------------------------- draining
    def pending(self, limit: int = 50) -> list:
        """Next rows to attempt, most urgent first, then oldest first."""
        return self.conn.execute(
            "SELECT * FROM smc_notifications WHERE delivery_state IN (?,?) "
            "ORDER BY priority ASC, event_id ASC LIMIT ?",
            (STATE_PENDING, STATE_FAILED, limit),
        ).fetchall()

    def mark_delivered(self, event_id: int) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE smc_notifications SET delivery_state=?, delivered_ts=?, "
                "attempts=attempts+1, last_attempt_ts=?, last_error=NULL "
                "WHERE event_id=?",
                (STATE_DELIVERED, _now(), _now(), event_id))

    def mark_failed(self, event_id: int, error: str) -> str:
        """Records a failed attempt. Non-critical events are ABANDONED once
        max_attempts is exhausted; critical events stay retryable forever --
        an undelivered fill notice must remain visible as an outstanding
        obligation rather than quietly aging out."""
        row = self.conn.execute(
            "SELECT attempts, critical FROM smc_notifications WHERE event_id=?",
            (event_id,)).fetchone()
        if row is None:
            return STATE_FAILED
        attempts = int(row["attempts"]) + 1
        critical = int(row["critical"])
        new_state = STATE_FAILED
        if attempts >= self.max_attempts and not critical:
            new_state = STATE_ABANDONED
        with self.conn:
            self.conn.execute(
                "UPDATE smc_notifications SET delivery_state=?, attempts=?, "
                "last_error=?, last_attempt_ts=? WHERE event_id=?",
                (new_state, attempts, str(error)[:500], _now(), event_id))
        return new_state

    def coalesce_informational(self, keep_latest: int = 1) -> int:
        """Collapses backed-up informational rows, keeping only the newest
        few. Critical kinds are excluded by construction: the WHERE clause
        filters on critical=0, so no amount of pressure can collapse a fill."""
        rows = self.conn.execute(
            "SELECT event_id FROM smc_notifications "
            "WHERE critical=0 AND kind IN (%s) AND delivery_state IN (?,?) "
            "ORDER BY event_id DESC" % ",".join("?" * len(COALESCIBLE_KINDS)),
            (*sorted(COALESCIBLE_KINDS), STATE_PENDING, STATE_FAILED),
        ).fetchall()
        stale = [r["event_id"] for r in rows[keep_latest:]]
        if not stale:
            return 0
        with self.conn:
            self.conn.executemany(
                "UPDATE smc_notifications SET delivery_state=?, "
                "last_error='coalesced: superseded by a newer health update' "
                "WHERE event_id=?",
                [(STATE_ABANDONED, eid) for eid in stale])
        return len(stale)

    # -------------------------------------------------------------- status
    def get(self, event_id: int):
        return self.conn.execute(
            "SELECT * FROM smc_notifications WHERE event_id=?", (event_id,)).fetchone()

    def counts(self) -> dict:
        out = {STATE_PENDING: 0, STATE_DELIVERED: 0, STATE_FAILED: 0, STATE_ABANDONED: 0}
        for row in self.conn.execute(
                "SELECT delivery_state, COUNT(*) c FROM smc_notifications "
                "GROUP BY delivery_state"):
            out[row["delivery_state"]] = row["c"]
        return out

    def recent_lifecycle(self, limit: int = 40) -> list:
        """Newest-first view of the trade chain for the dashboard, with each
        event's stage number and DELIVERY state.

        Delivery state is the point. A dashboard that showed only that an
        event happened would look identical whether or not anyone was ever
        told about it -- which is the exact condition this work exists to
        make visible.
        """
        rows = self.conn.execute(
            "SELECT event_id, created_ts, kind, critical, delivery_state, "
            "       attempts, last_error, delivered_ts "
            "FROM smc_notifications WHERE kind IN (%s) "
            "ORDER BY event_id DESC LIMIT ?" % ",".join("?" * len(ORDERED_KINDS)),
            (*sorted(ORDERED_KINDS), limit)).fetchall()
        return [{
            "event_id": r["event_id"],
            "stage": lc.stage_of(r["kind"]),
            "kind": r["kind"],
            "critical": bool(r["critical"]),
            "created_ts": r["created_ts"],
            "delivery_state": r["delivery_state"],
            "attempts": r["attempts"],
            "delivered_ts": r["delivered_ts"],
            "last_error": r["last_error"],
        } for r in rows]

    def oldest_undelivered_critical_age_seconds(self, now=None) -> Optional[float]:
        """Age of the OLDEST outstanding critical obligation, or None when
        there is none. This is the number the readiness gate acts on: a
        backlog that is merely deep may just be a burst, but a backlog that
        is OLD means delivery is not working."""
        row = self.conn.execute(
            "SELECT MIN(created_ts) AS oldest FROM smc_notifications "
            "WHERE critical=1 AND delivery_state IN (?,?)",
            (STATE_PENDING, STATE_FAILED)).fetchone()
        if row is None or not row["oldest"]:
            return None
        try:
            created = dt.datetime.fromisoformat(row["oldest"])
        except (TypeError, ValueError):
            return None
        if created.tzinfo is None:
            created = created.replace(tzinfo=dt.timezone.utc)
        now = now or dt.datetime.now(dt.timezone.utc)
        return max((now - created).total_seconds(), 0.0)

    def undelivered_critical(self) -> list:
        """Outstanding critical obligations -- surfaced on the dashboard so an
        undelivered fill notice is visible rather than merely absent."""
        return self.conn.execute(
            "SELECT * FROM smc_notifications WHERE critical=1 "
            "AND delivery_state IN (?,?) ORDER BY event_id ASC",
            (STATE_PENDING, STATE_FAILED)).fetchall()
