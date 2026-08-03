"""Presents the daemon's FastPaperBroker as the `Broker` protocol.

WHY THIS EXISTS. `smc/reconcile.py` holds the real reconciliation -- unresolved
intents, orphan detection, foreign-ownership disambiguation, broker-authoritative
quantity correction. It speaks the `smc.broker.Broker` protocol: parsed
`BrokerOrder` objects, and `BrokerTimeout` raised whenever an answer is
ambiguous. The daemon, built for latency, uses `FastPaperBroker`, which returns
raw `BrokerCall` envelopes and never raises on a bad status.

The two never met. `PaperRunner.reconcile()` called `open_orders()` and
`positions()`, checked only that both came back `ok`, and returned True --
so the `reconciled` readiness gate went green on "the broker answered the
phone", while the module that actually reconciles was reachable only from
`smc/pipeline.py`, which the daemon does not use. A restart holding a position
therefore passed its reconciliation gate having adopted nothing.

THE ONE RULE HERE: ambiguity raises. FastPaperBroker reports a failed read as
`ok=False` and moves on, which for reconciliation would read as "no positions"
-- indistinguishable from a genuinely flat account and the exact way an open
position becomes invisible. Every read below converts a non-OK response into
`BrokerTimeout`, because "I could not find out" must never be able to look
like "there is nothing there".

This adapter is READ-ONLY plus cancel. Order submission stays on the fast path;
nothing here is on the hot path at all.
"""
from __future__ import annotations

import logging
from typing import Optional

from smc.broker import BrokerOrder, BrokerTimeout, _parse_order

logger = logging.getLogger("smc.broker_adapter")

OPTION_ASSET_CLASS = "us_option"


class ReconcileBrokerAdapter:
    """Wraps a FastPaperBroker for reconciliation reads."""

    def __init__(self, fast_broker):
        self._b = fast_broker

    # ------------------------------------------------------------- positions
    def list_option_positions(self) -> list:
        call = self._b.positions()
        if not getattr(call, "ok", False):
            raise BrokerTimeout(
                f"position read failed: status={getattr(call, 'status', None)} "
                f"{getattr(call, 'error', None)} -- cannot distinguish 'flat' "
                "from 'could not read'")
        body = call.body
        if body is None:
            return []
        if not isinstance(body, list):
            raise BrokerTimeout(f"position read returned {type(body).__name__}, expected a list")
        # Alpaca omits asset_class on some historical rows; an option OCC is 21
        # chars and never a bare equity ticker, so fall back to shape rather
        # than dropping a position we cannot classify.
        return [p for p in body
                if p.get("asset_class") == OPTION_ASSET_CLASS
                or len(str(p.get("symbol") or "")) >= 15]

    def get_position_qty(self, occ: str) -> int:
        """Delegated: FastPaperBroker already raises BrokerTimeout here and
        already treats a 404 as a real zero."""
        return self._b.get_position_qty(occ)

    # ---------------------------------------------------------------- orders
    def list_open_orders(self) -> list:
        call = self._b.open_orders()
        if not getattr(call, "ok", False):
            raise BrokerTimeout(
                f"open-order read failed: status={getattr(call, 'status', None)} "
                f"{getattr(call, 'error', None)} -- cannot rule out latent exposure")
        body = call.body
        if body is None:
            return []
        if not isinstance(body, list):
            raise BrokerTimeout(f"open-order read returned {type(body).__name__}, expected a list")
        return [_parse_order(o) for o in body]

    def get_order_by_client_id(self, client_order_id: str) -> Optional[BrokerOrder]:
        """Returns None ONLY on an affirmative 404. Any other non-OK status is
        ambiguous and raises, so a crash-recovery path can never conclude that
        an order it cannot see does not exist."""
        call = self._b.get_order_by_client_id(client_order_id)
        if getattr(call, "status", None) == 404:
            return None
        if not getattr(call, "ok", False):
            raise BrokerTimeout(
                f"ambiguous lookup for {client_order_id}: "
                f"status={getattr(call, 'status', None)} {getattr(call, 'error', None)} "
                "-- refusing to conclude the order does not exist")
        body = call.body
        if not isinstance(body, dict):
            raise BrokerTimeout(
                f"lookup for {client_order_id} returned {type(body).__name__}, expected an object")
        return _parse_order(body)

    def cancel_order(self, broker_order_id: str) -> None:
        call = self._b.cancel_order(broker_order_id)
        status = getattr(call, "status", None)
        # 404 = already gone, 422 = already terminal. Both mean "not working".
        if status in (200, 204, 404, 422):
            return
        raise BrokerTimeout(f"cancel returned {status} for {broker_order_id}")
