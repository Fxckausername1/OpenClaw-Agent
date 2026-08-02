"""Broker <-> local reconciliation and cross-strategy OCC ownership.

This module answers one question on every management cycle and every startup:
**does any broker exposure exist that SMC cannot account for?** If yes, new
entries halt and someone gets told -- but protective supervision of what we DO
know about keeps running, because pausing risk management is never the safe
response to confusion.

Two real facts about this account shape the design:

1. The Alpaca paper account is SHARED. On 2026-07-31 it held CHRW, VRT, XEL
   equity positions and NCLH/PSKY option-spread legs belonging to entirely
   different strategies, plus open non-SMC orders. So "there is an option
   position at the broker" does NOT imply "SMC owns it," and SMC must never
   size an exit off a quantity it hasn't established ownership of. The old code
   never checked ownership at all.

2. The dashboard ledger (`options_eval.db`) is where the other strategies record
   their legs, so it is the available evidence for FOREIGN ownership. It is used
   here strictly as *evidence*, never as SMC's control plane. If it can't be
   read, we lose the ability to disambiguate -- so unattributed exposure becomes
   a halt rather than an assumption.

Fail direction, stated explicitly: ambiguity HALTS ENTRIES and CONTINUES
SUPERVISION. Never the reverse.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import sqlite3
from pathlib import Path
from typing import Optional

from .state import (
    CANCELED, CLOSED, EXIT_SUBMITTED, INTENT, OPEN, PARTIAL, RECON_MISMATCH, REJECTED, ROLE_ENTRY,
    SUBMITTED, TERMINAL_STATES, SmcStateError, SmcStateStore,
)
from .broker import Broker, BrokerTimeout

logger = logging.getLogger("smc.reconcile")

SMC_STRATEGY_ID = "SMC_TRIANGLE"
# Underlyings SMC is allowed to trade. An unattributed option position on one of
# these is a candidate SMC orphan; on anything else it is another strategy's
# business and not ours to touch or to halt on.
SMC_UNDERLYINGS = ("QQQ",)


@dataclasses.dataclass
class ReconResult:
    adopted: list = dataclasses.field(default_factory=list)
    qty_corrections: list = dataclasses.field(default_factory=list)
    orphan_orders: list = dataclasses.field(default_factory=list)
    orphans: list = dataclasses.field(default_factory=list)
    ambiguous: list = dataclasses.field(default_factory=list)
    resolved_intents: list = dataclasses.field(default_factory=list)
    halt_reasons: list = dataclasses.field(default_factory=list)
    degraded: bool = False

    @property
    def clean(self) -> bool:
        return not (self.orphans or self.ambiguous or self.halt_reasons)


def foreign_occ_owners(dashboard_db: Path) -> tuple:
    """{occ: [strategy_id, ...]} for every NON-SMC, non-terminal position recorded
    in the shared dashboard ledger. Returns (mapping, readable) -- `readable=False`
    means we could not establish foreign ownership at all, which the caller must
    treat as "cannot disambiguate", not as "no foreign owners"."""
    mapping: dict = {}
    if not Path(dashboard_db).exists():
        return mapping, False
    try:
        conn = sqlite3.connect(f"file:{dashboard_db}?mode=ro", uri=True, timeout=10)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT strategy_id, legs_metadata FROM trades_ledger "
            "WHERE status IN ('PENDING','OPEN','PARTIAL_CLOSE')"
        ).fetchall()
        conn.close()
    except sqlite3.Error as e:
        logger.error("cannot read foreign ownership from dashboard ledger %s: %s", dashboard_db, e)
        return mapping, False

    for row in rows:
        if row["strategy_id"] == SMC_STRATEGY_ID:
            continue
        try:
            meta = json.loads(row["legs_metadata"] or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        for leg in meta.get("legs", []) or []:
            occ = leg.get("occ")
            if occ:
                mapping.setdefault(occ, []).append(row["strategy_id"])
    return mapping, True


def _resolve_unresolved_intent(store: SmcStateStore, broker: Broker, pos, result: ReconResult):
    """A position in INTENT or SUBMITTED has an unknown broker outcome -- this is
    the crash-after-ack / timeout-during-POST recovery path. Never guesses."""
    coid = pos["entry_client_order_id"]
    try:
        order = broker.get_order_by_client_id(coid)
    except BrokerTimeout as e:
        # Still ambiguous. Leave the row exactly as-is so a later cycle retries;
        # do NOT finalise it, and do NOT let entries proceed while blind.
        result.degraded = True
        result.halt_reasons.append(
            f"cannot resolve entry {coid} against broker ({e}) -- outcome unknown")
        return

    if order is None:
        # Broker affirmatively never saw it: the intent is safely dead.
        store.record_order_terminal(coid, CANCELED, "broker 404: order never reached the venue")
        store.set_position_state(pos["position_id"], CANCELED,
                                  "reconciled: broker never received the entry order")
        result.resolved_intents.append({"position_id": pos["position_id"], "outcome": "NEVER_LANDED"})
        return

    if order.broker_order_id:
        store.record_broker_ack(coid, order.broker_order_id,
                                 state=SUBMITTED if order.is_working else SUBMITTED)

    if order.is_working:
        # A restart must not reset the entry TTL. Conservatively cancel any
        # surviving entry immediately, then prove terminal before deciding flat
        # versus filled. Otherwise a day order can outlive the signal forever.
        if not order.broker_order_id:
            result.halt_reasons.append(
                f"working entry {coid} has no broker_order_id; cannot cancel safely")
            return
        cancel_error = None
        try:
            broker.cancel_order(order.broker_order_id)
        except BrokerTimeout as e:
            cancel_error = str(e)  # cancel may still have landed; lookup decides
        try:
            refreshed = broker.get_order_by_client_id(coid)
        except BrokerTimeout as e:
            result.halt_reasons.append(
                f"cannot confirm restart cancel for {coid} ({e}; cancel={cancel_error})")
            return
        if refreshed is None:
            result.halt_reasons.append(
                f"working entry {coid} vanished during cancel confirmation; outcome unknown")
            return
        order = refreshed
        if order.is_working:
            if order.filled_qty > 0 and order.avg_fill_price is not None:
                # Record real exposure, but leave the ENTRY order SUBMITTED so
                # the next reconciliation pass retries the unconfirmed cancel.
                fully = order.filled_qty >= pos["intended_qty"]
                store.record_entry_filled(pos["position_id"], order.filled_qty,
                                           float(order.avg_fill_price), fully)
            result.halt_reasons.append(
                f"entry {coid} remains working after cancel request; "
                "no replacement/entry allowed")
            return

    if order.is_filled or order.filled_qty > 0:
        if order.avg_fill_price is None or float(order.avg_fill_price) <= 0:
            detail = (f"recovered broker fill for {coid} has qty={order.filled_qty} "
                      "but no valid average fill price; refusing zero-dollar bookkeeping")
            store.set_position_state(pos["position_id"], RECON_MISMATCH, detail)
            result.halt_reasons.append(detail)
            result.adopted.append({"position_id": pos["position_id"], "occ": pos["occ"],
                                   "filled_qty": order.filled_qty,
                                   "note": "exposure found; economics incomplete"})
            return
        fully = order.filled_qty >= pos["intended_qty"]
        store.record_order_fill(coid, order.filled_qty, order.avg_fill_price,
                                 OPEN if fully else CANCELED)
        store.record_entry_filled(pos["position_id"], order.filled_qty,
                                   float(order.avg_fill_price), fully)
        result.adopted.append({
            "position_id": pos["position_id"], "occ": pos["occ"],
            "filled_qty": order.filled_qty, "note": "recovered a fill we had no local record of",
        })
        logger.error(
            "RECOVERED ORPHAN-IN-WAITING: %s (%s) was %s locally but is filled %d at the broker. "
            "Now under supervision.", pos["position_id"], pos["occ"], pos["state"], order.filled_qty)
        return

    if order.is_terminal_unfilled:
        store.record_order_terminal(coid, CANCELED, f"broker terminal unfilled: {order.status}")
        store.set_position_state(pos["position_id"], CANCELED,
                                  f"reconciled: broker status {order.status}")
        result.resolved_intents.append({"position_id": pos["position_id"], "outcome": order.status})
        return

    detail = f"entry {coid} has unknown nonterminal broker status={order.status!r}"
    result.halt_reasons.append(detail)
    result.resolved_intents.append(
        {"position_id": pos["position_id"], "outcome": "UNKNOWN_NONTERMINAL"})

def _resolve_unresolved_exit(store: SmcStateStore, broker: Broker, row,
                             result: ReconResult) -> None:
    """Cancel/resolve a pre-restart exit before any replacement is allowed."""
    coid = row["client_order_id"]
    position_id = row["position_id"]
    try:
        order = broker.get_order_by_client_id(coid)
    except BrokerTimeout as e:
        result.halt_reasons.append(
            f"cannot resolve prior exit {coid} against broker ({e})")
        return
    if order is None:
        store.record_order_terminal(coid, CANCELED, "broker 404: exit never landed")
        store.set_position_state(position_id, OPEN,
                                 "reconciled: prior exit never reached broker")
        return

    if order.is_working:
        if not order.broker_order_id:
            detail = f"working prior exit {coid} has no broker_order_id"
            store.set_position_state(position_id, RECON_MISMATCH, detail)
            result.halt_reasons.append(detail)
            return
        try:
            broker.cancel_order(order.broker_order_id)
        except BrokerTimeout:
            pass  # cancel may have landed; the lookup below is authoritative
        try:
            refreshed = broker.get_order_by_client_id(coid)
        except BrokerTimeout as e:
            detail = (f"cannot confirm cancel of prior exit {coid} ({e}); "
                      "replacement forbidden")
            store.set_position_state(position_id, RECON_MISMATCH, detail)
            result.halt_reasons.append(detail)
            return
        if refreshed is None:
            detail = f"prior exit {coid} vanished during cancel confirmation"
            store.set_position_state(position_id, RECON_MISMATCH, detail)
            result.halt_reasons.append(detail)
            return
        order = refreshed
    if order.is_working:
        if order.filled_qty > 0 and order.avg_fill_price is not None:
            store.record_order_fill(coid, order.filled_qty, float(order.avg_fill_price),
                                    EXIT_SUBMITTED)
        detail = (
            f"prior exit {coid} remains working after cancel request; "
            "replacement forbidden because both orders could fill"
        )
        store.set_position_state(position_id, RECON_MISMATCH, detail)
        result.halt_reasons.append(detail)
        return


    if order.filled_qty > 0:
        if order.avg_fill_price is None or float(order.avg_fill_price) <= 0:
            detail = (
                f"prior exit {coid} filled {order.filled_qty} but has no valid fill price")
            store.set_position_state(position_id, RECON_MISMATCH, detail)
            result.halt_reasons.append(detail)
            return
        terminal_state = CLOSED if order.is_filled else CANCELED
        store.record_order_fill(coid, order.filled_qty, float(order.avg_fill_price),
                                terminal_state)
        store.set_position_state(position_id, OPEN,
                                 "reconciled terminal prior exit; quantity check pending")
        return


    if order.is_terminal_unfilled:
        store.record_order_terminal(coid, CANCELED, f"broker status={order.status}")
        store.set_position_state(position_id, OPEN,
                                 "reconciled canceled prior exit; position remains open")
        return
    detail = f"prior exit {coid} has unknown broker status={order.status!r}"
    store.set_position_state(position_id, RECON_MISMATCH, detail)
    result.halt_reasons.append(detail)

def reconcile(store: SmcStateStore, broker: Broker, config, dashboard_db: Optional[Path] = None,
              ) -> ReconResult:
    """Full reconciliation. Safe to call on startup AND every management cycle."""
    result = ReconResult()
    dashboard_db = Path(dashboard_db) if dashboard_db else (config.db_path.parent.parent / "options_eval.db")

    # 1. Resolve anything whose broker fate we don't know.
    for pos in store.unresolved_intents():
        _resolve_unresolved_intent(store, broker, pos, result)
    for order_row in store.unresolved_exit_orders():
        _resolve_unresolved_exit(store, broker, order_row, result)


    # 2. Read broker truth.
    try:
        broker_positions = broker.list_option_positions()
    except BrokerTimeout as e:
        result.degraded = True
        reason = f"broker position read failed ({e}) -- cannot verify exposure"
        result.halt_reasons.append(reason)
        store.set_halt(reason)
        store.log_event("RECONCILE_FAILED_CLOSED", {"reason": reason})
        return result

    broker_by_occ = {}
    for p in broker_positions:
        occ = p.get("symbol")
        if occ:
            broker_by_occ[occ] = broker_by_occ.get(occ, 0) + int(float(p.get("qty") or 0))

    try:
        open_orders = broker.list_open_orders()
    except BrokerTimeout as e:
        result.degraded = True
        result.halt_reasons.append(
            f"broker open-order read failed ({e}) -- cannot rule out latent exposure")
        open_orders = []

    for broker_order in open_orders:
        coid = broker_order.client_order_id
        occ = broker_order.occ or ""
        local = store.get_order(coid) if coid else None
        suspicious = bool(coid and coid.startswith("smc-")) or any(
            occ.startswith(u) for u in SMC_UNDERLYINGS)
        if local is None and suspicious:
            result.orphan_orders.append({
                "client_order_id": coid, "occ": occ,
                "broker_order_id": broker_order.broker_order_id,
                "status": broker_order.status,
            })
            result.halt_reasons.append(
                f"UNATTRIBUTED working order {coid or '<no client id>'} on {occ}")
            continue
        if local is not None and local["state"] in (OPEN, CLOSED, CANCELED, REJECTED):
            detail = f"local order {coid} is {local['state']} but broker reports it working"
            store.set_position_state(local["position_id"], RECON_MISMATCH, detail)
            result.halt_reasons.append(detail)
    foreign, foreign_readable = foreign_occ_owners(dashboard_db)
    if not foreign_readable:
        result.degraded = True
        result.halt_reasons.append(
            f"cannot read foreign OCC ownership from {dashboard_db} -- unable to prove that "
            "unattributed broker exposure is not SMC's, so entries halt (supervision continues)")

    # 3. Every live SMC position: broker is authoritative on quantity.
    for pos in store.open_positions():
        occ, pid = pos["occ"], pos["position_id"]
        broker_qty = broker_by_occ.get(occ, 0)
        foreign_claims = foreign.get(occ, [])

        if foreign_claims:
            # Shared symbol: we cannot attribute any part of broker_qty to SMC.
            store.set_position_state(pid, RECON_MISMATCH,
                                      f"OCC {occ} also claimed by {sorted(set(foreign_claims))}")
            result.ambiguous.append({
                "position_id": pid, "occ": occ, "broker_qty": broker_qty,
                "foreign_strategies": sorted(set(foreign_claims)),
                "note": "ownership ambiguous -- refusing to assume the broker quantity is SMC's",
            })
            result.halt_reasons.append(
                f"OCC {occ} is claimed by both SMC ({pid}) and {sorted(set(foreign_claims))}")
            continue

        local_qty = int(pos["filled_qty"] or 0)
        if broker_qty != local_qty:
            if broker_qty == 0:
                # Broker flat but we think we're open: the position left without us.
                exit_orders = store.orders_for_position(pid, role="EXIT")
                durable_fills = [
                    (int(row["filled_qty"] or 0), float(row["avg_fill_price"]))
                    for row in exit_orders
                    if int(row["filled_qty"] or 0) > 0 and row["avg_fill_price"] is not None
                ]
                recovered_qty = sum(q for q, _ in durable_fills)
                if recovered_qty == local_qty and recovered_qty > 0:
                    avg_exit = sum(q * price for q, price in durable_fills) / recovered_qty
                    exit_reason = next(
                        (row["exit_reason"] for row in reversed(exit_orders) if row["exit_reason"]),
                        "RECONCILED_EXIT")
                    realized = round((avg_exit - float(pos["entry_fill_price"])) * recovered_qty * 100, 2)
                    store.record_position_closed(
                        pid, exit_fill_price=round(avg_exit, 4), closed_qty=recovered_qty,
                        realized_pnl=realized, exit_reason=exit_reason)
                    result.qty_corrections.append({
                        "position_id": pid, "occ": occ, "local": local_qty,
                        "broker": 0, "action": "recovered durable exit fill and closed state",
                    })
                    continue
                store.set_position_state(pid, RECON_MISMATCH,
                                          f"local filled_qty={local_qty} but broker is FLAT on {occ}")
                result.qty_corrections.append({"position_id": pid, "occ": occ, "local": local_qty,
                                                "broker": 0, "action": "flagged RECON_MISMATCH"})
                result.halt_reasons.append(
                    f"{pid}: local believes {local_qty} open on {occ}, broker says 0")
            else:
                store.set_reconciled_qty(pid, broker_qty,
                                          f"broker authoritative: {local_qty}->{broker_qty}")
                result.qty_corrections.append({"position_id": pid, "occ": occ, "local": local_qty,
                                                "broker": broker_qty, "action": "adopted broker qty"})
                logger.warning("%s: quantity corrected from local %d to broker %d on %s",
                               pid, local_qty, broker_qty, occ)

    # 4. Unattributed broker exposure on an underlying SMC trades = candidate orphan.
    smc_claimed = set()
    for pos in store.live_positions():
        smc_claimed.add(pos["occ"])

    for occ, qty in broker_by_occ.items():
        if qty == 0 or occ in smc_claimed or occ in foreign:
            continue
        if not any(occ.startswith(u) for u in SMC_UNDERLYINGS):
            continue  # another strategy's symbol -- explicitly not our business
        result.orphans.append({
            "occ": occ, "broker_qty": qty,
            "note": ("broker exposure on an SMC-tradeable underlying with NO SMC state record "
                     "and no foreign claim -- unsupervised"),
        })
        result.halt_reasons.append(
            f"UNATTRIBUTED {occ} qty={qty} at broker with no recoverable SMC state record")
        logger.error("ORPHAN DETECTED: %s qty=%s has no SMC state record and no foreign owner. "
                     "Halting entries. Manual attribution required.", occ, qty)

    # 5. Any pre-existing mismatch keeps entries halted until explicitly cleared.
    for pos in store.positions_in_mismatch():
        result.halt_reasons.append(
            f"{pos['position_id']} is in RECON_MISMATCH ({pos['recon_note']})")

    if result.halt_reasons:
        store.set_halt("; ".join(sorted(set(result.halt_reasons))[:5]))

    store.log_event("RECONCILE", {
        "adopted": len(result.adopted), "qty_corrections": len(result.qty_corrections),
        "orphans": len(result.orphans), "orphan_orders": len(result.orphan_orders),
        "ambiguous": len(result.ambiguous),
        "degraded": result.degraded, "halts": len(result.halt_reasons),
    })
    return result


def assert_occ_free_for_entry(store: SmcStateStore, broker: Broker, occ: str,
                              dashboard_db: Path) -> tuple:
    """Pre-entry ownership gate: (allowed, reason). Blocks an entry when the exact
    OCC is already held by SMC (the 2026-07-31 same-contract-overwrite bug), by
    another strategy, or by an unattributable broker position."""
    existing = store.occ_owned_by_smc(occ)
    if existing:
        return False, (f"SMC already has a non-terminal position on {occ} "
                       f"({existing[0]['position_id']}, state={existing[0]['state']})")

    foreign, readable = foreign_occ_owners(dashboard_db)
    if not readable:
        return False, (f"cannot verify foreign ownership of {occ} (dashboard ledger unreadable) "
                       "-- refusing to enter blind")
    if occ in foreign:
        return False, f"{occ} is owned by another strategy: {sorted(set(foreign[occ]))}"

    try:
        broker_qty = broker.get_position_qty(occ)
    except BrokerTimeout as e:
        return False, f"cannot confirm broker is flat on {occ} ({e}) -- refusing to enter blind"
    if broker_qty != 0:
        return False, (f"broker already shows qty={broker_qty} on {occ} with no SMC or foreign "
                       "record -- unattributed, refusing to add exposure")
    return True, "ok"
