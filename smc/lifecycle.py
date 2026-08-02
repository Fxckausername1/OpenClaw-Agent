"""SMC order lifecycle: entry with a real TTL (Phase 2) and urgent liquidation
(Phase 3).

The two defects this replaces, both measured on 2026-07-31:

ENTRY -- orders were `time_in_force=day` limits with no TTL and no cancel path.
A signal fires off a 1-minute bar; the order could rest and fill many minutes
later, long after the edge that justified it had gone. Combined with a
227.5-407.3s (median 352.85s) signal-to-submit pipeline delay, the strategy was
routinely taking trades the backtest never modelled. Now: short TTL, cancel,
confirm terminal, and re-reconcile to catch a fill that lands DURING the cancel.

EXIT -- urgent stops were limits priced exactly at a lagging indicative bid, so
in a fast move they rested unfilled. One stop retried 6 times over 10 minutes,
each retry pricing off a quote that was already stale again, and realised ~3x the
intended -20% stop. The 5%-below-bid patch that followed reduced the odds but
still cannot GUARANTEE marketability -- a limit derived from indicative data is
an estimate, not a crossing order. Now: a real market order in paper (hard-blocked
for live money without two explicit switches), with an escalating marketable-limit
ladder as the fallback when a market order is refused.

Invariants held throughout:
* Broker quantity is authoritative. There is no `qty=1` anywhere in this module.
* No replacement order is ever submitted until the prior one is FILLED, CANCELED,
  or REJECTED -- so the strategy can never have two live exits racing.
* Broker quantity is re-read after every fill/cancel event.
* State is committed before any dashboard write or notification.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import logging
import time
from typing import Optional
from zoneinfo import ZoneInfo

from . import calendar as smc_calendar
from .broker import (
    Broker, BrokerOrder, BrokerRejected, BrokerTimeout, Quote, resolve_after_timeout,
)
from .config import URGENT_LIMIT_LADDER, URGENT_MARKET
from .risk import record_execution_failure
from .state import (
    CANCELED, CLOSED, EXIT_SUBMITTED, OPEN, PARTIAL, PENDING_CANCEL, RECON_MISMATCH,
    REJECTED, ROLE_ENTRY, ROLE_EXIT, SUBMITTED, DuplicateSignal, SmcStateError, SmcStateStore,
)

ET = ZoneInfo("America/New_York")
logger = logging.getLogger("smc.lifecycle")

# Exit reasons.
EXIT_TARGET = "TARGET"
EXIT_STOP = "STOP"
EXIT_TIME_STOP = "TIME_STOP"
EXIT_FORCED_CLOSE = "FORCED_CLOSE"
URGENT_REASONS = (EXIT_STOP, EXIT_TIME_STOP, EXIT_FORCED_CLOSE)


class Clock:
    """Injectable so tests never actually sleep."""

    def now(self) -> dt.datetime:
        return dt.datetime.now(dt.timezone.utc)

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


@dataclasses.dataclass
class EntryOutcome:
    position_id: Optional[str]
    client_order_id: Optional[str]
    state: str
    filled_qty: int = 0
    fill_price: Optional[float] = None
    reason: str = ""

    @property
    def is_open(self) -> bool:
        return self.state in (OPEN, PARTIAL)


@dataclasses.dataclass
class ExitOutcome:
    position_id: str
    state: str
    closed_qty: int = 0
    exit_fill_price: Optional[float] = None
    realized_pnl: Optional[float] = None
    attempts: int = 0
    reason: str = ""
    used_policy: str = ""


# ============================================================== ENTRY (Phase 2)

def signal_age_seconds(signal_ts: str, now: dt.datetime) -> float:
    parsed = dt.datetime.fromisoformat(str(signal_ts).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return (now - parsed).total_seconds()


def submit_entry(
    store: SmcStateStore, broker: Broker, config, *, signal_key: str, occ: str,
    underlying: str, contract_right: str, signal_side: str, intended_qty: int,
    limit_price: float, signal_ts: str, detected_ts: Optional[str] = None,
    selected_ts: Optional[str] = None, trigger_kind: Optional[str] = None,
    trigger_score: Optional[float] = None, arm: bool = False, clock: Optional[Clock] = None,
) -> EntryOutcome:
    """Intent -> submit -> ack -> wait TTL -> fill or cancel-and-settle.

    `arm=False` (the default, and what the disarmed wrapper runs) records the
    intent and logs what WOULD be sent, then rolls the intent to CANCELED so a
    dry run never leaves phantom state behind."""
    clock = clock or Clock()
    now = clock.now()

    # --- stale-signal rejection. Refusing to trade late is a real outcome.
    age = signal_age_seconds(signal_ts, now)
    if age > config.max_signal_age_seconds:
        store.log_event("SIGNAL_REJECTED_STALE",
                        {"signal_key": signal_key, "age_seconds": round(age, 2),
                         "max": config.max_signal_age_seconds})
        return EntryOutcome(None, None, CANCELED, reason=(
            f"signal {signal_key} rejected: {age:.1f}s old exceeds "
            f"{config.max_signal_age_seconds:.0f}s max age"))

    # --- intent BEFORE submit; the DB enforces one-position-per-signal.
    try:
        intent = store.create_entry_intent(
            signal_key=signal_key, occ=occ, underlying=underlying,
            contract_right=contract_right, signal_side=signal_side, intended_qty=intended_qty,
            limit_price=limit_price, order_type="limit", signal_ts=signal_ts,
            detected_ts=detected_ts, selected_ts=selected_ts, trigger_kind=trigger_kind,
            trigger_score=trigger_score,
        )
    except DuplicateSignal as e:
        return EntryOutcome(None, None, CANCELED, reason=f"duplicate signal suppressed: {e}")

    position_id, coid = intent["position_id"], intent["client_order_id"]

    payload = {
        "symbol": occ, "qty": str(intended_qty), "side": "buy", "type": "limit",
        "limit_price": f"{round(limit_price, 2):.2f}", "time_in_force": "day",
        "position_intent": "buy_to_open", "client_order_id": coid,
    }

    if not arm:
        store.record_order_terminal(coid, CANCELED, "DRY RUN -- nothing submitted")
        store.set_position_state(position_id, CANCELED, "DRY RUN")
        logger.info("DRY RUN entry (not submitted): %s", payload)
        return EntryOutcome(position_id, coid, CANCELED, reason="dry run", )

    # --- submit. Mark the attempt first so a timeout is distinguishable from
    # "never tried".
    store.mark_order_submitted(coid)
    order: Optional[BrokerOrder]
    try:
        order = broker.submit_order(payload)
    except BrokerRejected as e:
        store.record_order_terminal(coid, REJECTED, str(e))
        store.set_position_state(position_id, REJECTED, str(e))
        record_execution_failure(store, f"entry rejected: {e}", position_id)
        return EntryOutcome(position_id, coid, REJECTED, reason=str(e))
    except BrokerTimeout as e:
        # THE critical path. Never conclude failure -- ask by client_order_id.
        logger.error("entry POST ambiguous for %s (%s) -- resolving by client_order_id", coid, e)
        try:
            order = resolve_after_timeout(broker, coid)
        except BrokerTimeout as e2:
            # Genuinely unknown. Leave SUBMITTED so reconciliation retries; do NOT
            # finalise, and count it as an execution failure.
            record_execution_failure(store, f"entry outcome unresolved after timeout: {e2}",
                                     position_id)
            return EntryOutcome(position_id, coid, SUBMITTED, reason=(
                "order fate UNKNOWN after timeout; left SUBMITTED for reconciliation -- "
                "NOT assumed failed"))
        if order is None:
            store.record_order_terminal(coid, CANCELED, "broker 404 after timeout: never landed")
            store.set_position_state(position_id, CANCELED, "never reached the venue")
            record_execution_failure(store, "entry timed out and never landed", position_id)
            return EntryOutcome(position_id, coid, CANCELED,
                                reason="timed out; broker confirms it never landed")
        logger.error("entry %s DID land at the broker despite the timeout -- adopting it", coid)

    # --- persist the ack BEFORE any dashboard write or notification.
    if order.broker_order_id:
        store.record_broker_ack(coid, order.broker_order_id, state=SUBMITTED)

    return _await_entry_fill(store, broker, config, position_id, coid, intended_qty, order, clock)


def _await_entry_fill(store, broker, config, position_id, coid, intended_qty,
                      order: BrokerOrder, clock: Clock) -> EntryOutcome:
    """Poll to TTL, then cancel and settle. Handles fill-during-cancel."""
    deadline = clock.now() + dt.timedelta(seconds=config.entry_ttl_seconds)
    latest = order

    while clock.now() < deadline:
        if latest.is_filled or latest.filled_qty >= intended_qty:
            return _finalise_entry_fill(store, broker, position_id, coid, intended_qty, latest)
        if latest.is_terminal_unfilled:
            store.record_order_terminal(coid, CANCELED, f"broker terminal: {latest.status}")
            store.set_position_state(position_id, CANCELED, f"broker {latest.status}")
            return EntryOutcome(position_id, coid, CANCELED, reason=f"broker {latest.status}")
        clock.sleep(config.entry_poll_seconds)
        try:
            refreshed = broker.get_order_by_client_id(coid)
        except BrokerTimeout as e:
            logger.warning("entry poll failed for %s: %s", coid, e)
            continue
        if refreshed is not None:
            latest = refreshed

    # --- TTL expired. Partial fills count as real exposure and must be kept.
    if latest.filled_qty > 0:
        logger.warning("entry %s partially filled %d/%d at TTL -- keeping and protecting the fill",
                       coid, latest.filled_qty, intended_qty)

    store.set_position_state(position_id, PENDING_CANCEL, "entry TTL expired, cancel requested")
    return _cancel_entry_and_settle(store, broker, config, position_id, coid, intended_qty,
                                    latest, clock)


def _cancel_entry_and_settle(store, broker, config, position_id, coid, intended_qty,
                             latest: BrokerOrder, clock: Clock) -> EntryOutcome:
    """Request cancel, then CONFIRM a terminal broker state before deciding
    anything. A fill that lands during the cancel is registered and protected,
    never dropped -- that race is the whole reason this is a separate function
    with its own confirmation loop."""
    if latest.broker_order_id:
        try:
            broker.cancel_order(latest.broker_order_id)
        except BrokerTimeout as e:
            record_execution_failure(store, f"entry cancel transport failure: {e}", position_id)

    confirm_deadline = clock.now() + dt.timedelta(seconds=max(config.entry_ttl_seconds, 5.0))
    while clock.now() < confirm_deadline:
        try:
            refreshed = broker.get_order_by_client_id(coid)
        except BrokerTimeout:
            clock.sleep(config.entry_poll_seconds)
            continue
        if refreshed is not None:
            latest = refreshed
            if latest.is_terminal:
                break
        clock.sleep(config.entry_poll_seconds)

    # Broker quantity is authoritative -- re-read it after the cancel event.
    try:
        broker_qty = broker.get_position_qty(latest.occ or "")
    except BrokerTimeout as e:
        store.set_position_state(position_id, RECON_MISMATCH,
                                  f"cannot confirm broker qty after cancel: {e}")
        record_execution_failure(store, f"post-cancel qty read failed: {e}", position_id)
        return EntryOutcome(position_id, coid, RECON_MISMATCH,
                            reason="post-cancel quantity unverified -- flagged for reconciliation")

    if (broker_qty > 0 or latest.filled_qty > 0) and not latest.is_terminal:
        effective_qty = broker_qty or latest.filled_qty
        price = latest.avg_fill_price
        if broker_qty > 0 and price is not None and float(price) > 0:
            fully = effective_qty >= intended_qty
            store.record_entry_filled(position_id, effective_qty, float(price), fully)
        detail = (
            f"entry cancel is UNCONFIRMED while exposure may exist: broker_qty={broker_qty}, "
            f"order_filled_qty={latest.filled_qty}, status={latest.status}. "
            "Entry order remains unresolved; protective exit must not race it"
        )
        store.set_position_state(position_id, RECON_MISMATCH, detail)
        record_execution_failure(store, detail, position_id)
        return EntryOutcome(position_id, coid, RECON_MISMATCH,
                            filled_qty=max(int(effective_qty), 0),
                            reason=detail)
    if broker_qty > 0 or latest.filled_qty > 0:
        # Filled during (or before) the cancel. Register it and protect it.
        effective_qty = broker_qty or latest.filled_qty
        logger.error("FILL DURING CANCEL on %s: broker qty=%d (order filled_qty=%d) -- "
                     "registering and protecting", coid, broker_qty, latest.filled_qty)
        return _finalise_entry_fill(store, broker, position_id, coid, intended_qty, latest,
                                    override_qty=effective_qty,
                                    note="filled during cancel window")

    if not latest.is_terminal:
        store.set_position_state(position_id, RECON_MISMATCH,
                                  f"cancel unconfirmed, last broker status={latest.status}")
        return EntryOutcome(position_id, coid, RECON_MISMATCH,
                            reason=f"cancel not confirmed terminal (status={latest.status})")

    store.record_order_terminal(coid, CANCELED, f"TTL cancel confirmed: {latest.status}")
    store.set_position_state(position_id, CANCELED, "entry TTL expired, cancel confirmed, flat")
    return EntryOutcome(position_id, coid, CANCELED,
                        reason="entry unfilled within TTL and cancel confirmed -- flat")


def _finalise_entry_fill(store, broker, position_id, coid, intended_qty, order: BrokerOrder,
                         override_qty: Optional[int] = None, note: str = "") -> EntryOutcome:
    qty = override_qty if override_qty is not None else order.filled_qty
    price = order.avg_fill_price
    if qty <= 0 or price is None or float(price) <= 0:
        detail = (
            f"broker exposure exists but entry fill economics are incomplete: "
            f"qty={qty}, avg_fill_price={price!r}. Refusing to record a fabricated "
            "zero-dollar entry; reconciliation/manual broker review required"
        )
        store.set_position_state(position_id, RECON_MISMATCH, detail)
        record_execution_failure(store, detail, position_id)
        return EntryOutcome(position_id, coid, RECON_MISMATCH, filled_qty=max(int(qty), 0),
                            reason=detail)
    price = float(price)
    fully = qty >= intended_qty
    store.record_order_fill(coid, qty, price, OPEN if fully else CANCELED)
    store.record_entry_filled(position_id, qty, price, fully)
    return EntryOutcome(position_id, coid, OPEN if fully else PARTIAL, filled_qty=qty,
                        fill_price=price, reason=note or "filled")


# =============================================================== EXIT (Phase 3)

def decide_exit(entry_fill_price: float, quote: Quote, opened_at: dt.datetime,
                now: dt.datetime, exit_config, schedule, config) -> Optional[str]:
    """The SAME validated exit rule as the backtest -- target/stop/time-stop values
    come from bt2_exits.ExitConfig and are NOT tuned here. The only change from the
    old live version is that FORCED_CLOSE now uses the REAL session close from the
    market calendar instead of a hard-coded 15:30.

    Returns a reason string or None. Uses the quote's bid; the caller is
    responsible for having verified the quote is usable."""
    if quote is None or quote.bid is None:
        return None
    bid = float(quote.bid)
    target_level = entry_fill_price * (1 + exit_config.target_return)
    stop_level = entry_fill_price * (1 + exit_config.premium_stop_pct)

    if bid >= target_level:
        return EXIT_TARGET
    if bid <= stop_level:
        return EXIT_STOP

    now_et = now.astimezone(ET)
    should_flatten, _ = smc_calendar.must_flatten(now_et, schedule, config)
    if should_flatten:
        return EXIT_FORCED_CLOSE

    elapsed_min = (now - opened_at).total_seconds() / 60.0
    if elapsed_min >= exit_config.time_stop_minutes and bid <= entry_fill_price:
        return EXIT_TIME_STOP
    return None


def _urgent_payload(occ: str, qty: int, coid: str, policy: str,
                    quote: Optional[Quote], offset: float) -> dict:
    base = {"symbol": occ, "qty": str(qty), "side": "sell", "time_in_force": "day",
            "position_intent": "sell_to_close", "client_order_id": coid}
    if policy == URGENT_MARKET:
        base["type"] = "market"
        return base
    # Ladder rung: cross progressively deeper through the CURRENT bid.
    bid = float(quote.bid) if quote and quote.bid else 0.01
    base["type"] = "limit"
    base["limit_price"] = f"{max(round(bid * (1 - offset), 2), 0.01):.2f}"
    return base


def execute_exit(
    store: SmcStateStore, broker: Broker, config, position, reason: str, *,
    arm: bool = False, clock: Optional[Clock] = None,
) -> ExitOutcome:
    """Close a position, sizing off BROKER-CONFIRMED quantity.

    Urgent reasons (STOP/TIME_STOP/FORCED_CLOSE) use the configured urgent policy:
    a market order in paper, escalating marketable limits if the venue refuses a
    market order. TARGET stays a plain limit at the bid -- it is not urgent, and
    every TARGET exit on 2026-07-31 filled immediately at the quote."""
    clock = clock or Clock()
    position_id, occ = position["position_id"], position["occ"]
    urgent = reason in URGENT_REASONS

    # --- quantity: broker is authoritative. No qty=1 assumption anywhere.
    try:
        qty = broker.get_position_qty(occ)
    except BrokerTimeout as e:
        store.set_position_state(position_id, RECON_MISMATCH, f"pre-exit qty read failed: {e}")
        record_execution_failure(store, f"pre-exit qty read failed: {e}", position_id)
        return ExitOutcome(position_id, RECON_MISMATCH, reason=(
            f"cannot establish broker quantity for {occ} -- refusing to guess a size"))

    if qty <= 0:
        # Broker is flat. Either it closed elsewhere or we never really had it.
        local_qty = int(position["filled_qty"] or 0)
        if local_qty > 0:
            store.set_position_state(position_id, RECON_MISMATCH,
                                      f"local filled_qty={local_qty} but broker flat on {occ}")
            return ExitOutcome(position_id, RECON_MISMATCH,
                               reason="broker flat while local believed open -- flagged")
        store.set_position_state(position_id, CLOSED, "broker flat, nothing to exit")
        return ExitOutcome(position_id, CLOSED, reason="already flat at broker")

    policy = config.urgent_exit_policy if urgent else "LIMIT_AT_BID"
    if urgent and policy == URGENT_MARKET:
        permitted, why = config.market_orders_permitted()
        if not permitted:
            logger.error("urgent market order not permitted (%s) -- using limit ladder", why)
            policy = URGENT_LIMIT_LADDER

    remaining = qty
    attempts = 0
    fills: list = []
    offsets = config.urgent_ladder_offsets if urgent else (0.0,)
    max_attempts = config.urgent_max_attempts if urgent else 1

    while remaining > 0 and attempts < max_attempts:
        quote = None
        try:
            quote = broker.get_quote(occ)
        except Exception as e:  # noqa: BLE001 -- a quote failure must not stop a stop-loss
            logger.warning("quote unavailable for %s during exit: %s", occ, e)

        if policy != URGENT_MARKET and (quote is None or quote.bid is None or quote.bid <= 0):
            # A limit needs a price; a market order does not. If we cannot price a
            # limit and market orders are available, escalate rather than stall.
            if urgent and config.market_orders_permitted()[0]:
                logger.error("no usable bid for %s -- escalating to a market order", occ)
                policy = URGENT_MARKET
            else:
                attempts += 1
                store.log_event("EXIT_NO_QUOTE", {"occ": occ, "attempt": attempts},
                                position_id=position_id)
                clock.sleep(config.urgent_poll_seconds)
                continue

        offset = offsets[min(attempts, len(offsets) - 1)]
        effective_policy = URGENT_MARKET if policy == URGENT_MARKET else URGENT_LIMIT_LADDER
        if not urgent:
            effective_policy = URGENT_LIMIT_LADDER  # plain limit at bid (offset 0.0)

        intent = store.create_exit_intent(
            position_id=position_id, occ=occ, intended_qty=remaining,
            order_type="market" if effective_policy == URGENT_MARKET else "limit",
            limit_price=None if effective_policy == URGENT_MARKET else round(
                max(float(quote.bid) * (1 - offset), 0.01), 2),
            exit_reason=reason,
        )
        coid = intent["client_order_id"]
        payload = _urgent_payload(occ, remaining, coid, effective_policy, quote, offset)

        if not arm:
            store.record_order_terminal(coid, CANCELED, "DRY RUN -- nothing submitted")
            logger.info("DRY RUN exit (not submitted): %s", payload)
            return ExitOutcome(position_id, position["state"], attempts=attempts + 1,
                               reason="dry run", used_policy=effective_policy)

        store.mark_order_submitted(coid)
        attempts += 1
        try:
            order = broker.submit_order(payload)
        except BrokerRejected as e:
            store.record_order_terminal(coid, REJECTED, str(e))
            record_execution_failure(store, f"exit rejected ({effective_policy}): {e}", position_id)
            if effective_policy == URGENT_MARKET:
                logger.error("market exit REJECTED for %s (%s) -- falling back to limit ladder",
                             occ, e)
                policy = URGENT_LIMIT_LADDER
            continue
        except BrokerTimeout as e:
            logger.error("exit POST ambiguous for %s (%s) -- resolving by client_order_id", coid, e)
            try:
                order = resolve_after_timeout(broker, coid)
            except BrokerTimeout as e2:
                record_execution_failure(store, f"exit outcome unresolved: {e2}", position_id)
                store.set_position_state(position_id, EXIT_SUBMITTED,
                                          "exit outcome unknown after timeout")
                return ExitOutcome(position_id, EXIT_SUBMITTED, attempts=attempts, reason=(
                    "exit fate UNKNOWN after timeout -- left EXIT_SUBMITTED for reconciliation, "
                    "supervision continues"))
            if order is None:
                store.record_order_terminal(coid, CANCELED, "broker 404 after timeout")
                continue

        if order.broker_order_id:
            store.record_broker_ack(coid, order.broker_order_id, state=EXIT_SUBMITTED)
        store.set_position_state(position_id, EXIT_SUBMITTED, f"{reason} via {effective_policy}")

        settled = _settle_exit_attempt(store, broker, config, coid, order, clock)
        if settled.filled_qty > 0:
            fills.append((settled.filled_qty, settled.avg_fill_price or 0.0))
        if not settled.terminal:
            # The prior order may still fill. Submitting a replacement here can
            # oversell the position; stop the ladder until reconciliation proves
            # the first order terminal.
            detail = (f"exit order {coid} did not reach a confirmed terminal state; "
                      "NO replacement submitted because the prior order may still fill")
            store.set_position_state(position_id, RECON_MISMATCH, detail)
            record_execution_failure(store, detail, position_id)
            return ExitOutcome(position_id, RECON_MISMATCH, attempts=attempts,
                               reason=detail, used_policy=effective_policy)

        # Re-read broker quantity after EVERY fill/cancel event.
        try:
            remaining = broker.get_position_qty(occ)
        except BrokerTimeout as e:
            store.set_position_state(position_id, RECON_MISMATCH,
                                      f"post-exit qty read failed: {e}")
            record_execution_failure(store, f"post-exit qty read failed: {e}", position_id)
            return ExitOutcome(position_id, RECON_MISMATCH, attempts=attempts, reason=(
                "exit submitted but resulting quantity unverified -- flagged, supervision continues"))

    if remaining > 0:
        store.set_reconciled_qty(position_id, remaining,
                                 f"post-exit broker quantity: {remaining} still open")
        if not urgent:
            # A target is opportunistic. A confirmed cancel with residual
            # exposure is normal, consistent state—not a reconciliation fault.
            store.set_position_state(position_id, OPEN,
                                     "target limit canceled/unfilled; position remains supervised")
            return ExitOutcome(position_id, OPEN, closed_qty=sum(q for q, _ in fills),
                               attempts=attempts,
                               reason="target limit did not fill; confirmed canceled",
                               used_policy=policy)
        store.set_position_state(position_id, RECON_MISMATCH, (
            f"exit exhausted {attempts} attempts with {remaining} contract(s) still open"))
        record_execution_failure(store,
                                 f"exit could not fully liquidate {occ}: {remaining} left",
                                 position_id)
        return ExitOutcome(position_id, RECON_MISMATCH, closed_qty=sum(q for q, _ in fills),
                           attempts=attempts,
                           reason=f"{remaining} contract(s) STILL OPEN after {attempts} attempts",
                           used_policy=policy)

    # Reconstruct from durable order rows, not only this function invocation.
    # A prior partial close followed by a restart/retry must still be included in
    # final quantity, average exit, and P&L.
    exit_orders = store.orders_for_position(position_id, role=ROLE_EXIT)
    durable_fills = [
        (int(row["filled_qty"] or 0), row["avg_fill_price"])
        for row in exit_orders if int(row["filled_qty"] or 0) > 0
    ]
    total_qty = sum(q for q, _ in durable_fills)
    if total_qty <= 0 or any(price is None or float(price) <= 0 for _, price in durable_fills):
        detail = ("broker is flat but durable exit fill quantity/price is incomplete; "
                  "refusing to fabricate realized P&L")
        store.set_position_state(position_id, RECON_MISMATCH, detail)
        record_execution_failure(store, detail, position_id)
        return ExitOutcome(position_id, RECON_MISMATCH, closed_qty=total_qty,
                           attempts=attempts, reason=detail, used_policy=policy)
    avg_exit = sum(q * float(p) for q, p in durable_fills) / total_qty
    entry_price = float(position["entry_fill_price"] or 0.0)
    realized = round((avg_exit - entry_price) * total_qty * 100, 2)
    store.record_position_closed(position_id, exit_fill_price=round(avg_exit, 4),
                                 closed_qty=total_qty, realized_pnl=realized, exit_reason=reason)
    return ExitOutcome(position_id, CLOSED, closed_qty=total_qty, exit_fill_price=round(avg_exit, 4),
                       realized_pnl=realized, attempts=attempts, reason=reason, used_policy=policy)


@dataclasses.dataclass
class _Settlement:
    filled_qty: int = 0
    avg_fill_price: Optional[float] = None
    terminal: bool = False


def _settle_exit_attempt(store, broker, config, coid: str, order: BrokerOrder,
                         clock: Clock) -> _Settlement:
    """Drive ONE exit order to a terminal broker state before the caller is allowed
    to try another rung. This is the "never replace a non-terminal order" guarantee:
    the ladder cannot race itself, because escalation only happens after this
    returns."""
    deadline = clock.now() + dt.timedelta(seconds=config.urgent_attempt_timeout_seconds)
    latest = order

    while clock.now() < deadline:
        if latest.is_terminal:
            break
        clock.sleep(config.urgent_poll_seconds)
        try:
            refreshed = broker.get_order_by_client_id(coid)
        except BrokerTimeout:
            continue
        if refreshed is not None:
            latest = refreshed

    if not latest.is_terminal and latest.broker_order_id:
        # Not terminal within the attempt window: cancel and CONFIRM before moving on.
        store.set_position_state(store.get_order(coid)["position_id"], PENDING_CANCEL,
                                  f"exit attempt {coid} timed out, cancelling")
        try:
            broker.cancel_order(latest.broker_order_id)
        except BrokerTimeout as e:
            record_execution_failure(store, f"exit cancel failed: {e}")
        cancel_deadline = clock.now() + dt.timedelta(seconds=config.urgent_attempt_timeout_seconds)
        while clock.now() < cancel_deadline:
            try:
                refreshed = broker.get_order_by_client_id(coid)
            except BrokerTimeout:
                clock.sleep(config.urgent_poll_seconds)
                continue
            if refreshed is not None:
                latest = refreshed
                if latest.is_terminal:
                    break
            clock.sleep(config.urgent_poll_seconds)

    state = CLOSED if latest.is_filled else (CANCELED if latest.is_terminal_unfilled else EXIT_SUBMITTED)
    if latest.filled_qty > 0:
        # Includes fill-during-cancel on the exit side.
        store.record_order_fill(coid, latest.filled_qty, latest.avg_fill_price, state)
    elif latest.is_terminal:
        store.record_order_terminal(coid, state, f"broker status={latest.status}")
    else:
        # Do not stamp terminal_ts on an order whose cancel is unconfirmed.
        store.log_event("ORDER_NONTERMINAL_AFTER_CANCEL",
                        {"client_order_id": coid, "broker_status": latest.status},
                        client_order_id=coid)
    return _Settlement(filled_qty=latest.filled_qty, avg_fill_price=latest.avg_fill_price,
                       terminal=latest.is_terminal)
