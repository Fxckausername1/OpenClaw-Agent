"""The canonical SMC PAPER daemon: startup sequence, readiness gating and
the event loop.

Replaces the four cron stages (detector / selector / executor / exit
manager) with one persistent process. The cron chain cost ~6-7 minutes of
pure scheduler dwell between a confirmed bar and an order; a single process
holding warm state removes that entirely.

STARTUP IS AN ORDERED GATE, not a best-effort sequence. Each step must
succeed before the next is attempted, and `entries_permitted` is False until
every one of them is green:

     1 singleton acquired            6 Alpaca PAPER transport prewarmed
     2 SQLite + outbox recovered     7 trade_updates connected
     3 Theta Terminal running        8 orders/positions reconciled
     4 ThetaData stream healthy      9 detector synchronized
     5 universe/Greeks warm         10 entries permitted

DEGRADED IS NOT STOPPED. If a prerequisite fails or later goes red, the
daemon stops accepting NEW entries but keeps managing and reconciling an
existing position wherever it still can -- an open position with a
degraded feed still needs its stop watched, and shutting the whole process
down would abandon it. The degraded state is published to the dashboard and
Telegram rather than merely logged.

Two gates are deliberately RED-BY-DEFAULT because their components are not
built yet: the persistent minute detector (step 9) and stream-driven exits.
They block entries rather than silently passing, so this daemon cannot start
trading on a half-built path. That is the honest failure mode; a green gate
over a missing component would not be.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import logging
import threading
import time
from typing import Callable, Optional

from smc import events as ev
from smc import lifecycle_events as lc
from smc.events import Event, EventBus, make_event
from smc.readiness import (
    GATES, LIVE_DATA_GATES, MARKET_CLOSED, NOT_TESTABLE, PASS, RED, Readiness,
)
from smc.singleton import AlreadyRunning, SingleInstance

logger = logging.getLogger("smc.runner")


def ev_kind_reconciliation_result(ok: bool) -> str:
    """Stage 12. A clean pass and a mismatch are both reportable outcomes --
    previously only the exception path published anything, so a reconciliation
    that ran and found an orphan was recorded in SQLite and announced nowhere."""
    return lc.RECONCILIATION_RESULT if ok else lc.RECONCILIATION_MISMATCH



def _safe_health(component):
    """Telemetry must never raise. A component that cannot report its own
    health reports an error string instead of taking the daemon down."""
    if component is None:
        return None
    try:
        return component.health()
    except Exception as e:  # noqa: BLE001
        return {"error": repr(e)}


def _is_weekend(day: str) -> bool:
    """A Saturday/Sunday "expiration" is a probing artefact, never a listed
    contract date -- it must not be able to make the universe green."""
    import datetime as _dt
    try:
        return _dt.date.fromisoformat(str(day)[:10]).weekday() >= 5
    except ValueError:
        return False

RECONCILE_INTERVAL_SECONDS = 30.0
HEALTH_INTERVAL_SECONDS = 60.0

# Fraction of the desired universe that must be subscribed for the stream gate
# to pass. Not 100%: the candidate universe re-centres on spot, so a routine
# refresh briefly runs below full coverage. Well above any startup race, which
# sits near zero. Held contracts are exempt from this tolerance entirely.
MIN_SUBSCRIPTION_COVERAGE = 0.90


class PaperRunner:
    """Assembles the components. Every collaborator is injected so the whole
    daemon is testable without a Terminal, a socket or a broker."""

    def __init__(self, *, bus: Optional[EventBus] = None,
                 singleton: Optional[SingleInstance] = None,
                 state_store=None, outbox=None, notifier=None,
                 theta_stream=None, greek_cache=None, broker=None,
                 trade_updates=None, detector=None,
                 terminal_check: Optional[Callable] = None,
                 dashboard_sync: Optional[Callable] = None,
                 full_reconcile: Optional[Callable] = None,
                 rebuild_positions: Optional[Callable] = None,
                 clock=time.monotonic):
        self.bus = bus or EventBus()
        self.singleton = singleton
        self.state = state_store
        self.outbox = outbox
        self.notifier = notifier
        self.theta_stream = theta_stream
        self.greek_cache = greek_cache
        self.broker = broker
        self.trade_updates = trade_updates
        self.detector = detector
        self.terminal_check = terminal_check
        self.dashboard_sync = dashboard_sync
        # Injected so the daemon and the tests exercise the SAME reconciliation.
        # `full_reconcile` runs smc/reconcile.py against broker truth;
        # `rebuild_positions` rehydrates supervision from durable state. Both
        # optional only so a unit test can build a bare runner.
        self.full_reconcile = full_reconcile
        self.rebuild_positions = rebuild_positions
        self.last_recon = None
        # Suppression state for "announce the change, not the condition".
        self._last_recon_ok = None
        self._last_degraded_blocking = None
        self.clock = clock

        self.readiness = Readiness()
        # Live-data evidence, set by the daemon as real messages arrive. None
        # means "never verified", which is NOT the same as "failed".
        self.quote_verification = None    # quote_verifier.VerificationResult
        self.fresh_quote_count = 0
        self.candidate_ready_count = 0
        self.min_candidates = 1
        self.max_greek_age_seconds = 300.0
        # How long a critical lifecycle event may sit undelivered before new
        # entries are blocked. Generous enough to ride out a Telegram blip,
        # short enough that a whole session cannot run unannounced.
        self.max_notification_backlog_seconds = 180.0
        self._readiness_lock = threading.RLock()
        self._live_generation = None
        self._fresh_quote_times = {}      # occ -> receipt monotonic
        self._candidate_ready_times = {}  # occ -> receipt monotonic
        self.last_quote_occ = None
        self.offline = False
        self.market_closed = False
        self.universe_rows = 0
        self.attempts: dict = {}          # client_order_id -> EntryAttempt
        # Set by assembly.build_pipeline. When true, the pipeline owns the
        # lifecycle announcements and this runner suppresses its own
        # duplicates of them.
        self.pipeline_attached = False
        self._stop = threading.Event()
        self._handlers = {
            ev.EV_QUOTE: self._on_quote,
            ev.EV_ORDER_ACK: self._on_order_event,
            ev.EV_PARTIAL_FILL: self._on_order_event,
            ev.EV_FILL: self._on_order_event,
            ev.EV_CANCEL: self._on_order_event,
            ev.EV_REJECT: self._on_order_event,
            ev.EV_SIGNAL: self._on_signal,
            ev.EV_EXIT_TRIGGER: self._on_exit_trigger,
            ev.EV_RECONCILE: self._on_reconcile,
            ev.EV_RECONNECT: self._on_reconnect,
            ev.EV_DEADLINE: self._on_deadline,
            ev.EV_HEALTH: self._on_health,
            ev.EV_SHUTDOWN: self._on_shutdown,
        }
        self.handler_errors = 0

    # ------------------------------------------------------------- startup
    def start(self, *, offline: bool = False,
              market_closed: bool = False) -> Readiness:
        """`offline` and `market_closed` are passed in rather than sniffed,
        so the gate statuses reflect what the caller KNOWS about the run
        instead of each gate guessing independently."""
        self.offline = offline
        self.market_closed = market_closed
        return self._start_gates()

    def _start_gates(self) -> Readiness:
        """Runs the ordered gate sequence. Returns readiness rather than
        raising on a red gate: a degraded start is a real, reportable
        operating state, not a crash."""
        self._gate_singleton()
        self._gate_state()
        self._gate_notifications()
        self._gate_terminal()
        self._gate_stream()
        self._gate_quote_parser()
        self._gate_live_quotes()
        self._gate_universe()
        self._gate_candidate_universe()
        self._gate_broker()
        self._gate_trade_updates()
        self._gate_reconcile()
        self._gate_detector()
        self._schedule_periodics()
        logger.info("startup complete: %s", self.readiness.as_dict())
        self._publish_state("startup")
        return self.readiness

    def observe_live_quote(self, quote) -> None:
        """Turn a real current-generation stream message into gate evidence.

        Called on the ThetaData reader thread after the quote is cached.  It
        performs only bounded in-memory checks; no REST, broker, SQLite,
        dashboard or notification work is allowed here.
        """
        if quote is None or self.theta_stream is None:
            return
        from smc.quote_verifier import verify_live_message

        now_m = self.clock()
        gen = self.theta_stream.current_generation()
        result = verify_live_message(
            quote, expected_occ=getattr(quote, "occ", None),
            current_generation=gen,
            max_age_seconds=float(getattr(self, "max_quote_age_seconds", 10.0)))
        occ = getattr(quote, "occ", None)
        with self._readiness_lock:
            if gen != self._live_generation:
                self._live_generation = gen
                self._fresh_quote_times.clear()
                self._candidate_ready_times.clear()
            self.quote_verification = result
            self.last_quote_occ = occ
            if result.verified and occ:
                self._fresh_quote_times[occ] = now_m
                greek_ok = False
                if self.greek_cache is not None:
                    try:
                        snap = self.greek_cache.greek_snapshot([occ])
                        row = (snap.get("deltas") or {}).get(occ) or {}
                        src = row.get("source_monotonic")
                        greek_ok = (row.get("delta") is not None and src is not None
                                    and now_m - float(src)
                                    <= self.max_greek_age_seconds)
                    except Exception:  # noqa: BLE001 -- gate remains closed
                        greek_ok = False
                if greek_ok:
                    self._candidate_ready_times[occ] = now_m
            ceiling = float(getattr(self, "max_quote_age_seconds", 10.0))
            self._fresh_quote_times = {
                k: v for k, v in self._fresh_quote_times.items()
                if now_m - v <= ceiling}
            self._candidate_ready_times = {
                k: v for k, v in self._candidate_ready_times.items()
                if now_m - v <= ceiling}
            self.fresh_quote_count = len(self._fresh_quote_times)
            self.candidate_ready_count = len(self._candidate_ready_times)
            # A real option quote is direct evidence that the market is open.
            self.market_closed = False
            self._gate_stream()
            self._gate_quote_parser()
            self._gate_live_quotes()
            self._gate_universe()
            self._gate_candidate_universe()

    def _gate_singleton(self) -> None:
        if self.singleton is None:
            self.readiness.set("singleton", PASS)
            return
        try:
            self.singleton.acquire()
            self.readiness.set("singleton", PASS)
        except AlreadyRunning as e:
            self.readiness.set("singleton", RED, str(e))
            raise

    def _gate_state(self) -> None:
        try:
            if self.state is not None:
                self.state.verify_integrity()
            ok = self.state is not None and self.outbox is not None
            self.readiness.set_bool("state_recovered", ok,
                                    "" if ok else "state store or outbox missing")
        except Exception as e:  # noqa: BLE001
            self.readiness.set("state_recovered", RED, repr(e))

    def _gate_notifications(self) -> None:
        """Can the lifecycle actually be announced right now?

        Blocks NEW entries only. An open position keeps being managed and the
        daemon keeps running -- the failure mode this prevents is trading
        into silence, not trading at all. Self-heals: the periodic health tick
        re-runs this, so a breaker that closes after its cooldown or a worker
        that comes back reopens the gate with no manual action.
        """
        n = self.notifier
        if n is None:
            self.readiness.set("notifications_operational", RED,
                               "no notifier: lifecycle events cannot be delivered")
            return
        probe = getattr(n, "operational", None)
        if not callable(probe):
            # Fail closed rather than assume. A notifier that cannot report
            # its own delivery health cannot be trusted to have delivered
            # anything, and "no evidence" must not read as "healthy".
            self.readiness.set(
                "notifications_operational", RED,
                f"{type(n).__name__} does not report delivery health")
            return
        try:
            ok, reason, evidence = probe(
                max_backlog_seconds=self.max_notification_backlog_seconds)
        except Exception as e:  # noqa: BLE001 -- a probe must never kill startup
            self.readiness.set("notifications_operational", RED,
                               f"notification probe failed: {e!r}")
            return
        self.readiness.set_bool("notifications_operational", ok, reason,
                                evidence=evidence)

    def _gate_terminal(self) -> None:
        """MDDS+FPSS login only. Says nothing about the WebSocket."""
        if self.offline:
            self.readiness.set("theta_terminal_authenticated", NOT_TESTABLE,
                               "offline mode")
            return
        try:
            ok = bool(self.terminal_check()) if self.terminal_check else False
            self.readiness.set_bool("theta_terminal_authenticated", ok,
                                    "" if ok else "Theta Terminal not authenticated")
        except Exception as e:  # noqa: BLE001
            self.readiness.set("theta_terminal_authenticated", RED, repr(e))

    def _gate_stream(self) -> None:
        """Socket connected AND subscriptions acked. Still says nothing about
        any message having arrived."""
        if self.offline:
            self.readiness.set("theta_stream_connected", NOT_TESTABLE, "offline mode")
            return
        s_ = self.theta_stream
        if s_ is None:
            self.readiness.set("theta_stream_connected", RED, "no stream client")
            return
        try:
            h = s_.health() if hasattr(s_, "health") else {}
            connected = bool(s_.is_connected())
            n_sub = int(h.get("n_subscribed", 0) or 0)
            n_desired = int(h.get("n_desired", 0) or 0)
            pinned_missing = list(h.get("pinned_unsubscribed") or [])
        except Exception as e:  # noqa: BLE001 -- a broken dependency makes the
            # gate RED; it must never take startup down, because the daemon
            # still has to manage any already-open position.
            self.readiness.set("theta_stream_connected", RED, f"stream probe failed: {e!r}")
            return
        # Demanding 100% ack was right for the startup race this gate was
        # built for (0/826 -> 826/826) and wrong in steady state: the
        # candidate universe re-centres on spot as price moves, so a routine
        # refresh transiently sits below full coverage and was flipping
        # entries off mid-session for no real reason (observed live:
        # 300/300 -> 191/300 with the feed perfectly healthy).
        #
        # Two claims replace it, and neither weakens the race protection:
        #   * broad coverage -- a startup race sits near 0%, nowhere near this
        #   * every HELD contract subscribed, with no tolerance at all, since
        #     an open position we cannot price is the thing that actually hurts
        coverage = 1.0 if n_desired == 0 else n_sub / float(n_desired)
        ok = connected and coverage >= MIN_SUBSCRIPTION_COVERAGE and not pinned_missing
        if ok:
            reason = ""
        elif pinned_missing:
            reason = f"held contract(s) unsubscribed: {pinned_missing[:3]}"
        else:
            reason = (f"connected={connected} subscribed={n_sub}/{n_desired} "
                      f"({coverage:.0%} < {MIN_SUBSCRIPTION_COVERAGE:.0%} required)")
        self.readiness.set_bool(
            "theta_stream_connected", ok, reason,
            evidence={"connected": connected, "n_subscribed": n_sub,
                      "n_desired": n_desired, "coverage": round(coverage, 4),
                      "pinned_unsubscribed": pinned_missing,
                      "generation": h.get("generation")})

    def _gate_quote_parser(self) -> None:
        """Can ONLY be PASS from a real live message that passed all eleven
        checks. Synthetic data can never satisfy it."""
        if self.offline:
            self.readiness.set("theta_quote_parser_verified", NOT_TESTABLE,
                               "offline mode")
            return
        result = self.quote_verification
        if result is not None and result.verified:
            self.readiness.set("theta_quote_parser_verified", PASS,
                               evidence=result.as_dict())
        elif self.market_closed:
            self.readiness.set(
                "theta_quote_parser_verified", MARKET_CLOSED,
                "no live option quote can arrive while the market is closed; "
                "parser NOT verified by synthetic data")
        else:
            self.readiness.set(
                "theta_quote_parser_verified", RED,
                f"failed checks: {result.failures}" if result
                else "no live quote message verified yet",
                evidence=result.as_dict() if result else None)

    def _gate_live_quotes(self) -> None:
        """A required candidate must have a quote from the CURRENT stream
        generation. A REST snapshot does not satisfy this."""
        if self.offline:
            self.readiness.set("theta_live_quotes_fresh", NOT_TESTABLE, "offline mode")
            return
        s_ = self.theta_stream
        fresh = 0
        gen = None
        try:
            if s_ is not None and hasattr(s_, "health"):
                gen = s_.health().get("generation")
                fresh = self.fresh_quote_count
        except Exception as e:  # noqa: BLE001 -- fail closed, never take
            # startup down; the daemon must keep managing open positions.
            self.readiness.set("theta_live_quotes_fresh", RED,
                               f"stream probe failed: {e!r}")
            return
        if fresh > 0:
            self.readiness.set("theta_live_quotes_fresh", PASS,
                               evidence={"fresh_contracts": fresh, "generation": gen})
        elif self.market_closed:
            self.readiness.set("theta_live_quotes_fresh", MARKET_CLOSED,
                               "no live quotes while the market is closed")
        else:
            self.readiness.set("theta_live_quotes_fresh", RED,
                               "no candidate has a quote from the current stream "
                               "generation")

    def _gate_universe(self) -> None:
        """Positive rows, valid expirations only, Greek ages inside ceiling."""
        if self.offline:
            self.readiness.set("universe_greeks_warm", NOT_TESTABLE, "offline mode")
            return
        c = self.greek_cache
        try:
            status = c.cache_status() if c else {}
            rows = sum(int(v.get("n_rows", 0) or 0) for v in (status or {}).values())
            ages = [float(v.get("age_seconds", 1e9) or 1e9)
                    for v in (status or {}).values()]
            stale = [a for a in ages if a > self.max_greek_age_seconds]
            bad_days = [d for d in (status or {})
                        if _is_weekend(d)]
            ok = rows > 0 and not stale and not bad_days
            reason = ""
            if rows <= 0:
                reason = "no contract rows loaded"
            elif stale:
                reason = f"{len(stale)} expiration(s) older than {self.max_greek_age_seconds}s"
            elif bad_days:
                reason = f"weekend pseudo-expirations present: {bad_days}"
            self.readiness.set_bool(
                "universe_greeks_warm", ok, reason,
                evidence={"rows": rows, "expirations": sorted(status or {}),
                          "max_age_seconds": max(ages) if ages else None})
            self.universe_rows = rows
        except Exception as e:  # noqa: BLE001
            self.universe_rows = 0
            self.readiness.set("universe_greeks_warm", RED, repr(e))

    def _gate_candidate_universe(self) -> None:
        """Enough contracts with BOTH a fresh stream quote and a fresh Greek."""
        if self.offline:
            self.readiness.set("candidate_universe_ready", NOT_TESTABLE, "offline mode")
            return
        ready = self.candidate_ready_count
        if ready >= self.min_candidates:
            self.readiness.set("candidate_universe_ready", PASS,
                               evidence={"eligible": ready})
        elif self.market_closed:
            self.readiness.set("candidate_universe_ready", MARKET_CLOSED,
                               "candidates cannot pair a live quote with a Greek "
                               "while the market is closed")
        else:
            self.readiness.set("candidate_universe_ready", RED,
                               f"{ready} eligible candidates < {self.min_candidates} "
                               "required")

    def _gate_broker(self) -> None:
        if self.offline:
            self.readiness.set("broker_prewarmed", NOT_TESTABLE, "offline mode")
            return
        b = self.broker
        ok = bool(b and getattr(b, "prewarmed", False))
        self.readiness.set_bool("broker_prewarmed", ok,
                                "" if ok else "Alpaca PAPER transport not prewarmed")

    def _gate_trade_updates(self) -> None:
        if self.offline:
            self.readiness.set("trade_updates", NOT_TESTABLE, "offline mode")
            return
        t = self.trade_updates
        ok = bool(t and t.is_connected())
        self.readiness.set_bool("trade_updates", ok,
                                "" if ok else "trade_updates not connected")

    def _gate_reconcile(self) -> None:
        if self.offline:
            self.readiness.set("reconciled", NOT_TESTABLE, "offline mode")
            return
        ok = self.reconcile(reason="startup")
        self.readiness.set_bool("reconciled", ok,
                                "" if ok else "startup reconciliation failed")

    def _gate_detector(self) -> None:
        d = self.detector
        ok = bool(d and getattr(d, "synchronized", False))
        self.readiness.set_bool(
            "detector_synced", ok,
            "" if ok else "persistent minute detector not synchronized")

    def _schedule_periodics(self) -> None:
        now = self.clock()
        self.bus.schedule(now + RECONCILE_INTERVAL_SECONDS,
                          make_event(ev.EV_RECONCILE, payload={"periodic": True}))
        self.bus.schedule(now + HEALTH_INTERVAL_SECONDS,
                          make_event(ev.EV_HEALTH, payload={"periodic": True}))

    # ---------------------------------------------------------- event loop
    def run(self, max_events: Optional[int] = None,
            idle_timeout: Optional[float] = None) -> int:
        """Blocks on the bus. `max_events`/`idle_timeout` exist so tests can
        drive a bounded loop; production passes neither."""
        handled = 0
        while not self._stop.is_set():
            event = self.bus.get(timeout=idle_timeout)
            if event is None:
                break
            t0 = self.clock()
            try:
                self._handlers.get(event.event_type, self._on_unknown)(event)
            except Exception as e:  # noqa: BLE001 -- one bad handler must not
                # kill the loop; an unhandled exception here would abandon an
                # open position.
                self.handler_errors += 1
                logger.exception("handler failed for %s: %s", event.event_type, e)
            finally:
                self.bus.record_handled(event, t0, self.clock() - t0)
            handled += 1
            if max_events is not None and handled >= max_events:
                break
        return handled

    def stop(self) -> None:
        self._stop.set()
        self.bus.publish(make_event(ev.EV_SHUTDOWN))
        self.bus.close()

    # ------------------------------------------------------------ handlers
    def _on_quote(self, event: Event) -> None:
        """Quote arrival drives entry marketability sampling and (once
        wired) exit threshold evaluation."""
        now = self.clock()
        for attempt in list(self.attempts.values()):
            if attempt.occ == getattr(event.quote, "occ", None) and not attempt.terminal:
                attempt.tick(now, event.quote, cancel_fn=self._cancel_fn())

    def _on_order_event(self, event: Event) -> None:
        attempt = self.attempts.get(event.client_order_id)
        if attempt is None:
            return
        payload = event.payload or {}
        attempt.on_trade_update(
            payload.get("event", event.event_type),
            price=payload.get("price"), filled_qty=payload.get("filled_qty"),
            broker_order_id=event.order_id, now_monotonic=self.clock(),
            reason=payload.get("reason", ""))
        if attempt.terminal:
            self.bus.cancel_scheduled(f"ttl-{attempt.client_order_id}")
            if self.pipeline_attached:
                # The assembled pipeline chains to this handler and then
                # publishes the richer, position-aware version of the same
                # fact. Publishing here too sent every fill to Telegram
                # TWICE. The state work above still runs; only the duplicate
                # announcement is suppressed.
                return
            if attempt.has_position:
                # Includes FILLED_AFTER_CANCEL_REQUEST: a late fill is a real
                # position and must be managed, never discarded.
                self._publish_critical(lc.ORDER_FILL, attempt.summary(),
                                       client_order_id=attempt.client_order_id)
            else:
                self._publish_critical(lc.ORDER_TERMINAL, attempt.summary(),
                                       client_order_id=attempt.client_order_id)

    def _on_signal(self, event: Event) -> None:
        if not self.readiness.entries_permitted:
            logger.warning("signal %s ignored: entries blocked by %s",
                           event.signal_id, self.readiness.blocking())
            self._publish_critical("signal_blocked",
                                   {"signal_id": event.signal_id,
                                    "blocking": self.readiness.blocking()},
                                   signal_id=event.signal_id)
            return
        logger.info("signal %s accepted (entry wiring pending)", event.signal_id)

    def _on_exit_trigger(self, event: Event) -> None:
        self._publish_critical("exit_trigger", event.payload or {},
                               position_id=event.position_id)

    def _on_reconcile(self, event: Event) -> None:
        ok = self.reconcile(reason=(event.payload or {}).get("reason", "periodic"))
        # set_bool, not set: this is a genuine binary gate and the tri-state
        # API rejects a raw boolean.
        self.readiness.set_bool("reconciled", ok,
                                "" if ok else "reconciliation failed")
        self.bus.schedule(self.clock() + RECONCILE_INTERVAL_SECONDS,
                          make_event(ev.EV_RECONCILE, payload={"periodic": True}))

    def _on_reconnect(self, event: Event) -> None:
        """Any reconnect invalidates assumptions: force a reconciliation
        pass rather than trusting stream state across the gap."""
        logger.warning("reconnect observed: %s", (event.payload or {}).get("source"))
        self.bus.publish(make_event(ev.EV_RECONCILE,
                                    payload={"reason": "reconnect"}))

    def _on_deadline(self, event: Event) -> None:
        payload = event.payload or {}
        if payload.get("kind") == "entry_ttl":
            attempt = self.attempts.get(payload.get("client_order_id"))
            if attempt is not None and not attempt.terminal:
                attempt.tick(self.clock(), None, cancel_fn=self._cancel_fn())

    def _on_health(self, event: Event) -> None:
        """Periodic health tick ALSO re-evaluates readiness.

        Previously this only republished a snapshot, so a gate that was
        stale at startup stayed stale until the process restarted. A gate
        must be able to go green on its own -- that is the whole point of
        waiting behind gates rather than requiring a manual start."""
        self.revalidate()
        self._publish_state("health")
        self.bus.schedule(self.clock() + HEALTH_INTERVAL_SECONDS,
                          make_event(ev.EV_HEALTH, payload={"periodic": True}))

    def revalidate(self) -> Readiness:
        """Re-run the gates that can change at runtime, without re-acquiring
        the singleton or reopening state. Safe to call repeatedly."""
        was_permitted = self.readiness.entries_permitted
        try:
            self._gate_notifications()
            self._gate_terminal()
            self._gate_stream()
            self._gate_quote_parser()
            self._gate_live_quotes()
            self._gate_universe()
            self._gate_candidate_universe()
            self._gate_broker()
            self._gate_trade_updates()
            self._gate_detector()
        except Exception as e:  # noqa: BLE001 -- a gate error must not kill the loop
            logger.exception("readiness revalidation failed: %s", e)
        now_permitted = self.readiness.entries_permitted
        if now_permitted != was_permitted:
            logger.warning("READINESS CHANGED: entries_permitted %s -> %s (blocking=%s)",
                           was_permitted, now_permitted, self.readiness.blocking())
            self._publish_critical(
                "readiness_green" if now_permitted else "readiness_blocked",
                self.readiness.as_dict())
        return self.readiness

    def _on_shutdown(self, event: Event) -> None:
        self._stop.set()

    def _on_unknown(self, event: Event) -> None:
        logger.warning("no handler for event type %r", event.event_type)

    # ------------------------------------------------------------- helpers
    def _cancel_fn(self):
        if self.broker is None:
            return None
        return lambda broker_order_id: self.broker.cancel_order(broker_order_id)

    def register_attempt(self, attempt) -> None:
        """Tracks an entry and schedules its TTL as ONE monotonic deadline --
        not a polling loop."""
        self.attempts[attempt.client_order_id] = attempt
        self.bus.schedule(
            attempt.submitted_monotonic + attempt.ttl_seconds,
            make_event(ev.EV_DEADLINE,
                       event_id=f"ttl-{attempt.client_order_id}",
                       client_order_id=attempt.client_order_id,
                       payload={"kind": "entry_ttl",
                                "client_order_id": attempt.client_order_id}))

    def reconcile(self, reason: str = "periodic") -> bool:
        """Broker truth vs local state, then rebuild supervision.

        This used to check only that `open_orders()` and `positions()` came
        back `ok` and return True -- so the `reconciled` gate went green on
        "the broker answered", having adopted nothing. A restart holding a
        position passed the gate with an empty supervision set and the
        position went unmanaged. Three things now have to happen, in order,
        before entries may be permitted:

            1. the transport answers at all
            2. smc/reconcile.py runs and comes back clean (no orphan, no
               ambiguous ownership, no unresolved intent, no halt)
            3. every durably-open position is rebuilt into supervision

        Order matters between 2 and 3: reconciliation is what corrects
        quantities and finalises unresolved intents, so rebuilding first would
        hand the exit monitor a position the broker has already closed.

        Returns success; a failure degrades readiness rather than raising, so
        an open position keeps being managed while entries stay blocked.
        """
        if self.broker is None or self.state is None:
            return False
        detail = {"reason": reason}
        try:
            orders = self.broker.open_orders()
            positions = self.broker.positions()
            if not (getattr(orders, "ok", False) and getattr(positions, "ok", False)):
                detail["error"] = (
                    f"broker transport not ok (orders={getattr(orders, 'status', None)}, "
                    f"positions={getattr(positions, 'status', None)})")
                self._publish_critical(ev_kind_reconciliation_result(False), detail)
                return False

            result = None
            if self.full_reconcile is not None:
                result = self.full_reconcile()
                self.last_recon = result
                detail.update({
                    "clean": bool(getattr(result, "clean", False)),
                    "degraded": bool(getattr(result, "degraded", False)),
                    "adopted": len(getattr(result, "adopted", []) or []),
                    "qty_corrections": len(getattr(result, "qty_corrections", []) or []),
                    "orphans": len(getattr(result, "orphans", []) or []),
                    "orphan_orders": len(getattr(result, "orphan_orders", []) or []),
                    "ambiguous": len(getattr(result, "ambiguous", []) or []),
                    "resolved_intents": len(getattr(result, "resolved_intents", []) or []),
                    "halt_reasons": list(getattr(result, "halt_reasons", []) or [])[:5],
                })

            if self.rebuild_positions is not None:
                detail["supervised_positions"] = self.rebuild_positions()

            if self.trade_updates is not None:
                self.trade_updates.clear_needs_reconcile()

            ok = True if result is None else bool(result.clean)
            # ANNOUNCE ONLY WHAT IS NEWS. Reconciliation runs every 30s;
            # publishing its result unconditionally sent ~120 identical
            # "nothing changed" messages an hour to Telegram, which buries the
            # signal notices that actually matter. A clean, unchanged,
            # periodic pass is a heartbeat, and heartbeats belong on the
            # dashboard, not in an alert channel.
            changed = any(detail.get(k) for k in (
                "adopted", "qty_corrections", "orphans", "orphan_orders",
                "ambiguous", "resolved_intents"))
            recovered = ok and self._last_recon_ok is False
            if (not ok) or changed or recovered or reason != "periodic":
                self._publish_critical(ev_kind_reconciliation_result(ok), detail)
            self._last_recon_ok = ok
            return ok
        except Exception as e:  # noqa: BLE001
            logger.exception("reconciliation failed (%s): %s", reason, e)
            detail["error"] = repr(e)
            self._publish_critical("reconciliation_mismatch", detail)
            return False

    def _publish_critical(self, kind: str, detail: dict, **ids) -> None:
        """Durable-first: the outbox commits the canonical event before any
        delivery is attempted, so a notification failure cannot lose it."""
        if self.notifier is None:
            return
        try:
            self.notifier.publish(kind, self._format(kind, detail), detail=detail,
                                  client_order_id=ids.get("client_order_id"),
                                  position_id=ids.get("position_id"))
        except Exception as e:  # noqa: BLE001 -- notification never blocks trading
            logger.warning("notify publish failed (ignored): %s", e)

    @staticmethod
    def _format(kind: str, detail: dict) -> str:
        # Bounded: an over-length body is a permanent Telegram 400, and under
        # ordered delivery a permanently-failing event stalls the chain.
        return lc.format_message(kind, detail)

    def _publish_state(self, reason: str) -> None:
        snapshot = self.health()
        if self.dashboard_sync is not None:
            try:
                self.dashboard_sync(snapshot)
            except Exception as e:  # noqa: BLE001 -- dashboard never blocks trading
                logger.warning("dashboard sync failed (ignored): %s", e)
        # Same rule: announce the TRANSITION, not the condition. The health
        # tick fires every 60s, so publishing "still degraded" each time sent
        # an alert a minute for as long as the market was closed -- and the
        # blocking set is already on the dashboard continuously.
        blocking = tuple(self.readiness.blocking())
        if self.readiness.degraded and self.notifier is not None:
            if blocking != self._last_degraded_blocking:
                self._publish_critical("degraded",
                                       {"reason": reason, "blocking": list(blocking)})
                self._last_degraded_blocking = blocking
        elif not self.readiness.degraded:
            self._last_degraded_blocking = None

    def health(self) -> dict:
        return {
            "ts": dt.datetime.now(dt.timezone.utc).isoformat(),
            "readiness": self.readiness.as_dict(),
            "bus": self.bus.health(),
            "open_attempts": sum(1 for a in self.attempts.values() if not a.terminal),
            "handler_errors": self.handler_errors,
            "theta_stream": _safe_health(self.theta_stream),
            "trade_updates": _safe_health(self.trade_updates),
            "notifier": _safe_health(self.notifier),
        }

    def shutdown(self) -> None:
        """Orderly: stop the loop, close streams, flush state, release the
        lock. Never os._exit -- a half-written SQLite commit is worse than a
        slow exit."""
        self._stop.set()
        for comp in (self.trade_updates, self.theta_stream, self.notifier):
            try:
                if comp is not None and hasattr(comp, "stop"):
                    comp.stop()
            except Exception:  # noqa: BLE001
                pass
        try:
            self.bus.close()
        except Exception:  # noqa: BLE001
            pass
        if self.state is not None:
            try:
                self.state.close()
            except Exception:  # noqa: BLE001
                pass
        if self.singleton is not None:
            self.singleton.release()
        logger.info("runner shutdown complete")
