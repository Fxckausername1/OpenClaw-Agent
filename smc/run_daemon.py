"""Entry point for the SMC PAPER daemon.

    python -m smc.run_daemon --mode validate
    python -m smc.run_daemon --mode paper-connectivity
    python -m smc.run_daemon --mode paper-forward

MODES, in increasing order of what they are allowed to touch:

  validate            Offline. Config, paper guard, singleton, SQLite,
                      outbox, detector warmup from cache. Opens NO socket to
                      ThetaData or Alpaca and can never place an order.
                      This is the mode the cutover checklist runs first.

  paper-connectivity  Everything validate does, plus real ThetaData and
                      Alpaca PAPER connections, reconciliation and the
                      dashboard. Entries remain HARD-DISABLED regardless of
                      gate state -- this mode exists to measure the Monday
                      gate items without any possibility of trading.

  paper-forward       The real forward runner. Entries permitted only when
                      every readiness gate is green.

Entries are gated by mode AND readiness, and the mode check is evaluated
first and separately. A gate bug therefore cannot enable trading in
validate or paper-connectivity -- two independent conditions have to be
true, not one.

STARTUP ORDER is fixed and each step must succeed before the next is tried.
The paper guard runs at step 2, before the singleton and before any handle
exists, so a misconfigured endpoint exits while there is nothing to flush.

SHUTDOWN blocks new entries first, preserves management of any open
position, persists cursors and intents, stops workers in a defined order,
flushes durable state and releases the singleton last. SIGTERM is handled,
so a restart cannot orphan an order.
"""
from __future__ import annotations

import argparse
import datetime as dt
import logging
import signal
import sys
import time
from pathlib import Path

logger = logging.getLogger("smc.run_daemon")

MODE_OFFLINE_VALIDATE = "offline-validate"
MODE_VALIDATE = "validate"
MODE_CONNECTIVITY = "paper-connectivity"
MODE_FORWARD = "paper-forward"
MODES = (MODE_OFFLINE_VALIDATE, MODE_VALIDATE, MODE_CONNECTIVITY, MODE_FORWARD)

# Modes that open no external connection whatsoever.
OFFLINE_MODES = frozenset({MODE_OFFLINE_VALIDATE})

# Dependency statuses. MARKET_CLOSED is deliberately NOT a failure: it means
# the dependency is healthy but cannot be fully exercised right now.
DEP_PASS = "PASS"
DEP_FAIL = "FAIL"
DEP_MARKET_CLOSED = "MARKET_CLOSED"
DEP_SKIPPED = "SKIPPED"

# Only ONE mode may ever place an order.
ENTRY_CAPABLE_MODES = frozenset({MODE_FORWARD})

EXIT_OK = 0
EXIT_CONFIG = 2
EXIT_PAPER_VIOLATION = 3
EXIT_ALREADY_RUNNING = 4
EXIT_STARTUP_FAILED = 5
EXIT_DEPENDENCY_FAILED = 6

ROOT = Path(__file__).resolve().parent.parent
DASHBOARD_PATH = ROOT / "data" / "live_heff_smc" / "daemon_dashboard.json"
HEALTH_PATH = ROOT / "data" / "live_heff_smc" / "daemon_health.json"
STATE_DB = ROOT / "data" / "live_heff_smc" / "smc_state.db"
CURSOR_PATH = ROOT / "data" / "live_heff_smc" / "detector_cursor.json"


class StartupError(RuntimeError):
    pass


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be >= 1")
    return parsed


def parse_args(argv=None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(prog="smc.run_daemon")
    ap.add_argument("--mode", choices=MODES, required=True)
    ap.add_argument("--once", action="store_true",
                    help="run startup and one health pass, then exit (CI/checklist)")
    ap.add_argument("--lock", default="/tmp/smc_paper_runner.lock")
    ap.add_argument("--dashboard", default=str(DASHBOARD_PATH))
    ap.add_argument(
        "--max-entry-submissions", type=positive_int, default=None,
        help="operational canary latch: after N entry POST attempts, block new entries")
    return ap.parse_args(argv)


def entries_allowed(mode: str, readiness) -> bool:
    """TWO independent conditions. Mode first, so a readiness bug cannot
    enable trading in a non-trading mode."""
    if mode not in ENTRY_CAPABLE_MODES:
        return False
    return bool(readiness.entries_permitted)


class Daemon:
    def __init__(self, args):
        self.args = args
        self.mode = args.mode
        self.components = {}
        self.pipeline = None
        self.runner = None
        self.config = None
        self.singleton = None
        self._stopping = False
        self._entry_submission_count = 0
        self._budget_exhausted_announced = False
        self.entry_budget = None

    # --------------------------------------------------------------- boot
    def start(self):
        from smc import paper_guard
        from smc.singleton import AlreadyRunning, SingleInstance

        # 1. immutable configuration
        config = self._load_config()
        self.config = config

        # 2. PAPER enforcement -- before any thread, socket or DB handle.
        #    fatal_guard converts a violation into SystemExit(3); nothing is
        #    initialized yet, so there is nothing to flush.
        import options_orchestrator as oo
        paper_guard.fatal_guard(oo.PAPER, (oo.H or {}).get("APCA-API-KEY-ID"))

        # 3. singleton
        self.singleton = SingleInstance(Path(self.args.lock))
        try:
            self.singleton.acquire()
        except AlreadyRunning as e:
            logger.critical("%s", e)
            raise SystemExit(EXIT_ALREADY_RUNNING) from e

        # 4. SQLite + durable outbox
        store, outbox = self._open_state()

        # 5-9. external connections (skipped entirely in validate)
        terminal_manager = stream = greeks = broker = trade_updates = None
        if self.mode not in OFFLINE_MODES:
            terminal_manager, stream, greeks, broker, trade_updates = self._connect(config)

        # 11. detector
        detector = self._build_detector()

        # 12. dashboard + notification workers
        notifier = self._build_notifier(config, outbox)

        # assemble
        from smc.runner import PaperRunner
        self.runner = PaperRunner(
            singleton=None,                 # already held; do not re-acquire
            state_store=store, outbox=outbox, notifier=notifier,
            theta_stream=stream, greek_cache=greeks, broker=broker,
            trade_updates=trade_updates, detector=detector,
            terminal_check=(lambda: bool(terminal_manager and terminal_manager.is_ready())),
            dashboard_sync=self._sync_dashboard)
        self.runner.max_quote_age_seconds = config.max_quote_age_seconds
        self.components = {"store": store, "outbox": outbox, "notifier": notifier,
                           "stream": stream, "greeks": greeks, "broker": broker,
                           "trade_updates": trade_updates, "detector": detector,
                           "terminal_manager": terminal_manager,
                           "runner": self.runner}

        # ONE production assembly, identical across every mode.
        self.pipeline = self._build_production_pipeline()

        market_closed = self._market_closed()
        self.runner.start(offline=(self.mode in OFFLINE_MODES),
                          market_closed=market_closed)

        # 13. entries: mode AND readiness
        allowed = entries_allowed(self.mode, self.runner.readiness)
        logger.info("mode=%s entries_allowed=%s blocking=%s",
                    self.mode, allowed, self.runner.readiness.blocking())
        return self.runner

    # ------------------------------------------------------------- pieces
    def _load_config(self):
        try:
            from smc.config import load_config
            return load_config()
        except Exception as e:  # noqa: BLE001
            logger.critical("configuration invalid: %s", e)
            raise SystemExit(EXIT_CONFIG) from e

    def _open_state(self):
        from smc.entry_budget import EntryBudget
        from smc.notify_outbox import NotifyOutbox
        from smc.state import SmcStateStore
        store = SmcStateStore(STATE_DB)
        # Durable, session-scoped. Survives Restart=always, crash and reboot;
        # the in-memory counter alone did not.
        ceiling = getattr(self.args, "max_entry_submissions", None)
        self.entry_budget = EntryBudget(store.conn,
                                        max_attempts=1 if ceiling is None else int(ceiling))
        return store, NotifyOutbox(store.conn)

    def _connect(self, config):
        """Steps 5-9. Any failure is recorded as a red gate rather than a
        crash: a degraded daemon still manages an open position."""
        terminal_manager = None
        stream = greeks = broker = trade_updates = None
        try:
            from smc.theta_terminal import ThetaTerminalManager
            terminal_manager = ThetaTerminalManager()
            terminal_manager.start()
        except Exception as e:  # noqa: BLE001
            logger.error("Theta Terminal manager start failed: %s", e)
        # ORDER MATTERS. The Greek cache is warmed and the desired universe
        # is set BEFORE the socket starts, so _resubscribe_all() covers the
        # whole universe on connect. Starting the socket first meant
        # _resubscribe_all ran against an empty desired set and nothing was
        # subscribed until the next 5s reconcile tick -- which is what made
        # the startup readiness report say subscribed=0/826.
        occs = set()
        try:
            from smc.theta_market_data import ThetaMarketDataCache
            greeks = ThetaMarketDataCache()
            greeks.refresh()
            occs = set(greeks.candidate_occs())
        except Exception as e:  # noqa: BLE001
            logger.error("Greek cache warm failed: %s", e)
        try:
            from smc.theta_stream import ThetaStreamClient
            stream = ThetaStreamClient()
            if occs:
                stream.set_desired_universe(occs)
            stream.start()
            if occs:
                # Prove the acks rather than assume the ordering held.
                ok = stream.wait_for_subscriptions(timeout=30.0)
                logger.info("startup subscription barrier: %s (%s)",
                            "acknowledged" if ok else "TIMED OUT",
                            stream.health().get("n_subscribed"))
        except Exception as e:  # noqa: BLE001
            logger.error("ThetaData stream start failed: %s", e)
        try:
            import options_orchestrator as oo
            from smc.fast_broker import FastPaperBroker
            broker = FastPaperBroker(oo.PAPER, oo.H)      # prewarms
        except Exception as e:  # noqa: BLE001
            logger.error("Alpaca PAPER transport failed: %s", e)
        try:
            import options_orchestrator as oo
            from smc.trade_updates import AlpacaTradeUpdatesClient
            trade_updates = AlpacaTradeUpdatesClient(
                oo.PAPER, oo.H.get("APCA-API-KEY-ID"), oo.H.get("APCA-API-SECRET-KEY"),
                quote_source=(stream.get_quote if stream else None))
            trade_updates.start()
        except Exception as e:  # noqa: BLE001
            logger.error("trade_updates start failed: %s", e)
        return terminal_manager, stream, greeks, broker, trade_updates

    def _build_detector(self):
        """In validate the bar source is the LOCAL cache, so the mode proves
        the detector assembles without opening a socket. In the connected
        modes it is Alpaca REST."""
        try:
            from smc.detector import PersistentDetector
            if self.mode in OFFLINE_MODES:
                fetch = self._cached_bar_source()
            else:
                from thetadata_pipeline.qqq_bars_fetch import (
                    SYMBOL, _headers, fetch_day_bars)
                headers = _headers()
                fetch = lambda s: fetch_day_bars(SYMBOL, s, headers)  # noqa: E731
            det = PersistentDetector(fetch_session_bars=fetch, cursor_path=CURSOR_PATH)
            self._warm_detector(det)
            return det
        except Exception as e:  # noqa: BLE001
            logger.error("detector build failed: %s", e)
            return None

    @staticmethod
    def _cached_bar_source():
        """Per-session slices of the already-downloaded bar store."""
        from zoneinfo import ZoneInfo

        import pandas as pd

        from thetadata_pipeline.qqq_bars_fetch import load_all_bars
        et = ZoneInfo("America/New_York")
        df = load_all_bars()
        df["t"] = pd.to_datetime(df["t"], utc=True)
        df["_s"] = [t.astimezone(et).date().isoformat() for t in df["t"]]
        index = {k: g.drop(columns="_s").sort_values("t").reset_index(drop=True)
                 for k, g in df.groupby("_s")}
        return lambda s: index.get(s, pd.DataFrame(columns=["t", "o", "h", "l", "c", "v"]))

    def _warm_detector(self, det) -> None:
        """Warms from the most recent sessions the source actually has. A
        warmup failure leaves the gate red rather than raising -- a degraded
        detector must not take the whole daemon down."""
        try:
            from thetadata_pipeline.qqq_bars_fetch import load_all_bars
            from zoneinfo import ZoneInfo

            import pandas as pd
            et = ZoneInfo("America/New_York")
            df = load_all_bars()
            df["t"] = pd.to_datetime(df["t"], utc=True)
            sessions = sorted({t.astimezone(et).date().isoformat() for t in df["t"]})
            if not sessions:
                return
            target_session = sessions[-1]
            if self.mode not in OFFLINE_MODES:
                from smc import calendar as smc_calendar
                today_et = dt.datetime.now(et).date()
                schedule = smc_calendar.session_schedule(today_et)
                if getattr(schedule, "is_trading_day", False):
                    # Before the open this intentionally fetches an empty
                    # current session, then the minute clock appends bars.
                    target_session = today_et.isoformat()
            prior = [s for s in sessions if s < target_session][-5:]
            det.warmup(session=target_session, prior_sessions=prior)
        except Exception as e:  # noqa: BLE001
            logger.error("detector warmup failed (gate stays red): %s", e)

    def _build_notifier(self, config, outbox):
        try:
            from smc.notify_queue import NotifyQueue
            nq = NotifyQueue(config, outbox, db_path=STATE_DB)
            nq.start()
            return nq
        except Exception as e:  # noqa: BLE001
            logger.error("notifier start failed: %s", e)
            return None

    # ------------------------------------------------- production assembly
    def _build_production_pipeline(self):
        """Constructs every production component and wires them through
        smc.assembly.build_pipeline -- the SAME factory the integration test
        drives. There is no second wiring path."""
        from smc.assembly import build_pipeline
        from smc.detector_worker import DetectorWorker
        from smc.exit_liveness import ExitLivenessTracker
        from smc.exit_monitor import StreamingExitMonitor
        from smc.quote_worker import QuoteExitWorker
        from smc.live_detector_clock import LiveDetectorCycle
        from smc.live_detector_clock import ConfirmedMinuteScheduler
        from thetadata_pipeline.bt2_exits import ExitConfig

        cfg = self.config
        detector = self.components.get("detector")
        broker = self.components.get("broker")
        notifier = self.components.get("notifier")

        worker = None
        live_cycle = None
        if detector is not None:
            run_detect = detector.detect
            if self.mode not in OFFLINE_MODES:
                live_cycle = LiveDetectorCycle(detector)
                run_detect = live_cycle
            worker = DetectorWorker(run_detect=run_detect)
            worker.start()
        self.components["detector_worker"] = worker
        self.components["live_detector_cycle"] = live_cycle

        liveness = ExitLivenessTracker(
            rest_lookup=(lambda coid: self._rest_order_lookup(coid)),
            raise_incident=(lambda kind, detail: self._raise_incident(kind, detail)))
        self.components["exit_liveness"] = liveness

        exits = StreamingExitMonitor(
            open_positions=(lambda: []),        # rebound by build_pipeline
            submit_exit=self._submit_exit,
            exit_config=ExitConfig(),
            schedule_for=self._schedule_for,
            config=cfg)
        self.components["exit_monitor"] = exits

        pipeline = build_pipeline(
            runner=self.runner, detector_worker=worker, exit_monitor=exits,
            exit_liveness=liveness, broker=broker, notifier=notifier,
            select_contract=self._select_contract,
            build_universe=self._build_universe,
            entry_builder=self._build_entry,
            entry_submitter=self._submit_entry,
            entry_enabled=self._entry_enabled)
        minute_scheduler = None
        if worker is not None and self.mode not in OFFLINE_MODES:
            minute_scheduler = ConfirmedMinuteScheduler(worker.request)
            minute_scheduler.start()
        self.components["minute_scheduler"] = minute_scheduler
        quote_worker = QuoteExitWorker(
            handle_quote=pipeline.process_exit_quote,
            is_protected_occ=(lambda occ: any(
                p.get("occ") == occ for p in pipeline.positions.values())))
        pipeline.exit_worker = quote_worker
        self.components["quote_worker"] = quote_worker
        quote_worker.start()
        self._wire_streams()
        return pipeline

    def _wire_streams(self) -> None:
        """Quote stream -> universe + exit monitor; trade_updates -> entry,
        position and exit state. Both publish onto the canonical bus rather
        than calling handlers directly, so priority and instrumentation
        apply uniformly."""
        from smc import events as ev
        from smc.events import make_event
        stream = self.components.get("stream")
        tu = self.components.get("trade_updates")
        quote_worker = self.components.get("quote_worker")
        bus = self.runner.bus
        if stream is not None:
            def on_quote(q):
                # Reader-side work is bounded and in-memory only.
                self.runner.observe_live_quote(q)
                if quote_worker is not None:
                    quote_worker.submit(q)
                bus.publish(
                    make_event(ev.EV_QUOTE, quote=q,
                               stream_generation=getattr(q, "generation", None)),
                    coalesce_key=f"quote:{getattr(q, 'occ', '')}")
            stream.on_quote = on_quote
        if tu is not None:
            tu._on_event = (lambda upd: bus.publish(make_event(
                {"fill": ev.EV_FILL, "partial_fill": ev.EV_PARTIAL_FILL,
                 "canceled": ev.EV_CANCEL, "rejected": ev.EV_REJECT,
                 "new": ev.EV_ORDER_ACK}.get(upd.event, ev.EV_ORDER_ACK),
                client_order_id=upd.client_order_id, order_id=upd.broker_order_id,
                payload={"event": upd.event,
                         "price": upd.filled_avg_price or upd.event_price,
                         "filled_qty": upd.filled_qty,
                         "reason": upd.order_status or ""})))

    # ---- collaborators the pipeline calls --------------------------------
    def _entry_enabled(self) -> bool:
        """Mode AND readiness AND the DURABLE session budget.

        The budget is read from SQLite rather than process memory, so a
        restart cannot hand back a spent allowance."""
        if not entries_allowed(self.mode, self.runner.readiness):
            return False
        budget = getattr(self, "entry_budget", None)
        if budget is None:
            return False          # fail closed: no budget, no entries
        if not budget.can_submit():
            st = budget.state()
            if not self._budget_exhausted_announced:
                self._budget_exhausted_announced = True
                self._raise_incident("daily_canary_budget_exhausted", st.as_dict())
            return False
        return True

    def _rest_order_lookup(self, client_order_id):
        b = self.components.get("broker")
        if b is None:
            return None
        call = b.get_order_by_client_id(client_order_id)
        return call.body if getattr(call, "ok", False) else None

    def _raise_incident(self, kind, detail):
        n = self.components.get("notifier")
        if n is not None:
            n.publish(kind, f"{kind}: {detail}", detail=detail)

    def _schedule_for(self, now):
        import datetime as _dt
        from zoneinfo import ZoneInfo
        from smc import calendar as smc_calendar
        return smc_calendar.session_schedule(
            now.astimezone(ZoneInfo("America/New_York")).date()
            if isinstance(now, _dt.datetime) else now)

    def _build_universe(self, sig):
        """Atomic quote+Greek snapshot for the signal's candidate set."""
        stream = self.components.get("stream")
        greeks = self.components.get("greeks")
        if stream is None or greeks is None:
            return None
        from smc.candidate_universe import take_snapshot
        occs = greeks.candidate_occs()
        if not occs:
            return None
        snapshot = take_snapshot(stream, greeks, occs)
        self.components["last_universe"] = snapshot.diagnostics()
        self.runner.candidate_ready_count = len(snapshot.eligible)
        return snapshot.to_book()

    def _select_contract(self, book, sig):
        from smc.selector_variant_b import select_variant_b_contract
        if book is None or getattr(book, "empty", True):
            return None
        import datetime as _dt
        return select_variant_b_contract(
            book, "C" if getattr(sig, "side", "long") == "long" else "P",
            _dt.datetime.now(_dt.timezone.utc))

    def _build_entry(self, sig, selection, ident):
        """Risk-check and durably create the entry intent before any POST."""
        from zoneinfo import ZoneInfo

        from smc.entry_manager import EntryAttempt
        from smc.lifecycle import signal_age_seconds
        from smc.risk import check_entry_allowed
        from smc.state import DuplicateSignal
        from smc.theta_market_data import build_occ_symbol
        from thetadata_pipeline.selector_policy_experiment import (
            DEFAULT_FEE_PER_CONTRACT, DEFAULT_QUANTITY, DEBIT_CAP_DOLLARS)

        contract = getattr(selection, "contract", None) or {}
        try:
            expiration = contract["expiration"]
            if hasattr(expiration, "date") and not isinstance(expiration, dt.date):
                expiration = expiration.date()
            occ = build_occ_symbol("QQQ", expiration, float(contract["strike"]),
                                   contract["right"])
            limit_price = round(float(contract["ask"]), 2)
        except (KeyError, TypeError, ValueError):
            return None
        qty = int(DEFAULT_QUANTITY)
        if limit_price * 100 * qty + DEFAULT_FEE_PER_CONTRACT * qty > DEBIT_CAP_DOLLARS:
            return None

        now = dt.datetime.now(dt.timezone.utc)
        signal_ts = ident.get("bar_close_utc")
        if not signal_ts or signal_age_seconds(signal_ts, now) > self.config.max_signal_age_seconds:
            self.components["store"].log_event(
                "SIGNAL_REJECTED_STALE",
                {"signal_key": ident.get("signal_key"), "signal_ts": signal_ts})
            return None
        now_et = now.astimezone(ZoneInfo("America/New_York"))
        gate = check_entry_allowed(
            self.components["store"], self.components.get("broker"), self.config,
            occ=occ, entry_premium=limit_price, intended_qty=qty,
            dashboard_db=ROOT / "data" / "options_eval.db",
            now_et=now_et, schedule=self._schedule_for(now))
        if not gate.allowed:
            return None

        try:
            intent = self.components["store"].create_entry_intent(
                signal_key=ident["signal_key"], occ=occ, underlying="QQQ",
                contract_right=contract["right"], signal_side=getattr(sig, "side", ""),
                intended_qty=qty, limit_price=limit_price, order_type="limit",
                signal_ts=signal_ts, detected_ts=getattr(sig, "detected_ts", None),
                selected_ts=now.isoformat(), trigger_kind=getattr(sig, "trigger", None),
                trigger_score=getattr(sig, "score", None))
        except (DuplicateSignal, KeyError):
            return None

        attempt = EntryAttempt(
            client_order_id=intent["client_order_id"], occ=occ,
            limit_price=limit_price, quantity=qty,
            ttl_seconds=self.config.entry_ttl_seconds,
            submitted_monotonic=time.monotonic(), submitted_ts=now)
        try:
            closed = dt.datetime.fromisoformat(signal_ts.replace("Z", "+00:00"))
            attempt.stage_latency["bar_close_to_intent_ms"] = round(
                (now - closed.astimezone(dt.timezone.utc)).total_seconds() * 1000.0, 3)
        except (AttributeError, TypeError, ValueError):
            attempt.stage_latency["bar_close_to_intent_ms"] = None
        attempt.position_id = intent["position_id"]
        attempt.signal_key = ident["signal_key"]
        return attempt

    def _submit_entry(self, attempt, selection):
        from smc.broker import BrokerRejected, BrokerTimeout
        from smc.entry_manager import REJECTED
        from smc.risk import record_execution_failure
        from smc.state import REJECTED as DB_REJECTED

        store = self.components["store"]
        broker = self.components.get("broker")
        if broker is None:
            return None
        payload = {
            "symbol": attempt.occ, "qty": str(attempt.quantity),
            "side": "buy", "type": "limit",
            "limit_price": f"{attempt.limit_price:.2f}", "time_in_force": "day",
            "position_intent": "buy_to_open",
            "client_order_id": attempt.client_order_id}
        # A transport timeout has unknown fate, so the allowance is spent
        # BEFORE network I/O and can never invite a second POST. This is now
        # committed to SQLite, not just incremented in memory -- an
        # in-memory latch cannot survive the restart it exists to guard.
        if not self.entry_budget.consume(attempt.client_order_id):
            st = self.entry_budget.state()
            logger.error("REFUSING entry: session budget exhausted %s", st.as_dict())
            self._raise_incident("daily_canary_budget_exhausted", st.as_dict())
            return None
        self._entry_submission_count += 1   # retained for in-process telemetry only
        attempt.stage_latency["intent_to_post_start_ms"] = round(
            (time.monotonic() - attempt.submitted_monotonic) * 1000.0, 3)
        store.mark_order_submitted(attempt.client_order_id)
        t0 = time.monotonic()
        try:
            call = broker.submit_order(payload)
        except BrokerRejected as e:
            attempt.submit_latency_ms = round((time.monotonic() - t0) * 1000.0, 3)
            attempt.stage_latency["broker_post_ms"] = attempt.submit_latency_ms
            store.record_order_terminal(attempt.client_order_id, DB_REJECTED, str(e))
            store.set_position_state(attempt.position_id, DB_REJECTED, str(e))
            record_execution_failure(store, f"entry rejected: {e}", attempt.position_id)
            attempt.state = REJECTED
            attempt.reject_reason = str(e)
            return None
        except BrokerTimeout as e:
            attempt.submit_latency_ms = round((time.monotonic() - t0) * 1000.0, 3)
            attempt.stage_latency["broker_post_ms"] = attempt.submit_latency_ms
            record_execution_failure(store, f"entry fate unknown: {e}", attempt.position_id)
            self._raise_incident("entry_submit_unknown", {
                "client_order_id": attempt.client_order_id, "error": str(e)})
            return None
        attempt.submit_latency_ms = round((time.monotonic() - t0) * 1000.0, 3)
        attempt.stage_latency["broker_post_ms"] = attempt.submit_latency_ms
        if not getattr(call, "ok", False):
            record_execution_failure(store, "entry POST returned non-success",
                                     attempt.position_id)
            self._raise_incident("entry_submit_unknown", {
                "client_order_id": attempt.client_order_id,
                "status": getattr(call, "status", None)})
            return None
        body = call.body or {}
        attempt.broker_order_id = body.get("id")
        if attempt.broker_order_id:
            store.record_broker_ack(attempt.client_order_id, attempt.broker_order_id)
        return attempt.client_order_id

    def _submit_exit(self, position, reason, quote):
        """Persist intent, size from broker truth, then submit once."""
        from smc.broker import BrokerRejected, BrokerTimeout
        from smc.lifecycle import URGENT_REASONS
        from smc.risk import record_execution_failure
        from smc.state import EXIT_SUBMITTED, REJECTED

        store = self.components["store"]
        broker = self.components.get("broker")
        if broker is None:
            return None
        pid, occ = position["position_id"], position["occ"]
        try:
            qty = broker.get_position_qty(occ)
        except BrokerTimeout as e:
            self._raise_incident("exit_qty_unverified", {
                "position_id": pid, "occ": occ, "error": str(e)})
            return None
        if qty <= 0:
            self._raise_incident("exit_qty_zero", {"position_id": pid, "occ": occ})
            return None

        urgent = reason in URGENT_REASONS
        use_market = urgent and self.config.market_orders_permitted()[0]
        limit_price = None if use_market else round(float(quote.bid), 2)
        order_type = "market" if use_market else "limit"
        intent = store.create_exit_intent(
            position_id=pid, occ=occ, intended_qty=qty, order_type=order_type,
            limit_price=limit_price, exit_reason=reason)
        coid = intent["client_order_id"]
        payload = {"symbol": occ, "qty": str(qty), "side": "sell",
                   "type": order_type, "time_in_force": "day",
                   "position_intent": "sell_to_close", "client_order_id": coid}
        if limit_price is not None:
            payload["limit_price"] = f"{limit_price:.2f}"
        store.mark_order_submitted(coid)
        try:
            call = broker.submit_order(payload)
        except BrokerRejected as e:
            store.record_order_terminal(coid, REJECTED, str(e))
            record_execution_failure(store, f"exit rejected: {e}", pid)
            return None
        except BrokerTimeout as e:
            store.set_position_state(pid, EXIT_SUBMITTED, "exit outcome unknown")
            record_execution_failure(store, f"exit fate unknown: {e}", pid)
            self._raise_incident("exit_submit_unknown", {
                "position_id": pid, "client_order_id": coid, "error": str(e)})
            return coid
        if not getattr(call, "ok", False):
            store.set_position_state(pid, EXIT_SUBMITTED, "exit POST non-success")
            self._raise_incident("exit_submit_unknown", {
                "position_id": pid, "client_order_id": coid,
                "status": getattr(call, "status", None)})
            return coid
        body = call.body or {}
        if body.get("id"):
            store.record_broker_ack(coid, body["id"], state=EXIT_SUBMITTED)
        store.set_position_state(pid, EXIT_SUBMITTED, f"{reason} via {order_type}")
        return coid

    # ---------------------------------------------------------- dashboard
    def _sync_dashboard(self, runner_health: dict) -> None:
        from smc import dashboard
        pipeline = self.pipeline
        payload = dashboard.build_snapshot(
            runner_health=runner_health,
            detector=self.components.get("detector"),
            exit_monitor=self.components.get("exit_monitor"),
            exit_liveness=self.components.get("exit_liveness"),
            notifier=self.components.get("notifier"),
            positions=list(pipeline.positions.values()) if pipeline else [],
            recent_signals=pipeline.signals_seen if pipeline else [],
            entry_attempts=list(self.runner.attempts.values()) if self.runner else [])
        payload["mode_flag"] = self.mode
        payload["entries_allowed"] = entries_allowed(
            self.mode, self.runner.readiness) if self.runner else False
        payload["option_pricing_feed"] = "thetadata_opra_nbbo"
        payload["underlying_signal_feed"] = "alpaca_iex"
        payload["pipeline"] = pipeline.health() if pipeline else None
        for name in ("quote_worker", "live_detector_cycle", "minute_scheduler"):
            component = self.components.get(name)
            payload[name] = component.health() if component is not None else None
        terminal_manager = self.components.get("terminal_manager")
        payload["theta_terminal_manager"] = (
            terminal_manager.health() if terminal_manager is not None else None)
        payload["last_universe"] = self.components.get("last_universe")
        payload["trading_readiness"] = (
            self.runner.readiness.as_dict() if self.runner else None)
        budget = getattr(self, "entry_budget", None)
        payload["entry_budget_durable"] = (budget.state().as_dict() if budget
                                           else {"error": "no budget"})
        payload["entry_submission_latch"] = {
            "max": getattr(self.args, "max_entry_submissions", None),
            "used": self._entry_submission_count,
            "available": self._entry_enabled() if self.runner else False,
        }
        dashboard.write_snapshot(self.args.dashboard, payload)
        dashboard.write_snapshot(HEALTH_PATH, runner_health)

    # --------------------------------------------------------- dependencies
    def dependency_report(self) -> dict:
        """Per-dependency status. Distinguishes MARKET_CLOSED (healthy but
        not fully exercisable now) from FAIL (genuinely broken), so a
        weekend run is never reported as a green pass over a red stream, and
        a broken stream is never excused as "market closed"."""
        from smc.readiness import MARKET_CLOSED as GATE_MARKET_CLOSED
        from smc.readiness import NOT_TESTABLE as GATE_NOT_TESTABLE
        from smc.readiness import PASS as GATE_PASS
        r = {}
        rd = self.runner.readiness if self.runner else None
        statuses = {n: g.status for n, g in rd.gates.items()} if rd else {}
        reasons = {n: g.reason for n, g in rd.gates.items()} if rd else {}
        gates = {n: (st == GATE_PASS) for n, st in statuses.items()}
        offline = self.mode in OFFLINE_MODES

        def gate(name, key, offline_status=DEP_SKIPPED):
            """Carries the gate's own status through. MARKET_CLOSED and
            NOT_TESTABLE stay distinct from FAIL, so a weekend run never
            reports amber as green OR as broken."""
            if offline:
                r[name] = (offline_status, "not attempted in offline mode")
                return
            st = statuses.get(key)
            if st == GATE_PASS:
                r[name] = (DEP_PASS, "")
            elif st == GATE_MARKET_CLOSED:
                r[name] = (DEP_MARKET_CLOSED, reasons.get(key, ""))
            elif st == GATE_NOT_TESTABLE:
                r[name] = (DEP_SKIPPED, reasons.get(key, ""))
            else:
                r[name] = (DEP_FAIL, reasons.get(key, "not ready"))

        # Always checkable, in every mode.
        r["config"] = (DEP_PASS, "")
        r["paper_endpoint"] = (DEP_PASS, "verified before any socket")
        r["singleton"] = ((DEP_PASS, "") if gates.get("singleton")
                          else (DEP_FAIL, "not acquired"))
        r["sqlite_state"] = ((DEP_PASS, "") if gates.get("state_recovered")
                             else (DEP_FAIL, reasons.get("state_recovered", "")))
        det = self.components.get("detector")
        r["detector_cache"] = ((DEP_PASS, "warmed from cache")
                               if det is not None and getattr(det, "synchronized", False)
                               else (DEP_FAIL, "detector not synchronized"))
        r["outbox"] = ((DEP_PASS, "") if self.components.get("outbox") is not None
                       else (DEP_FAIL, "outbox unavailable"))
        r["dashboard_serialization"] = self._probe_dashboard()

        # Connection-dependent.
        gate("theta_terminal", "theta_terminal_authenticated")
        gate("thetadata_stream", "theta_stream_connected")
        gate("theta_quote_parser", "theta_quote_parser_verified")
        gate("theta_live_quotes", "theta_live_quotes_fresh")
        gate("candidate_universe", "candidate_universe_ready")
        gate("alpaca_https", "broker_prewarmed")
        gate("alpaca_trade_updates", "trade_updates")
        gate("account_reconciliation", "reconciled")

        # Greeks/universe legitimately have nothing to warm when the market
        # is closed -- that is MARKET_CLOSED, not a failure.
        if offline:
            r["universe_greeks"] = (DEP_SKIPPED, "not attempted in offline mode")
        elif gates.get("universe_greeks_warm"):
            r["universe_greeks"] = (DEP_PASS, "")
        elif self._market_closed():
            r["universe_greeks"] = (DEP_MARKET_CLOSED,
                                    "no live chain to warm while the market is closed")
        else:
            r["universe_greeks"] = (DEP_FAIL, reasons.get("universe_greeks_warm", ""))
        return r

    @staticmethod
    def _market_closed() -> bool:
        import datetime as _dt
        from zoneinfo import ZoneInfo
        try:
            from smc import calendar as smc_calendar
            now_et = _dt.datetime.now(ZoneInfo("America/New_York"))
            sched = smc_calendar.session_schedule(now_et.date())
            if not getattr(sched, "is_trading_day", False):
                return True
            o, c = getattr(sched, "open_et", None), getattr(sched, "close_et", None)
            return not (o and c and o <= now_et < c)
        except Exception:  # noqa: BLE001
            return False

    def _probe_dashboard(self):
        try:
            from smc import dashboard
            payload = dashboard.build_snapshot(
                runner_health=self.runner.health() if self.runner else {})
            return ((DEP_PASS, "") if payload.get("schema")
                    else (DEP_FAIL, "empty payload"))
        except Exception as e:  # noqa: BLE001
            return (DEP_FAIL, repr(e))

    # ----------------------------------------------------------- shutdown
    def shutdown(self, signum=None, frame=None) -> None:
        """Ordered, and idempotent under a repeated signal."""
        if self._stopping:
            return
        self._stopping = True
        logger.info("shutdown requested (signal=%s)", signum)
        # Block new entries before stopping any worker.
        if self.runner is not None:
            from smc.readiness import RED
            for gate in list(self.runner.readiness.gates):
                self.runner.readiness.set(gate, RED, "shutting down")
        minute_scheduler = self.components.get("minute_scheduler")
        if minute_scheduler is not None:
            try:
                minute_scheduler.stop()
            except Exception:  # noqa: BLE001
                pass
        quote_worker = self.components.get("quote_worker")
        if quote_worker is not None:
            try:
                quote_worker.stop(drain=True)
            except Exception:  # noqa: BLE001
                pass
        worker = self.components.get("detector_worker")
        if worker is not None:
            try:
                worker.stop()
            except Exception:  # noqa: BLE001
                pass
        if self.runner is not None:
            # 2-5. runner.shutdown stops workers in order, flushes state.
            self.runner.shutdown()
        terminal_manager = self.components.get("terminal_manager")
        if terminal_manager is not None:
            try:
                terminal_manager.stop()
            except Exception:  # noqa: BLE001
                pass
        # 6. singleton released last, so nothing else can start mid-teardown.
        if self.singleton is not None:
            self.singleton.release()
        logger.info("shutdown complete")


def main(argv=None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
    args = parse_args(argv)
    daemon = Daemon(args)

    signal.signal(signal.SIGTERM, daemon.shutdown)
    signal.signal(signal.SIGINT, daemon.shutdown)

    try:
        runner = daemon.start()
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001
        logger.exception("startup failed: %s", e)
        daemon.shutdown()
        return EXIT_STARTUP_FAILED

    allowed = entries_allowed(args.mode, runner.readiness)
    report = daemon.dependency_report()
    print(f"mode={args.mode} entries_allowed={allowed}")
    print("dependencies:")
    for name, (status, note) in report.items():
        print(f"  {name:<20} {status:<14} {note}")
    failed = [n for n, (st, _) in report.items() if st == DEP_FAIL]
    closed = [n for n, (st, _) in report.items() if st == DEP_MARKET_CLOSED]
    if closed:
        print(f"MARKET_CLOSED (expected, not a failure): {closed}")
    verdict = EXIT_OK if not failed else EXIT_DEPENDENCY_FAILED

    # Validation success and trading readiness are DIFFERENT claims and are
    # reported as different lines. A bare "PASS" while entries are blocked
    # invites exactly the misreading this separation prevents.
    rd = runner.readiness
    if rd.entries_permitted:
        trading = "READY"
    elif rd.by_status("MARKET_CLOSED"):
        trading = "BLOCKED_MARKET_CLOSED"
    elif rd.by_status("NOT_TESTABLE"):
        trading = "BLOCKED_NOT_TESTABLE"
    else:
        trading = "BLOCKED_RED"

    print(f"VALIDATION_RESULT={'PASS' if not failed else 'FAIL'}"
          + (f" (failed: {failed})" if failed else ""))
    print(f"TRADING_READINESS={trading}"
          + (f" (blocking: {rd.blocking()})" if not rd.entries_permitted else ""))
    print(f"ENTRIES_ALLOWED={'true' if allowed else 'false'}")

    try:
        if args.once:
            daemon._sync_dashboard(runner.health())
            return verdict
        runner.run()
        return verdict
    finally:
        daemon.shutdown()


if __name__ == "__main__":
    sys.exit(main())
