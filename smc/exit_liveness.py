"""Liveness for in-flight exits.

"In-flight until a terminal trade_updates event" stops duplicate exits, but
on its own it creates a worse failure than the one it prevents: if a
WebSocket event is LOST, the position stays marked in-flight forever and is
never exited. A stuck protective exit is unmanaged risk wearing the costume
of a managed position.

So every in-flight exit carries a MONOTONIC RECONCILIATION DEADLINE. If no
broker update arrives by then, we ask Alpaca directly, by deterministic
client_order_id, what actually happened. We never resubmit blindly to find
out -- that is how a full-quantity exit becomes two.

Quantity is tracked, not assumed. A partial fill leaves a REMAINING
quantity that is still exposed and still needs managing, and the next exit
attempt is sized to the remaining quantity only. Exiting 1 contract twice
because the first attempt partially filled would open a short position.

If fate stays unknown after reconciliation, the position enters
UNMANAGED_RISK: raised as a critical incident to dashboard and Telegram, and
explicitly NOT treated as closed. Pretending a position is flat when we
cannot prove it is the single most dangerous thing this module could do.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import logging
import time
from typing import Callable, Optional

logger = logging.getLogger("smc.exit_liveness")

# Exit attempt states
EXIT_PENDING = "PENDING"            # submitted, no terminal event yet
EXIT_PARTIAL = "PARTIAL"            # some filled, remainder still working
EXIT_FILLED = "FILLED"              # fully filled
EXIT_CANCELED = "CANCELED"          # canceled with remaining position
EXIT_REJECTED = "REJECTED"
EXIT_UNKNOWN = "UNKNOWN"            # fate unresolved -> UNMANAGED RISK

TERMINAL = frozenset({EXIT_FILLED, EXIT_CANCELED, EXIT_REJECTED})
DEFAULT_RECONCILE_SECONDS = 5.0
DEFAULT_MAX_RECONCILE_ATTEMPTS = 3


@dataclasses.dataclass
class ExitAttempt:
    position_id: str
    occ: str
    client_order_id: str
    intended_qty: float
    submitted_monotonic: float
    broker_order_id: Optional[str] = None
    filled_qty: float = 0.0
    state: str = EXIT_PENDING
    reconcile_deadline: float = 0.0
    reconcile_attempts: int = 0
    last_event_monotonic: Optional[float] = None
    last_error: Optional[str] = None
    unmanaged_risk: bool = False

    @property
    def remaining_qty(self) -> float:
        return max(self.intended_qty - self.filled_qty, 0.0)

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL

    @property
    def needs_followup_exit(self) -> bool:
        """Canceled or rejected with size still exposed -- a NEW attempt is
        required, sized to the remainder only."""
        return self.state in (EXIT_CANCELED, EXIT_REJECTED) and self.remaining_qty > 0

    def arm_deadline(self, now: float, seconds: float = DEFAULT_RECONCILE_SECONDS) -> None:
        self.reconcile_deadline = now + seconds

    def overdue(self, now: float) -> bool:
        return (not self.terminal and self.reconcile_deadline > 0
                and now >= self.reconcile_deadline)

    def as_dict(self) -> dict:
        d = dataclasses.asdict(self)
        d["remaining_qty"] = self.remaining_qty
        d["terminal"] = self.terminal
        return d


class ExitLivenessTracker:
    """Owns in-flight exits: deadlines, quantities, reconciliation, restart.

    `rest_lookup(client_order_id) -> dict|None` and `raise_incident(kind,
    detail)` are injected so this module never touches the broker or the
    notifier directly."""

    def __init__(self, *, rest_lookup: Callable, raise_incident: Callable,
                 persist: Optional[Callable] = None,
                 reconcile_seconds: float = DEFAULT_RECONCILE_SECONDS,
                 max_attempts: int = DEFAULT_MAX_RECONCILE_ATTEMPTS,
                 clock: Callable = time.monotonic):
        self.rest_lookup = rest_lookup
        self.raise_incident = raise_incident
        self.persist = persist
        self.reconcile_seconds = reconcile_seconds
        self.max_attempts = max_attempts
        self.clock = clock
        self.attempts: dict = {}          # client_order_id -> ExitAttempt
        self.entries_blocked = False
        self.lost_events = 0

    # ---------------------------------------------------------- lifecycle
    def register(self, *, position_id: str, occ: str, client_order_id: str,
                 intended_qty: float, broker_order_id: Optional[str] = None) -> ExitAttempt:
        a = ExitAttempt(position_id=position_id, occ=occ,
                        client_order_id=client_order_id, intended_qty=float(intended_qty),
                        submitted_monotonic=self.clock(), broker_order_id=broker_order_id)
        a.arm_deadline(self.clock(), self.reconcile_seconds)
        self.attempts[client_order_id] = a
        self._persist(a)
        return a

    def on_update(self, client_order_id: str, event: str, *, filled_qty=None,
                  broker_order_id=None, reason: str = "") -> Optional[ExitAttempt]:
        """Applies a trade_updates event. Out-of-order and duplicate events
        are tolerated: filled_qty only ever moves FORWARD, and a terminal
        state is never downgraded."""
        a = self.attempts.get(client_order_id)
        if a is None:
            return None
        now = self.clock()
        a.last_event_monotonic = now
        if broker_order_id and not a.broker_order_id:
            a.broker_order_id = broker_order_id
        if filled_qty is not None:
            a.filled_qty = max(a.filled_qty, float(filled_qty))

        if event in ("fill", "partial_fill"):
            if a.remaining_qty <= 0 or event == "fill":
                a.state = EXIT_FILLED
            else:
                a.state = EXIT_PARTIAL
                a.arm_deadline(now, self.reconcile_seconds)
        elif event == "canceled":
            # Only downgrade to CANCELED if we are not already fully filled;
            # a late cancel after a fill must not resurrect exposure.
            a.state = EXIT_FILLED if a.remaining_qty <= 0 else EXIT_CANCELED
        elif event in ("rejected", "expired"):
            a.state = EXIT_REJECTED if event == "rejected" else EXIT_CANCELED
            a.last_error = reason or event
        self._persist(a)
        return a

    # ------------------------------------------------------ reconciliation
    def due_for_reconcile(self) -> list:
        now = self.clock()
        return [a for a in self.attempts.values() if a.overdue(now)]

    def reconcile(self, attempt: ExitAttempt) -> ExitAttempt:
        """Asks the broker what actually happened. NEVER resubmits."""
        now = self.clock()
        attempt.reconcile_attempts += 1
        self.lost_events += 1
        try:
            row = self.rest_lookup(attempt.client_order_id)
        except Exception as e:  # noqa: BLE001
            row = None
            attempt.last_error = repr(e)

        if row:
            status = str(row.get("status", "")).lower()
            filled = row.get("filled_qty")
            if filled is not None:
                attempt.filled_qty = max(attempt.filled_qty, float(filled))
            if not attempt.broker_order_id:
                attempt.broker_order_id = row.get("id")
            if status in ("filled",):
                attempt.state = EXIT_FILLED
            elif status in ("partially_filled",):
                attempt.state = EXIT_PARTIAL
                attempt.arm_deadline(now, self.reconcile_seconds)
            elif status in ("canceled", "expired", "done_for_day"):
                attempt.state = (EXIT_FILLED if attempt.remaining_qty <= 0
                                 else EXIT_CANCELED)
            elif status in ("rejected",):
                attempt.state = EXIT_REJECTED
            else:
                attempt.arm_deadline(now, self.reconcile_seconds)
            self._persist(attempt)
            return attempt

        # No answer.
        if attempt.reconcile_attempts >= self.max_attempts:
            attempt.state = EXIT_UNKNOWN
            attempt.unmanaged_risk = True
            self.raise_incident("reconciliation_mismatch", {
                "position_id": attempt.position_id, "occ": attempt.occ,
                "client_order_id": attempt.client_order_id,
                "intended_qty": attempt.intended_qty,
                "filled_qty": attempt.filled_qty,
                "remaining_qty": attempt.remaining_qty,
                "state": "UNMANAGED_RISK",
                "detail": "exit order fate unknown after "
                          f"{attempt.reconcile_attempts} reconciliation attempts; "
                          "position is NOT confirmed closed",
            })
        else:
            attempt.arm_deadline(now, self.reconcile_seconds)
        self._persist(attempt)
        return attempt

    def on_stream_disconnect(self) -> list:
        """Block new entries and REST-reconcile everything active. An
        existing position keeps being protected -- degraded is not stopped."""
        self.entries_blocked = True
        active = [a for a in self.attempts.values() if not a.terminal]
        for a in active:
            self.reconcile(a)
        return active

    def on_stream_reconnect(self) -> None:
        self.entries_blocked = False

    # ------------------------------------------------------------ restart
    def restore(self, rows) -> list:
        """Rebuilds in-flight exits from SQLite after a restart, then
        reconciles BEFORE anything is submitted."""
        restored = []
        now = self.clock()
        for r in rows:
            a = ExitAttempt(
                position_id=r["position_id"], occ=r["occ"],
                client_order_id=r["client_order_id"],
                intended_qty=float(r.get("intended_qty", 0) or 0),
                submitted_monotonic=now,
                broker_order_id=r.get("broker_order_id"),
                filled_qty=float(r.get("filled_qty", 0) or 0),
                state=r.get("state", EXIT_PENDING))
            a.arm_deadline(now, 0.0)          # reconcile immediately
            self.attempts[a.client_order_id] = a
            restored.append(a)
        for a in restored:
            if not a.terminal:
                self.reconcile(a)
        return restored

    # ------------------------------------------------------------- status
    def unmanaged(self) -> list:
        return [a for a in self.attempts.values() if a.unmanaged_risk]

    def _persist(self, attempt: ExitAttempt) -> None:
        if self.persist is None:
            return
        try:
            self.persist(attempt)
        except Exception as e:  # noqa: BLE001 -- persistence failure must not
            # break exit management, but must be visible.
            logger.error("exit attempt persist failed (non-fatal): %s", e)

    def health(self) -> dict:
        return {
            "tracked": len(self.attempts),
            "in_flight": sum(1 for a in self.attempts.values() if not a.terminal),
            "partial": sum(1 for a in self.attempts.values() if a.state == EXIT_PARTIAL),
            "unmanaged_risk": len(self.unmanaged()),
            "lost_event_reconciles": self.lost_events,
            "entries_blocked": self.entries_blocked,
            "oldest_deadline_age_s": round(max(
                (self.clock() - a.reconcile_deadline
                 for a in self.attempts.values() if not a.terminal), default=0.0), 3),
        }
