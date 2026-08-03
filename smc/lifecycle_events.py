"""The canonical trade-lifecycle event vocabulary.

ONE source of truth for the twelve links heff requires between a detected
signal and a reconciled outcome. Everything that classifies, orders, tests or
renders a lifecycle event reads its answer from here, so a kind cannot be
"critical" to the outbox and "informational" to the dashboard.

THE CHAIN, in the order it must be delivered:

     1  signal detected
     2  signal rejected / accepted
     3  contract selected
     4  entry intent persisted
     5  entry submitted
     6  broker acknowledgment
     7  partial / full fill
     8  cancel / reject / unknown outcome
     9  exit trigger
    10  exit submitted
    11  exit fill
    12  reconciliation result

ORDERING, and why it is a priority decision rather than a sort.

The outbox drains by (priority, event_id). Before this module, lifecycle
kinds sat at priority 1 and "critical" kinds at priority 0, so a fill
committed AFTER a signal was delivered BEFORE it -- the chain arrived
scrambled. Every kind below is therefore priority 0: within the lifecycle,
delivery order is commit order, full stop. Only health/heartbeat chatter sits
behind at priority 5, where jumping the queue is harmless.

CRITICAL is a narrower claim than ORDERED, and the two are deliberately not
the same set:

    ORDERED   -> drains in commit order and is never coalesced.  Every kind
                 in this module.
    CRITICAL  -> additionally retries forever and is never abandoned, and is
                 counted as an outstanding obligation on the dashboard.
                 Only the money-touching subset.

A rejected signal is worth delivering in order; it is not worth retrying
until the end of time, and making it critical would bury a genuinely
undelivered fill under dozens of rejection notices during any Telegram
outage. An order, a fill, an exit and a reconciliation verdict are the
opposite: silence about them is indistinguishable from "it never happened",
which is the failure this whole layer exists to prevent.

HEAD-OF-LINE BLOCKING IS INTENDED. Because delivery must be ordered, a
failing event stops the batch instead of letting its successors overtake it.
A permanently undeliverable CRITICAL event will therefore stall the chain --
visibly, via `undelivered_critical` on the dashboard and via the
`notifications_operational` readiness gate, which blocks NEW entries while
the backlog persists. Managing an already-open position is never blocked.
That is the intended trade: a stalled, loudly-flagged chain beats a silently
reordered one. `MAX_MESSAGE_CHARS` exists so the most likely poison pill -- a
message Telegram refuses for length -- cannot be the thing that stalls it.
"""
from __future__ import annotations

# Telegram hard-rejects a sendMessage body over 4096 characters, and a 400 is
# permanent: it would fail every retry forever and, under ordered delivery,
# stall every event behind it. Formatting truncates to this instead.
MAX_MESSAGE_CHARS = 3500

# ---------------------------------------------------------------- stage 1
SIGNAL_DETECTED = "signal_detected"

# ---------------------------------------------------------------- stage 2
SIGNAL_ACCEPTED = "signal_accepted"
SIGNAL_REJECTED = "signal_rejected"
SIGNAL_BLOCKED = "signal_blocked"
SIGNAL_BLOCKED_MODE = "signal_blocked_mode"
SIGNAL_DUPLICATE_SUPPRESSED = "signal_duplicate_suppressed"
SIGNAL_EXCLUDED_SWEEP_RECLAIM = "signal_excluded_sweep_reclaim"
ENTRY_BLOCKED_OR_BUILD_FAILED = "entry_blocked_or_build_failed"

# ---------------------------------------------------------------- stage 3
CONTRACT_SELECTED = "contract_selected"
NO_ELIGIBLE_CONTRACT = "no_eligible_contract"

# ---------------------------------------------------------------- stage 4
ENTRY_INTENT_PERSISTED = "entry_intent_persisted"

# ---------------------------------------------------------------- stage 5
ENTRY_SUBMITTED = "entry_submitted"
ORDER_SUBMITTED = "order_submitted"

# ---------------------------------------------------------------- stage 6
BROKER_ACK = "broker_ack"

# ---------------------------------------------------------------- stage 7
ORDER_PARTIAL_FILL = "order_partial_fill"
ORDER_FILL = "order_fill"

# ---------------------------------------------------------------- stage 8
ORDER_CANCELED = "order_canceled"
ORDER_REJECTED = "order_rejected"
ORDER_UNKNOWN = "order_unknown"
ORDER_TERMINAL = "order_terminal"
ORDER_SUBMIT_FAILED_OR_UNKNOWN = "order_submit_failed_or_unknown"
ENTRY_SUBMIT_UNKNOWN = "entry_submit_unknown"

# ---------------------------------------------------------------- stage 9
EXIT_TRIGGER = "exit_trigger"
STOP_TRIGGERED = "stop_triggered"
TARGET_TRIGGERED = "target_triggered"

# --------------------------------------------------------------- stage 10
EXIT_SUBMITTED = "exit_submitted"
# A protective exit that was DECIDED but did not go out. Distinct from
# EXIT_SUBMIT_UNKNOWN (fate unknown after a timeout): this one definitively
# did not reach the broker, and an unprotected open position is precisely the
# thing that must never be announced only as a trigger.
EXIT_NOT_SUBMITTED = "exit_not_submitted"
EXIT_SUBMIT_UNKNOWN = "exit_submit_unknown"

# --------------------------------------------------------------- stage 11
EXIT_FILLED = "exit_filled"

# --------------------------------------------------------------- stage 12
RECONCILIATION_RESULT = "reconciliation_result"
RECONCILIATION_MISMATCH = "reconciliation_mismatch"

# Stage number for every kind. The number is the position in heff's chain,
# NOT a delivery rank -- delivery is by commit order. It exists so a test can
# assert "the chain advanced through these stages" without hard-coding one
# particular set of kinds, and so the dashboard can render progress.
STAGE_OF = {
    SIGNAL_DETECTED: 1,

    SIGNAL_ACCEPTED: 2,
    SIGNAL_REJECTED: 2,
    SIGNAL_BLOCKED: 2,
    SIGNAL_BLOCKED_MODE: 2,
    SIGNAL_DUPLICATE_SUPPRESSED: 2,
    SIGNAL_EXCLUDED_SWEEP_RECLAIM: 2,
    ENTRY_BLOCKED_OR_BUILD_FAILED: 2,

    CONTRACT_SELECTED: 3,
    NO_ELIGIBLE_CONTRACT: 3,

    ENTRY_INTENT_PERSISTED: 4,

    ENTRY_SUBMITTED: 5,
    ORDER_SUBMITTED: 5,

    BROKER_ACK: 6,

    ORDER_PARTIAL_FILL: 7,
    ORDER_FILL: 7,

    ORDER_CANCELED: 8,
    ORDER_REJECTED: 8,
    ORDER_UNKNOWN: 8,
    ORDER_TERMINAL: 8,
    ORDER_SUBMIT_FAILED_OR_UNKNOWN: 8,
    ENTRY_SUBMIT_UNKNOWN: 8,

    EXIT_TRIGGER: 9,
    STOP_TRIGGERED: 9,
    TARGET_TRIGGERED: 9,

    EXIT_SUBMITTED: 10,
    EXIT_NOT_SUBMITTED: 10,
    EXIT_SUBMIT_UNKNOWN: 10,

    EXIT_FILLED: 11,

    RECONCILIATION_RESULT: 12,
    RECONCILIATION_MISMATCH: 12,
}

# Every kind above: ordered by commit sequence, never coalesced.
LIFECYCLE_KINDS = frozenset(STAGE_OF)

# The money-touching subset: additionally retried forever, never abandoned,
# and counted as an outstanding obligation until delivered.
CRITICAL_KINDS = frozenset({
    ENTRY_INTENT_PERSISTED,
    ENTRY_SUBMITTED, ORDER_SUBMITTED,
    BROKER_ACK,
    ORDER_PARTIAL_FILL, ORDER_FILL,
    ORDER_CANCELED, ORDER_REJECTED, ORDER_UNKNOWN, ORDER_TERMINAL,
    ORDER_SUBMIT_FAILED_OR_UNKNOWN, ENTRY_SUBMIT_UNKNOWN,
    EXIT_TRIGGER, STOP_TRIGGERED, TARGET_TRIGGERED,
    EXIT_SUBMITTED, EXIT_NOT_SUBMITTED, EXIT_SUBMIT_UNKNOWN,
    EXIT_FILLED,
    RECONCILIATION_RESULT, RECONCILIATION_MISMATCH,
})

# Ordered but abandonable: worth delivering in sequence, not worth retrying
# forever. Derived rather than written out twice, so the two sets cannot
# drift apart.
ORDERED_NONCRITICAL_KINDS = LIFECYCLE_KINDS - CRITICAL_KINDS

# The minimum stage sequence an entry that fills and exits must produce.
# Used by the ordering tests as the expected spine of the chain.
FULL_ENTRY_TO_EXIT_STAGES = (1, 2, 3, 4, 5, 6, 7, 9, 10, 11, 12)


def stage_of(kind: str):
    """Chain position for a kind, or None if it is not a lifecycle event."""
    return STAGE_OF.get(kind)


def is_lifecycle(kind: str) -> bool:
    return kind in LIFECYCLE_KINDS


def is_critical(kind: str) -> bool:
    return kind in CRITICAL_KINDS


def format_message(kind: str, detail) -> str:
    """Human-readable, and bounded.

    Truncation is a delivery-safety requirement, not cosmetics: an
    over-length message is a permanent Telegram 400, and under ordered
    delivery one permanent failure stalls everything behind it.
    """
    body = f"{kind}: {detail}"
    if len(body) <= MAX_MESSAGE_CHARS:
        return body
    keep = MAX_MESSAGE_CHARS - len(kind) - 40
    return f"{kind}: {str(detail)[:max(keep, 0)]}... [truncated]"
