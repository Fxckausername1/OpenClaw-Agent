"""Durable SQLite control plane for SMC execution -- replaces
`data/live_heff_smc/open_positions.json` as the authority on what the strategy
believes it owns.

WHY the JSON file had to go (real incident, 2026-07-31): it was a dict keyed by
OCC symbol. Three triangle signals selected the SAME contract
(QQQ260731C00690000) inside 30 minutes, and each new entry's
`positions[occ] = {...}` silently OVERWROTE the previous record. A real, filled
paper position vanished from the control plane while remaining open at the
broker, so the exit manager never saw it again; it sat unmanaged at -57.6% until
a human found it. A plain dict write cannot express "this already exists" --
a UNIQUE constraint can, and does, here.

Design rules this module enforces structurally, not by convention:

1. INTENT BEFORE SUBMIT. A row exists, committed, with a deterministic
   client_order_id BEFORE any HTTP call reaches the broker. If the process dies
   between commit and submit, restart reconciliation finds the intent, asks the
   broker "did you ever see this client_order_id?", and recovers. The old code
   submitted first and recorded second, so a crash in that window created a
   broker position with NO local record -- unrecoverable by construction.
2. DETERMINISTIC client_order_id derived from the signal key. Same signal always
   maps to the same id, so a POST that times out can be resolved by asking the
   broker about the id instead of guessing, and a retry can never double-submit.
3. UNIQUE(signal_key) on positions -- duplicate signals are rejected by the
   database, not by application bookkeeping that a restart could lose.
4. UNIQUE(client_order_id) and UNIQUE(broker_order_id) on orders.
5. BROKER IS AUTHORITATIVE for quantity. Local `filled_qty` is a cache that
   reconciliation overwrites; exits size off reconciled quantity, never off a
   hard-coded 1.
6. CORRUPTION IS LOUD AND HALTING. Every read path raises `SmcStateError` rather
   than returning an empty list. "No rows" and "database unreadable" must never
   be indistinguishable -- treating the latter as the former is precisely how a
   live position becomes invisible.
7. The dashboard ledger (`options_eval.db`) is REPORTING ONLY. `dashboard_synced`
   is a nullable flag here; a dashboard write failure can never block an exit.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import json
import logging
import sqlite3
from pathlib import Path
from typing import Iterable, Optional

logger = logging.getLogger("smc.state")

SCHEMA_VERSION = 1

# ------------------------------------------------------------------ lifecycle
INTENT = "INTENT"                    # committed locally, not yet sent to broker
SUBMITTED = "SUBMITTED"              # broker acknowledged receipt
PARTIAL = "PARTIAL"                  # partially filled
OPEN = "OPEN"                        # fully filled, position live
EXIT_SUBMITTED = "EXIT_SUBMITTED"    # protective/target exit working
PENDING_CANCEL = "PENDING_CANCEL"    # cancel requested, terminal state unconfirmed
CLOSED = "CLOSED"                    # flat, P&L realised
CANCELED = "CANCELED"                # never filled, terminal
REJECTED = "REJECTED"                # broker refused, terminal
RECON_MISMATCH = "RECON_MISMATCH"    # local vs broker disagree -- halt entries, keep supervising

POSITION_STATES = (
    INTENT, SUBMITTED, PARTIAL, OPEN, EXIT_SUBMITTED, PENDING_CANCEL,
    CLOSED, CANCELED, REJECTED, RECON_MISMATCH,
)
ORDER_STATES = POSITION_STATES

# States where the strategy may still own broker exposure and MUST keep
# supervising. RECON_MISMATCH is deliberately included: an ambiguous position is
# the one you most need to keep watching, not the one you get to forget.
LIVE_POSITION_STATES = (SUBMITTED, PARTIAL, OPEN, EXIT_SUBMITTED, PENDING_CANCEL, RECON_MISMATCH)
# Filled positions that may still carry broker exposure and therefore must remain
# in the protective supervisor. Keep this separate from LIVE_POSITION_STATES:
# SUBMITTED is an unresolved entry order, while the states below are positions
# whose broker quantity must be checked/managed. In particular,
# RECON_MISMATCH must never disappear from supervision merely because ownership
# or quantity is uncertain.
SUPERVISED_POSITION_STATES = (PARTIAL, OPEN, EXIT_SUBMITTED, PENDING_CANCEL, RECON_MISMATCH)
TERMINAL_STATES = (CLOSED, CANCELED, REJECTED)

ROLE_ENTRY = "ENTRY"
ROLE_EXIT = "EXIT"


class SmcStateError(RuntimeError):
    """Raised on any state-integrity failure. Callers must treat this as
    'halt new entries and alert loudly', NEVER as 'there are no positions'."""


class DuplicateSignal(SmcStateError):
    """The signal already has a position row. Not an error condition in the
    operational sense -- it is the dedup guarantee working."""


_DDL = f"""
CREATE TABLE IF NOT EXISTS smc_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS smc_positions (
    position_id            TEXT PRIMARY KEY,
    signal_key             TEXT NOT NULL UNIQUE,
    occ                    TEXT NOT NULL,
    underlying             TEXT NOT NULL,
    contract_right         TEXT NOT NULL,
    signal_side            TEXT NOT NULL,
    state                  TEXT NOT NULL CHECK(state IN {POSITION_STATES!r}),
    intended_qty           INTEGER NOT NULL CHECK(intended_qty > 0),
    filled_qty             INTEGER NOT NULL DEFAULT 0 CHECK(filled_qty >= 0),
    closed_qty             INTEGER NOT NULL DEFAULT 0 CHECK(closed_qty >= 0),
    entry_client_order_id  TEXT,
    entry_broker_order_id  TEXT,
    entry_fill_price       REAL,
    exit_fill_price        REAL,
    realized_pnl           REAL,
    exit_reason            TEXT,
    entry_limit_price      REAL,
    signal_ts              TEXT,
    detected_ts            TEXT,
    selected_ts            TEXT,
    intent_ts              TEXT NOT NULL,
    submitted_ts           TEXT,
    acked_ts               TEXT,
    entry_filled_ts        TEXT,
    closed_ts              TEXT,
    last_recon_ts          TEXT,
    recon_note             TEXT,
    dashboard_synced       INTEGER NOT NULL DEFAULT 0,
    trigger_kind           TEXT,
    trigger_score          REAL
);

CREATE TABLE IF NOT EXISTS smc_orders (
    client_order_id  TEXT PRIMARY KEY,
    position_id      TEXT NOT NULL REFERENCES smc_positions(position_id),
    role             TEXT NOT NULL CHECK(role IN ('ENTRY','EXIT')),
    attempt          INTEGER NOT NULL,
    occ              TEXT NOT NULL,
    side             TEXT NOT NULL,
    order_type       TEXT NOT NULL,
    limit_price      REAL,
    intended_qty     INTEGER NOT NULL CHECK(intended_qty > 0),
    filled_qty       INTEGER NOT NULL DEFAULT 0,
    avg_fill_price   REAL,
    broker_order_id  TEXT UNIQUE,
    state            TEXT NOT NULL CHECK(state IN {ORDER_STATES!r}),
    exit_reason      TEXT,
    intent_ts        TEXT NOT NULL,
    submitted_ts     TEXT,
    acked_ts         TEXT,
    terminal_ts      TEXT,
    note             TEXT,
    UNIQUE(position_id, role, attempt)
);

CREATE TABLE IF NOT EXISTS smc_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT NOT NULL,
    position_id     TEXT,
    client_order_id TEXT,
    kind            TEXT NOT NULL,
    detail          TEXT
);

CREATE TABLE IF NOT EXISTS smc_halts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    reason      TEXT NOT NULL,
    cleared_ts  TEXT
);

CREATE INDEX IF NOT EXISTS idx_positions_state ON smc_positions(state);
CREATE INDEX IF NOT EXISTS idx_positions_occ ON smc_positions(occ);
CREATE INDEX IF NOT EXISTS idx_orders_position ON smc_orders(position_id);
CREATE INDEX IF NOT EXISTS idx_orders_state ON smc_orders(state);
"""


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def derive_position_id(signal_key: str) -> str:
    """Deterministic and stable: the same signal always yields the same id, so a
    retry after a timeout or a restart addresses the SAME logical trade instead
    of creating a second one."""
    digest = hashlib.sha256(signal_key.encode("utf-8")).hexdigest()[:12]
    return f"smc-{digest}"


def derive_client_order_id(signal_key: str, role: str, attempt: int) -> str:
    """Deterministic per (signal, role, attempt). Alpaca allows arbitrary
    client_order_id strings and lets us GET an order BY it, which is the whole
    reason this is derived rather than random: after a POST timeout we can ask
    "do you have this id?" instead of guessing whether the order exists.
    Attempt is included so an exit ladder can submit successive orders without
    colliding, while still never reusing an id."""
    if role not in (ROLE_ENTRY, ROLE_EXIT):
        raise ValueError(f"bad role {role!r}")
    suffix = "e" if role == ROLE_ENTRY else "x"
    return f"{derive_position_id(signal_key)}-{suffix}{attempt}"


class SmcStateStore:
    """Thin, explicit wrapper. Every mutation is its own IMMEDIATE transaction --
    no implicit autocommit surprises, and no long-held write locks that could
    block the protective supervisor."""

    def __init__(self, db_path: Path, verify_integrity: bool = True):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.conn = sqlite3.connect(str(self.db_path), timeout=20, isolation_level=None)
            self.conn.row_factory = sqlite3.Row
            for pragma in ("journal_mode=WAL", "synchronous=FULL", "busy_timeout=15000",
                           "foreign_keys=ON"):
                self.conn.execute(f"PRAGMA {pragma};")
        except sqlite3.Error as e:
            raise SmcStateError(
                f"CANNOT OPEN SMC STATE DB at {self.db_path}: {e}. Halting new entries. "
                "This is NOT 'no positions' -- local state is unavailable and any real "
                "broker position is currently unsupervised."
            ) from e

        # synchronous=FULL, not NORMAL: this database is the only thing standing
        # between a broker fill and an unrecoverable orphan. Durability of the
        # pre-submit INTENT commit is the entire safety property, so we pay the
        # fsync cost -- these are single-digit writes per trade, not a hot path.
        self._init_schema()
        if verify_integrity:
            self.verify_integrity()

    # ------------------------------------------------------------- lifecycle
    def _init_schema(self) -> None:
        try:
            self.conn.executescript(_DDL)
            self.conn.execute(
                "INSERT OR IGNORE INTO smc_meta(key, value) VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
        except sqlite3.Error as e:
            raise SmcStateError(f"SMC state schema init/migration failed: {e}") from e

    def verify_integrity(self) -> None:
        """Loud, halting corruption check. Runs on every open (i.e. every cron
        tick and every supervisor start), because a corrupted control plane that
        silently reads as empty is the worst possible failure here."""
        try:
            row = self.conn.execute("PRAGMA integrity_check;").fetchone()
            result = row[0] if row else "no result"
            if str(result).lower() != "ok":
                raise SmcStateError(
                    f"SMC STATE DB INTEGRITY CHECK FAILED: {result!r}. Halting new entries. "
                    "Existing broker positions may be unsupervised -- reconcile against the "
                    "broker manually before resuming."
                )
            version = self.conn.execute(
                "SELECT value FROM smc_meta WHERE key='schema_version'"
            ).fetchone()
            if version is None:
                raise SmcStateError("SMC state DB missing schema_version -- refusing to trust it")
            if int(version[0]) > SCHEMA_VERSION:
                raise SmcStateError(
                    f"SMC state DB schema_version {version[0]} is NEWER than this code "
                    f"({SCHEMA_VERSION}) -- refusing to operate on state written by a future "
                    "version, which could mean silently misreading fields."
                )
            # Required tables must be queryable, not merely present in sqlite_master.
            for table in ("smc_positions", "smc_orders", "smc_events", "smc_halts"):
                self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
        except sqlite3.DatabaseError as e:
            raise SmcStateError(
                f"SMC state DB unreadable/corrupt ({e}). Halting new entries. Treating this as "
                "'no positions' is forbidden -- a real position may be open and unsupervised."
            ) from e

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    # ------------------------------------------------------------- utilities
    def log_event(self, kind: str, detail=None, position_id=None, client_order_id=None) -> None:
        """Append-only audit trail. Best-effort: an audit write must never break
        a risk action, so this swallows errors after logging them."""
        try:
            self.conn.execute(
                "INSERT INTO smc_events(ts, position_id, client_order_id, kind, detail) "
                "VALUES(?,?,?,?,?)",
                (_now(), position_id, client_order_id, kind,
                 json.dumps(detail, default=str) if detail is not None else None),
            )
        except sqlite3.Error as e:
            logger.error("event log write failed (non-fatal): %s", e)

    # ----------------------------------------------------------------- halts
    def set_halt(self, reason: str) -> None:
        try:
            active = self.active_halt()
            if active and active["reason"] == reason:
                return
            self.conn.execute("BEGIN IMMEDIATE")
            self.conn.execute("INSERT INTO smc_halts(ts, reason) VALUES(?,?)", (_now(), reason))
            self.conn.execute("COMMIT")
        except sqlite3.Error as e:
            raise SmcStateError(f"could not record halt {reason!r}: {e}") from e
        logger.error("SMC ENTRY HALT SET: %s", reason)

    def active_halt(self) -> Optional[sqlite3.Row]:
        try:
            return self.conn.execute(
                "SELECT * FROM smc_halts WHERE cleared_ts IS NULL ORDER BY id DESC LIMIT 1"
            ).fetchone()
        except sqlite3.Error as e:
            raise SmcStateError(f"halt table unreadable: {e}") from e

    def clear_halts(self, note: str = "") -> int:
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            cur = self.conn.execute(
                "UPDATE smc_halts SET cleared_ts=? WHERE cleared_ts IS NULL", (_now(),)
            )
            self.conn.execute("COMMIT")
            self.log_event("HALTS_CLEARED", {"note": note, "count": cur.rowcount})
            return cur.rowcount
        except sqlite3.Error as e:
            raise SmcStateError(f"could not clear halts: {e}") from e

    # ------------------------------------------------------- intent creation
    def create_entry_intent(
        self, *, signal_key: str, occ: str, underlying: str, contract_right: str,
        signal_side: str, intended_qty: int, limit_price: Optional[float],
        order_type: str, signal_ts=None, detected_ts=None, selected_ts=None,
        trigger_kind=None, trigger_score=None,
    ) -> dict:
        """Creates position + entry-order rows in ONE committed transaction,
        BEFORE anything is sent to the broker. Returns the identifiers the caller
        must use when submitting.

        Raises DuplicateSignal if this signal already has a position -- that is
        the database refusing a double-submit, which is exactly the protection
        the old occ-keyed dict could not provide.
        """
        if intended_qty <= 0:
            raise ValueError("intended_qty must be positive")
        position_id = derive_position_id(signal_key)
        coid = derive_client_order_id(signal_key, ROLE_ENTRY, 0)
        now = _now()
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            self.conn.execute(
                "INSERT INTO smc_positions("
                "position_id, signal_key, occ, underlying, contract_right, signal_side, state,"
                "intended_qty, entry_client_order_id, entry_limit_price, signal_ts, detected_ts,"
                "selected_ts, intent_ts, trigger_kind, trigger_score) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (position_id, signal_key, occ, underlying, contract_right, signal_side, INTENT,
                 intended_qty, coid, limit_price, signal_ts, detected_ts, selected_ts, now,
                 trigger_kind, trigger_score),
            )
            self.conn.execute(
                "INSERT INTO smc_orders("
                "client_order_id, position_id, role, attempt, occ, side, order_type, limit_price,"
                "intended_qty, state, intent_ts) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (coid, position_id, ROLE_ENTRY, 0, occ, "buy", order_type, limit_price,
                 intended_qty, INTENT, now),
            )
            self.conn.execute("COMMIT")
        except sqlite3.IntegrityError as e:
            with contextlib.suppress(Exception):
                self.conn.execute("ROLLBACK")
            raise DuplicateSignal(
                f"signal {signal_key!r} already has an SMC position ({position_id}): {e}"
            ) from e
        except sqlite3.Error as e:
            with contextlib.suppress(Exception):
                self.conn.execute("ROLLBACK")
            raise SmcStateError(f"could not persist entry intent for {signal_key!r}: {e}") from e

        self.log_event("ENTRY_INTENT", {"occ": occ, "qty": intended_qty,
                                        "limit": limit_price, "type": order_type},
                       position_id=position_id, client_order_id=coid)
        return {"position_id": position_id, "client_order_id": coid, "intent_ts": now}

    def create_exit_intent(
        self, *, position_id: str, occ: str, intended_qty: int, order_type: str,
        limit_price: Optional[float], exit_reason: str,
    ) -> dict:
        """Next exit attempt for a position. `attempt` auto-increments so an
        urgent ladder can escalate without ever reusing a client_order_id."""
        if intended_qty <= 0:
            raise ValueError("exit intended_qty must be positive")
        pos = self.get_position(position_id)
        if pos is None:
            raise SmcStateError(f"cannot create exit intent: unknown position {position_id!r}")
        try:
            row = self.conn.execute(
                "SELECT COALESCE(MAX(attempt), -1) AS a FROM smc_orders "
                "WHERE position_id=? AND role=?", (position_id, ROLE_EXIT),
            ).fetchone()
            attempt = int(row["a"]) + 1
            coid = derive_client_order_id(pos["signal_key"], ROLE_EXIT, attempt)
            now = _now()
            self.conn.execute("BEGIN IMMEDIATE")
            self.conn.execute(
                "INSERT INTO smc_orders("
                "client_order_id, position_id, role, attempt, occ, side, order_type, limit_price,"
                "intended_qty, state, exit_reason, intent_ts) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (coid, position_id, ROLE_EXIT, attempt, occ, "sell", order_type, limit_price,
                 intended_qty, INTENT, exit_reason, now),
            )
            self.conn.execute("COMMIT")
        except sqlite3.Error as e:
            with contextlib.suppress(Exception):
                self.conn.execute("ROLLBACK")
            raise SmcStateError(f"could not persist exit intent for {position_id!r}: {e}") from e

        self.log_event("EXIT_INTENT", {"occ": occ, "qty": intended_qty, "type": order_type,
                                        "limit": limit_price, "reason": exit_reason, "attempt": attempt},
                       position_id=position_id, client_order_id=coid)
        return {"client_order_id": coid, "attempt": attempt, "intent_ts": now}

    # ------------------------------------------------------- order mutations
    def mark_order_submitted(self, client_order_id: str) -> None:
        """Called immediately BEFORE the HTTP POST. Distinguishes "we never tried"
        from "we tried and don't know the outcome" -- the latter must be resolved
        against the broker by client_order_id, never assumed failed.

        The POSITION row is advanced too, not just the order row: that INTENT ->
        SUBMITTED transition IS the record that a submission was attempted, and a
        reviewer (or a recovery path) reading only the position row would otherwise
        see INTENT and wrongly conclude nothing was ever sent to the venue. Caught
        by test_submit_timeout_with_unresolvable_lookup_stays_submitted."""
        now = _now()
        self._update_order(client_order_id, state=SUBMITTED, submitted_ts=now)
        row = self.get_order(client_order_id)
        if row is not None and row["role"] == ROLE_ENTRY:
            self._update_position(row["position_id"], state=SUBMITTED, submitted_ts=now)
        self.log_event("ORDER_SUBMIT_ATTEMPT", client_order_id=client_order_id,
                       position_id=row["position_id"] if row is not None else None)

    def record_broker_ack(self, client_order_id: str, broker_order_id: str,
                          state: str = SUBMITTED) -> None:
        """Broker acknowledgement is persisted HERE, before any dashboard write or
        notification. The 2026-07-30 executor did dashboard + Telegram work in the
        same block as its position bookkeeping, so a failure in reporting could
        interleave with control-plane truth."""
        self._update_order(client_order_id, state=state, broker_order_id=broker_order_id,
                           acked_ts=_now())
        row = self.get_order(client_order_id)
        if row is not None and row["role"] == ROLE_ENTRY:
            self._update_position(row["position_id"], entry_broker_order_id=broker_order_id,
                                   acked_ts=_now(), submitted_ts=row["submitted_ts"],
                                   state=state if state in POSITION_STATES else SUBMITTED)
        self.log_event("BROKER_ACK", {"broker_order_id": broker_order_id, "state": state},
                       client_order_id=client_order_id,
                       position_id=row["position_id"] if row is not None else None)

    def record_order_fill(self, client_order_id: str, filled_qty: int,
                          avg_fill_price: Optional[float], state: str) -> None:
        self._update_order(client_order_id, state=state, filled_qty=filled_qty,
                           avg_fill_price=avg_fill_price,
                           terminal_ts=_now() if state in TERMINAL_STATES or state == OPEN else None)
        self.log_event("ORDER_FILL", {"filled_qty": filled_qty, "avg_fill_price": avg_fill_price,
                                       "state": state}, client_order_id=client_order_id)

    def record_order_terminal(self, client_order_id: str, state: str, note: str = "") -> None:
        if state not in ORDER_STATES:
            raise ValueError(f"bad terminal state {state!r}")
        self._update_order(client_order_id, state=state, terminal_ts=_now(), note=note)
        self.log_event("ORDER_TERMINAL", {"state": state, "note": note},
                       client_order_id=client_order_id)

    def _update_order(self, client_order_id: str, **fields) -> None:
        fields = {k: v for k, v in fields.items() if v is not None}
        if not fields:
            return
        sets = ", ".join(f"{k}=?" for k in fields)
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            cur = self.conn.execute(
                f"UPDATE smc_orders SET {sets} WHERE client_order_id=?",
                (*fields.values(), client_order_id),
            )
            self.conn.execute("COMMIT")
        except sqlite3.Error as e:
            with contextlib.suppress(Exception):
                self.conn.execute("ROLLBACK")
            raise SmcStateError(f"order update failed for {client_order_id!r}: {e}") from e
        if cur.rowcount == 0:
            raise SmcStateError(f"no such order to update: {client_order_id!r}")

    # ---------------------------------------------------- position mutations
    def _update_position(self, position_id: str, **fields) -> None:
        fields = {k: v for k, v in fields.items() if v is not None}
        if not fields:
            return
        if "state" in fields and fields["state"] not in POSITION_STATES:
            raise ValueError(f"bad position state {fields['state']!r}")
        sets = ", ".join(f"{k}=?" for k in fields)
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            cur = self.conn.execute(
                f"UPDATE smc_positions SET {sets} WHERE position_id=?",
                (*fields.values(), position_id),
            )
            self.conn.execute("COMMIT")
        except sqlite3.Error as e:
            with contextlib.suppress(Exception):
                self.conn.execute("ROLLBACK")
            raise SmcStateError(f"position update failed for {position_id!r}: {e}") from e
        if cur.rowcount == 0:
            raise SmcStateError(f"no such position to update: {position_id!r}")

    def set_position_state(self, position_id: str, state: str, note: str = "") -> None:
        self._update_position(position_id, state=state, recon_note=note or None)
        self.log_event("POSITION_STATE", {"state": state, "note": note}, position_id=position_id)

    def record_entry_filled(self, position_id: str, filled_qty: int, fill_price: float,
                            fully_filled: bool) -> None:
        self._update_position(
            position_id, state=OPEN if fully_filled else PARTIAL, filled_qty=filled_qty,
            entry_fill_price=fill_price, entry_filled_ts=_now(),
        )
        self.log_event("ENTRY_FILLED", {"filled_qty": filled_qty, "fill_price": fill_price,
                                         "fully_filled": fully_filled}, position_id=position_id)

    def record_position_closed(self, position_id: str, *, exit_fill_price: float,
                               closed_qty: int, realized_pnl: float, exit_reason: str) -> None:
        self._update_position(
            position_id, state=CLOSED, exit_fill_price=exit_fill_price, closed_qty=closed_qty,
            realized_pnl=realized_pnl, exit_reason=exit_reason, closed_ts=_now(),
        )
        self.log_event("POSITION_CLOSED", {"exit_fill_price": exit_fill_price,
                                            "closed_qty": closed_qty, "realized_pnl": realized_pnl,
                                            "exit_reason": exit_reason}, position_id=position_id)

    def set_reconciled_qty(self, position_id: str, broker_qty: int, note: str) -> None:
        """Broker is authoritative. This deliberately OVERWRITES the local cache
        rather than reconciling toward it."""
        self._update_position(position_id, filled_qty=broker_qty, last_recon_ts=_now(),
                               recon_note=note)
        self.log_event("RECON_QTY", {"broker_qty": broker_qty, "note": note},
                       position_id=position_id)

    def mark_dashboard_synced(self, position_id: str, ok: bool, note: str = "") -> None:
        """Reporting-only bookkeeping. Never gates an exit; a False here means the
        dashboard is behind, not that risk management should pause."""
        try:
            self._update_position(position_id, dashboard_synced=1 if ok else 0)
        except SmcStateError as e:
            logger.error("dashboard-sync flag write failed (non-fatal): %s", e)
        self.log_event("DASHBOARD_SYNC", {"ok": ok, "note": note}, position_id=position_id)

    # ------------------------------------------------------------- accessors
    def _query(self, sql: str, params: Iterable = ()) -> list:
        try:
            return self.conn.execute(sql, tuple(params)).fetchall()
        except sqlite3.Error as e:
            raise SmcStateError(
                f"SMC state query failed ({e}). Refusing to return an empty result -- "
                "callers must not mistake a broken read for 'no positions'."
            ) from e

    def get_position(self, position_id: str):
        rows = self._query("SELECT * FROM smc_positions WHERE position_id=?", (position_id,))
        return rows[0] if rows else None

    def get_position_by_signal(self, signal_key: str):
        rows = self._query("SELECT * FROM smc_positions WHERE signal_key=?", (signal_key,))
        return rows[0] if rows else None

    def get_order(self, client_order_id: str):
        rows = self._query("SELECT * FROM smc_orders WHERE client_order_id=?", (client_order_id,))
        return rows[0] if rows else None

    def orders_for_position(self, position_id: str, role: Optional[str] = None) -> list:
        if role:
            return self._query(
                "SELECT * FROM smc_orders WHERE position_id=? AND role=? ORDER BY attempt",
                (position_id, role))
        return self._query(
            "SELECT * FROM smc_orders WHERE position_id=? ORDER BY role, attempt", (position_id,))

    def live_positions(self) -> list:
        """Everything that may still carry broker exposure, INCLUDING intents
        (a crash between intent-commit and submit can leave a real order at the
        broker) and RECON_MISMATCH."""
        placeholders = ",".join("?" * (len(LIVE_POSITION_STATES) + 1))
        states = (INTENT,) + LIVE_POSITION_STATES
        return self._query(
            f"SELECT * FROM smc_positions WHERE state IN ({placeholders}) ORDER BY intent_ts",
            states)

    def open_positions(self) -> list:
        """Filled or ambiguous positions needing exit supervision.

        RECON_MISMATCH is intentionally included. Excluding it would turn a
        reconciliation warning into an unmanaged-position bug: the position
        most in need of broker-authoritative checks would vanish from the
        supervisor worklist.
        """
        placeholders = ",".join("?" * len(SUPERVISED_POSITION_STATES))
        return self._query(
            f"SELECT * FROM smc_positions WHERE state IN ({placeholders}) ORDER BY intent_ts",
            SUPERVISED_POSITION_STATES)

    def unresolved_intents(self) -> list:
        """INTENT/SUBMITTED rows whose broker outcome is unknown -- the startup
        reconciliation worklist."""
        return self._query(
            "SELECT DISTINCT p.* FROM smc_positions p "
            "JOIN smc_orders o ON o.position_id=p.position_id "
            "WHERE o.role=? AND o.state IN (?,?,?) ORDER BY p.intent_ts",
            (ROLE_ENTRY, INTENT, SUBMITTED, PENDING_CANCEL))

    def has_unresolved_entry_order(self, position_id: str) -> bool:
        rows = self._query(
            "SELECT 1 FROM smc_orders WHERE position_id=? AND role=? "
            "AND state IN (?,?,?) LIMIT 1",
            (position_id, ROLE_ENTRY, INTENT, SUBMITTED, PENDING_CANCEL))
        return bool(rows)

    def unresolved_exit_orders(self) -> list:
        return self._query(
            "SELECT * FROM smc_orders WHERE role=? "
            "AND state IN (?,?,?,?) ORDER BY intent_ts",
            (ROLE_EXIT, INTENT, SUBMITTED, EXIT_SUBMITTED, PENDING_CANCEL))



    def has_unresolved_exit_order(self, position_id: str) -> bool:
        rows = self._query(
            "SELECT 1 FROM smc_orders WHERE position_id=? AND role=? "
            "AND state IN (?,?,?,?) LIMIT 1",
            (position_id, ROLE_EXIT, INTENT, SUBMITTED, EXIT_SUBMITTED, PENDING_CANCEL))
        return bool(rows)

    def positions_in_mismatch(self) -> list:
        return self._query("SELECT * FROM smc_positions WHERE state=?", (RECON_MISMATCH,))

    def realized_pnl_since(self, since_iso: str) -> float:
        rows = self._query(
            "SELECT COALESCE(SUM(realized_pnl), 0.0) AS s FROM smc_positions "
            "WHERE realized_pnl IS NOT NULL AND closed_ts >= ?", (since_iso,))
        return float(rows[0]["s"]) if rows else 0.0

    def closed_positions_since(self, since_iso: str) -> list:
        return self._query(
            "SELECT * FROM smc_positions WHERE closed_ts IS NOT NULL AND closed_ts >= ? "
            "ORDER BY closed_ts", (since_iso,))

    def entries_since(self, since_iso: str) -> list:
        return self._query(
            "SELECT * FROM smc_positions WHERE intent_ts >= ? ORDER BY intent_ts", (since_iso,))

    def recent_closed_ordered(self, limit: int = 20) -> list:
        return self._query(
            "SELECT * FROM smc_positions WHERE state=? AND realized_pnl IS NOT NULL "
            "ORDER BY closed_ts DESC LIMIT ?", (CLOSED, limit))

    def execution_failure_count_since(self, since_iso: str) -> int:
        rows = self._query(
            "SELECT COUNT(*) AS c FROM smc_events WHERE kind=? AND ts >= ?",
            ("EXECUTION_FAILURE", since_iso))
        return int(rows[0]["c"]) if rows else 0

    def open_premium_at_risk(self) -> float:
        """Dollars of premium currently exposed, using reconciled quantities."""
        states = (INTENT,) + LIVE_POSITION_STATES
        placeholders = ",".join("?" * len(states))
        rows = self._query(
            "SELECT COALESCE(SUM("
            "  COALESCE(entry_fill_price, entry_limit_price, 0) * "
            "  (CASE WHEN filled_qty > 0 THEN filled_qty ELSE intended_qty END) * 100"
            f"), 0.0) AS s FROM smc_positions WHERE state IN ({placeholders})",
            states)
        return float(rows[0]["s"]) if rows else 0.0

    def occ_owned_by_smc(self, occ: str) -> list:
        """Every non-terminal SMC position on this exact OCC. Used by the
        cross-strategy ownership check."""
        placeholders = ",".join("?" * (len(LIVE_POSITION_STATES) + 1))
        states = (INTENT,) + LIVE_POSITION_STATES
        return self._query(
            f"SELECT * FROM smc_positions WHERE occ=? AND state IN ({placeholders})",
            (occ, *states))
