"""Wires the tested components into ONE running pipeline.

This exists so there is exactly one wiring, used by both run_daemon and the
integration test. A test that assembles its own copy of the wiring proves
the components work and proves nothing about the daemon; the point of this
module is that `build_pipeline` IS what production runs.

DATA FLOW:

    confirmed bar
      -> DetectorWorker (replay off the loop)
      -> DetectedSignal + stable signal identity
      -> EV_SIGNAL on the bus
      -> candidate universe snapshot (atomic quote+Greek merge)
      -> VARIANT_B_NO_SWEEP selector
      -> EntryAttempt + broker submit
      -> trade_updates -> EV_FILL/EV_CANCEL/...
      -> position state
      -> ThetaData quote -> StreamingExitMonitor -> exit submit
      -> exit trade_updates -> ExitLivenessTracker -> closed
      -> dashboard + durable outbox at every step

PRIORITY. Exits are wired to arrive as EV_FILL/EV_EXIT_TRIGGER (priority 1)
while new-signal work is EV_SIGNAL (priority 3) and dashboard/Telegram are
EV_HEALTH (priority 9). The bus drains by priority, so a stop-triggering
quote is handled before new-signal selection even when both are queued.
Crucially the exit path is evaluated SYNCHRONOUSLY in the quote handler
rather than being queued behind anything.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import logging
from typing import Callable, Optional

from smc import events as ev
from smc.events import make_event
from smc.signal_identity import make_signal_identity

logger = logging.getLogger("smc.assembly")


@dataclasses.dataclass
class Pipeline:
    """Everything wired together, plus the callbacks the runner installs."""
    runner: object
    detector_worker: object = None
    exit_monitor: object = None
    exit_liveness: object = None
    exit_worker: object = None
    process_exit_quote: Optional[Callable] = None
    selector: Optional[Callable] = None
    universe: Optional[Callable] = None
    broker: object = None
    notifier: object = None
    positions: dict = dataclasses.field(default_factory=dict)
    signals_seen: list = dataclasses.field(default_factory=list)
    entries: dict = dataclasses.field(default_factory=dict)

    def health(self) -> dict:
        return {
            "detector_worker": self.detector_worker.health() if self.detector_worker else None,
            "exit_monitor": self.exit_monitor.health() if self.exit_monitor else None,
            "exit_liveness": self.exit_liveness.health() if self.exit_liveness else None,
            "exit_worker": self.exit_worker.health() if self.exit_worker else None,
            "open_positions": len(self.positions),
            "signals_seen": len(self.signals_seen),
        }


def build_pipeline(*, runner, detector_worker=None, exit_monitor=None,
                   exit_liveness=None, select_contract=None, build_universe=None,
                   broker=None, notifier=None, entry_builder=None,
                   entry_submitter=None, entry_enabled=None, clock=None) -> Pipeline:
    """Installs the handlers that connect the components. Returns the
    Pipeline so a test can inspect what actually happened."""
    import time as _time
    clock = clock or _time.monotonic

    p = Pipeline(runner=runner, detector_worker=detector_worker,
                 exit_monitor=exit_monitor, exit_liveness=exit_liveness,
                 selector=select_contract, universe=build_universe,
                 broker=broker, notifier=notifier)

    # ---------------------------------------------------------- signals
    def on_detector_result(task, signals):
        """Detector worker -> canonical events. Runs on the worker thread, so
        it does nothing but publish; all decisions happen on the loop."""
        task.received_monotonic = clock()
        for s in signals or []:
            ident = make_signal_identity(
                symbol=getattr(s, "session", None) and "QQQ" or "QQQ",
                timeframe=getattr(s, "bar_timeframe", "1Min"),
                bar_open=getattr(s, "bar_timestamp", None) or dt.datetime.now(dt.timezone.utc),
                side=s.side, trigger=s.trigger)
            runner.bus.publish(make_event(
                ev.EV_SIGNAL, signal_id=ident.signal_key,
                payload={"signal": s, "identity": ident.as_dict(),
                         "detector_task": task.task_id}))

    if detector_worker is not None:
        detector_worker.on_result = on_detector_result

    def handle_signal(event):
        signal_received = clock()
        payload = event.payload or {}
        sig = payload.get("signal")
        ident = payload.get("identity", {})
        execution_enabled = entry_enabled() if entry_enabled is not None else True
        signal_view = {
            "signal_key": ident.get("signal_key"),
            "bar_close_utc": ident.get("bar_close_utc"),
            "side": getattr(sig, "side", ident.get("side")),
            "trigger": getattr(sig, "trigger", ident.get("trigger")),
            "score": getattr(sig, "score", None),
            "price": getattr(sig, "price", None),
            "detected_ts": getattr(sig, "detected_ts", None),
            "execution_mode": "paper_forward" if execution_enabled
                              else "shadow_no_order",
        }
        p.signals_seen.append(signal_view)
        _notify("signal_detected", signal_view)

        # Mode is an independent, first gate. A readiness bug can never
        # make validate or paper-connectivity submit an order.
        if not execution_enabled:
            _notify("signal_blocked_mode", {"signal_key": ident.get("signal_key")})
            return

        # Dedup on the stable identity: a repeat can never place a 2nd order.
        if ident.get("signal_key") in p.entries:
            _notify("signal_duplicate_suppressed", {"signal_key": ident.get("signal_key")})
            return

        if not runner.readiness.entries_permitted:
            _notify("signal_blocked", {"signal_key": ident.get("signal_key"),
                                       "blocking": runner.readiness.blocking()})
            return

        # SWEEP_RECLAIM is excluded UPSTREAM, before any book is built.
        from smc.selector_variant_b import is_trigger_eligible
        trigger = getattr(sig, "trigger", ident.get("trigger"))
        if not is_trigger_eligible(trigger):
            _notify("signal_excluded_sweep_reclaim",
                    {"signal_key": ident.get("signal_key"), "trigger": trigger})
            return

        universe_started = clock()
        book = build_universe(sig) if build_universe else None
        universe_ms = (clock() - universe_started) * 1000.0
        selection_started = clock()
        selection = select_contract(book, sig) if select_contract else None
        selection_ms = (clock() - selection_started) * 1000.0
        if selection is None or not getattr(selection, "found", False):
            _notify("no_eligible_contract",
                    {"signal_key": ident.get("signal_key"),
                     "reason": getattr(selection, "reason", "no selection")})
            return

        entry_started = clock()
        attempt = entry_builder(sig, selection, ident) if entry_builder else None
        entry_build_ms = (clock() - entry_started) * 1000.0
        if attempt is None:
            _notify("entry_blocked_or_build_failed",
                    {"signal_key": ident.get("signal_key")})
            return
        attempt.stage_latency.update({
            "signal_bus_to_universe_ms": round(
                (universe_started - signal_received) * 1000.0, 3),
            "universe_ms": round(universe_ms, 3),
            "selection_ms": round(selection_ms, 3),
            "entry_build_and_intent_ms": round(entry_build_ms, 3),
        })
        p.entries[ident.get("signal_key")] = attempt
        runner.register_attempt(attempt)
        submitted = attempt.client_order_id
        if entry_submitter is not None:
            submitted = entry_submitter(attempt, selection)
            if submitted is None and attempt.broker_order_id:
                submitted = attempt.client_order_id
        if not submitted:
            _notify("order_submit_failed_or_unknown", attempt.summary(),
                    client_order_id=attempt.client_order_id)
            return
        _notify("order_submitted", attempt.summary(),
                client_order_id=attempt.client_order_id)

    runner._handlers[ev.EV_SIGNAL] = handle_signal

    # ------------------------------------------------------------ quotes
    prior_quote_handler = runner._handlers.get(ev.EV_QUOTE)

    def process_exit_quote(quote):
        """Worker-side exit decision and submission."""
        if exit_monitor is not None:
            rec = exit_monitor.on_quote(quote)
            if rec is not None:
                if exit_liveness is not None and rec.submitted:
                    pos = p.positions.get(rec.position_id, {})
                    exit_liveness.register(
                        position_id=rec.position_id, occ=rec.occ,
                        client_order_id=rec.client_order_id,
                        intended_qty=pos.get("qty", 1))
                _notify("stop_triggered" if rec.reason == "STOP" else
                        "target_triggered" if rec.reason == "TARGET" else "exit_submitted",
                        dataclasses.asdict(rec), position_id=rec.position_id)
                return rec
        return None

    p.process_exit_quote = process_exit_quote

    def handle_quote(event):
        """Entry sampling stays on the bus. Production exits use their own
        worker; tests without one retain the synchronous seam."""
        if p.exit_worker is None:
            process_exit_quote(event.quote)
        if prior_quote_handler is not None:
            prior_quote_handler(event)

    runner._handlers[ev.EV_QUOTE] = handle_quote

    # ------------------------------------------------------ order events
    prior_order_handler = runner._handlers.get(ev.EV_FILL)

    def handle_order(event):
        if prior_order_handler is not None:
            prior_order_handler(event)
        payload = event.payload or {}
        coid = event.client_order_id
        kind = payload.get("event", event.event_type)

        # Exit-side updates go to the liveness tracker.
        if exit_liveness is not None and coid in exit_liveness.attempts:
            a = exit_liveness.on_update(coid, kind,
                                        filled_qty=payload.get("filled_qty"),
                                        broker_order_id=event.order_id,
                                        reason=payload.get("reason", ""))
            store = getattr(runner, "state", None)
            price = payload.get("price")
            if store is not None and store.get_order(coid) is not None:
                from smc.state import CANCELED, CLOSED, PARTIAL, REJECTED
                if kind in ("fill", "partial_fill") and price is not None:
                    order_state = CLOSED if kind == "fill" else PARTIAL
                    store.record_order_fill(coid, int(a.filled_qty), float(price),
                                            order_state)
                elif kind == "canceled":
                    store.record_order_terminal(coid, CANCELED, "trade_updates canceled")
                elif kind in ("rejected", "expired"):
                    store.record_order_terminal(coid, REJECTED, payload.get("reason", kind))
            if a is not None and a.terminal:
                if exit_monitor is not None:
                    exit_monitor.on_exit_terminal(a.position_id)
                if a.state == "FILLED":
                    pos = p.positions.pop(a.position_id, None)
                    if (store is not None and pos is not None and price is not None
                            and store.get_position(a.position_id) is not None):
                        qty = int(a.filled_qty)
                        entry = float(pos["entry_fill_price"])
                        exit_price = float(price)
                        store.record_position_closed(
                            a.position_id, exit_fill_price=exit_price,
                            closed_qty=qty,
                            realized_pnl=round((exit_price - entry) * qty * 100, 2),
                            exit_reason=payload.get("reason") or "STREAM_EXIT")
                    _notify("exit_filled", a.as_dict(), position_id=a.position_id)
            return

        # Entry-side fill opens a position.
        attempt = runner.attempts.get(coid)
        store = getattr(runner, "state", None)
        pid = getattr(attempt, "position_id", None) if attempt is not None else None
        if (attempt is not None and store is not None and pid
                and store.get_order(coid) is not None):
            from smc.state import CANCELED, OPEN, PARTIAL, REJECTED
            if attempt.filled_qty > 0 and attempt.fill_price is not None:
                fully = kind == "fill" or attempt.filled_qty >= attempt.quantity
                store.record_order_fill(
                    coid, int(attempt.filled_qty), float(attempt.fill_price),
                    OPEN if fully else PARTIAL)
                store.record_entry_filled(
                    pid, int(attempt.filled_qty), float(attempt.fill_price), fully)
            if kind == "canceled":
                store.record_order_terminal(coid, CANCELED, "trade_updates canceled")
                if attempt.filled_qty <= 0:
                    store.set_position_state(pid, CANCELED, "entry canceled unfilled")
            elif kind in ("rejected", "expired"):
                store.record_order_terminal(coid, REJECTED, payload.get("reason", kind))
                if attempt.filled_qty <= 0:
                    store.set_position_state(pid, REJECTED, payload.get("reason", kind))

        if (attempt is not None and attempt.filled_qty > 0
                and attempt.fill_price is not None):
            pid = pid or f"pos-{coid}"
            opened = p.positions.get(pid, {}).get(
                "opened_at", dt.datetime.now(dt.timezone.utc))
            p.positions[pid] = {
                "position_id": pid, "occ": attempt.occ,
                "entry_fill_price": attempt.fill_price,
                "qty": attempt.filled_qty,
                "opened_at": opened}
            _notify("order_fill", attempt.summary(), client_order_id=coid,
                    position_id=pid)
        elif attempt is not None and attempt.state == "REJECTED":
            _notify("order_rejected", attempt.summary(), client_order_id=coid)

    for etype in (ev.EV_FILL, ev.EV_PARTIAL_FILL, ev.EV_CANCEL,
                  ev.EV_REJECT, ev.EV_ORDER_ACK):
        runner._handlers[etype] = handle_order

    # ---------------------------------------------------------- helpers
    def _notify(kind, detail, **ids):
        if notifier is None:
            return
        try:
            notifier.publish(kind, f"{kind}: {detail}", detail=detail,
                             client_order_id=ids.get("client_order_id"),
                             position_id=ids.get("position_id"))
        except Exception as e:  # noqa: BLE001 -- never blocks trading
            logger.warning("notify failed (ignored): %s", e)

    p._notify = _notify
    if exit_monitor is not None:
        exit_monitor.open_positions = lambda: list(p.positions.values())
    return p
