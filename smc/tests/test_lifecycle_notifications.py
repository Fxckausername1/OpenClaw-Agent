"""Telegram lifecycle delivery: ordering, prefixing, durability, recovery.

heff's requirement, restated as the properties under test:

    signal detected -> rejected/accepted -> contract selected -> intent
    persisted -> entry submitted -> broker ack -> partial/full fill ->
    cancel/reject/unknown -> exit trigger -> exit submitted -> exit fill ->
    reconciliation result

must all be emitted, through the canonical durable outbox, delivered IN
ORDER, PAPER-prefixed, persisted before delivery, recovered across a restart,
and never lost.

The sender is a fake throughout -- nothing here touches Telegram, the network
or a subprocess. The PIPELINE is real: these drive smc.assembly.build_pipeline
and the production Daemon methods, not a reimplementation of them, because a
test that emits its own events would prove only that the test can emit events.
"""
from __future__ import annotations

import datetime as dt
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from smc import events as ev
from smc import lifecycle_events as lc
from smc.assembly import build_pipeline
from smc.entry_manager import EntryAttempt
from smc.events import make_event
from smc.exit_liveness import ExitLivenessTracker
from smc.exit_monitor import StreamingExitMonitor
from smc.notify_outbox import STATE_DELIVERED, STATE_PENDING, NotifyOutbox
from smc.notify_queue import PAPER_PREFIX, NotifyQueue
from smc.readiness import GATES, PASS, RED
from smc.runner import PaperRunner
from smc.state import SmcStateStore

OCC = "QQQ260803C00580000"


class FakeTelegram:
    """Records what would have been sent, in delivery order.

    `fail_on` makes a specific message body fail so the ordering guarantee can
    be tested under partial failure -- the case where a naive drainer would
    let a later event overtake an earlier one.
    """

    def __init__(self, ok=True, fail_on=None):
        self.sent = []
        self.attempts = []
        self.ok = ok
        self.fail_on = fail_on or (lambda m: False)

    def __call__(self, message, config):
        self.attempts.append(message)
        if not self.ok or self.fail_on(message):
            return False
        self.sent.append(message)
        return True

    def kinds(self):
        """Delivered kinds, in delivery order."""
        out = []
        for m in self.sent:
            body = m[len(PAPER_PREFIX):].strip()
            out.append(body.split(":", 1)[0])
        return out


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


class SQ:
    def __init__(self, occ=OCC, bid=0.80, ask=0.84, age=0.1, generation=3):
        self.occ, self.bid, self.ask = occ, bid, ask
        self.bid_size = self.ask_size = 10
        self._age, self.generation = age, generation
        self.exchange_ts = dt.datetime(2026, 8, 3, 14, 30, tzinfo=dt.timezone.utc)

    def age_seconds(self):
        return self._age


class Sig:
    session = "2026-08-03"
    side = "long"
    trigger = "MSS"
    bar_timeframe = "1Min"
    bar_timestamp = "2026-08-03T13:32:00+00:00"


class Selection:
    found = True
    reason = None
    contract = {"strike": 580.0, "right": "C", "ask": 0.94, "bid": 0.90,
                "delta": 0.35, "expiration": dt.date(2026, 8, 3),
                "spread_pct_mid": 0.04}


class FakeBroker:
    prewarmed = True

    def __init__(self):
        self.submitted, self.cancelled = [], []

    def submit_order(self, payload):
        self.submitted.append(payload)
        return type("C", (), {"ok": True, "latency_ms": 11.7,
                              "body": {"id": f"brk-{len(self.submitted)}",
                                       "status": "new"}})()

    def cancel_order(self, oid):
        self.cancelled.append(oid)
        return type("C", (), {"ok": True, "status": 200})()

    def open_orders(self):
        return type("C", (), {"ok": True, "status": 200, "body": []})()

    def positions(self):
        return type("C", (), {"ok": True, "status": 200, "body": []})()


class ExitCfg:
    target_return = 0.25
    premium_stop_pct = -0.20
    time_stop_minutes = 30


class Cfg:
    max_quote_age_seconds = 10.0
    forced_close_buffer_minutes = 30
    early_close_flatten_buffer_minutes = 15
    notifications_enabled = True
    telegram_target = "x"
    telegram_timeout_seconds = 5.0
    max_signal_age_seconds = 90.0


def sched(now=None):
    from zoneinfo import ZoneInfo

    from smc.calendar import SessionSchedule
    et = ZoneInfo("America/New_York")
    n = dt.datetime.now(dt.timezone.utc).astimezone(et)
    return SessionSchedule(session_date=n.date(), is_trading_day=True,
                           open_et=n - dt.timedelta(hours=1),
                           close_et=n + dt.timedelta(hours=6),
                           is_early_close=False, source="test", degraded=False)


# ===================================================================== chain
class LifecycleChainTests(unittest.TestCase):
    """Drives the assembled pipeline and inspects what Telegram received."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Path(self._tmp.name) / "smc.db"
        self.store = SmcStateStore(self.db)
        self.outbox = NotifyOutbox(self.store.conn)
        self.telegram = FakeTelegram()
        self.notifier = NotifyQueue(Cfg(), self.outbox, sender=self.telegram)
        self.broker = FakeBroker()

        self.runner = PaperRunner(state_store=self.store, outbox=self.outbox,
                                  notifier=self.notifier, broker=self.broker)
        for g in GATES:
            self.runner.readiness.set_bool(g, True)

        self.liveness = ExitLivenessTracker(
            rest_lookup=lambda c: None,
            raise_incident=lambda k, d: self.notifier.publish(k, str(d), detail=d))
        self.exits = StreamingExitMonitor(
            open_positions=lambda: [], submit_exit=self._submit_exit,
            exit_config=ExitCfg(), schedule_for=sched, config=Cfg())
        self.pipeline = build_pipeline(
            runner=self.runner, exit_monitor=self.exits, exit_liveness=self.liveness,
            select_contract=lambda book, sig: Selection(),
            build_universe=lambda sig: None,
            broker=self.broker, notifier=self.notifier,
            entry_builder=self._build_entry, entry_submitter=self._submit_entry)

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    # ---------------------------------------------------------- collaborators
    def _build_entry(self, sig, selection, ident):
        return EntryAttempt(
            client_order_id=f"coid-{ident['signal_key'][:8]}", occ=OCC,
            limit_price=selection.contract["ask"], quantity=2, ttl_seconds=20.0,
            submitted_monotonic=time.monotonic(),
            submitted_ts=dt.datetime.now(dt.timezone.utc))

    def _submit_entry(self, attempt, selection):
        call = self.broker.submit_order({
            "symbol": attempt.occ, "qty": "1", "side": "buy", "type": "limit",
            "limit_price": f"{attempt.limit_price:.2f}",
            "client_order_id": attempt.client_order_id})
        attempt.submit_latency_ms = call.latency_ms
        attempt.broker_order_id = call.body["id"]
        return attempt.client_order_id

    def _submit_exit(self, position, reason, quote):
        coid = f"exit-{position['position_id']}"
        self.broker.submit_order({"symbol": position["occ"], "qty": "1",
                                  "side": "sell", "type": "limit",
                                  "client_order_id": coid})
        return coid

    # ---------------------------------------------------------------- helpers
    def _committed(self):
        return [r["kind"] for r in self.outbox.conn.execute(
            "SELECT kind FROM smc_notifications ORDER BY event_id")]

    def _drain(self):
        for _ in range(60):
            if not self.notifier.drain_once(self.outbox):
                break

    def _run_full_chain(self):
        from smc.signal_identity import make_signal_identity
        ident = make_signal_identity(symbol="QQQ", timeframe="1Min",
                                     bar_open=Sig.bar_timestamp, side="long",
                                     trigger="MSS")
        self.runner.bus.publish(make_event(
            ev.EV_SIGNAL, signal_id=ident.signal_key,
            payload={"signal": Sig(), "identity": ident.as_dict()}))
        self.runner.run(max_events=1, idle_timeout=0.2)
        coid = self.broker.submitted[0]["client_order_id"]

        # broker acknowledgment, from the trade_updates stream
        self.runner.bus.publish(make_event(
            ev.EV_ORDER_ACK, client_order_id=coid, order_id="brk-1",
            payload={"event": "new"}))
        self.runner.run(max_events=1, idle_timeout=0.2)

        # partial, then full fill
        self.runner.bus.publish(make_event(
            ev.EV_PARTIAL_FILL, client_order_id=coid, order_id="brk-1",
            payload={"event": "partial_fill", "price": 0.94, "filled_qty": 1}))  # 1 of 2
        self.runner.run(max_events=1, idle_timeout=0.2)
        self.runner.bus.publish(make_event(
            ev.EV_FILL, client_order_id=coid, order_id="brk-1",
            payload={"event": "fill", "price": 0.94, "filled_qty": 2}))
        self.runner.run(max_events=1, idle_timeout=0.2)

        # stop -> exit submitted -> exit filled
        self.runner.bus.publish(make_event(ev.EV_QUOTE, quote=SQ(bid=0.70)))
        self.runner.run(max_events=1, idle_timeout=0.2)
        exit_coid = self.broker.submitted[1]["client_order_id"]
        self.runner.bus.publish(make_event(
            ev.EV_FILL, client_order_id=exit_coid, order_id="brk-2",
            payload={"event": "fill", "price": 0.70, "filled_qty": 1}))
        self.runner.run(max_events=1, idle_timeout=0.2)

        # reconciliation result
        self.runner.reconcile(reason="test")
        return coid

    # ------------------------------------------------------------------ tests
    def test_every_required_stage_is_emitted(self):
        """The chain heff enumerated, link by link."""
        self._run_full_chain()
        stages = {lc.stage_of(k) for k in self._committed()}
        for required in (1, 2, 3, 5, 6, 7, 9, 10, 11, 12):
            self.assertIn(required, stages,
                          f"stage {required} never emitted; got {sorted(s for s in stages if s)}")

    def test_specific_kinds_present_for_each_link(self):
        self._run_full_chain()
        kinds = self._committed()
        for kind in (lc.SIGNAL_DETECTED, lc.SIGNAL_ACCEPTED, lc.CONTRACT_SELECTED,
                     lc.ORDER_SUBMITTED, lc.BROKER_ACK, lc.ORDER_PARTIAL_FILL,
                     lc.ORDER_FILL, lc.STOP_TRIGGERED, lc.EXIT_SUBMITTED,
                     lc.EXIT_FILLED, lc.RECONCILIATION_RESULT):
            self.assertIn(kind, kinds)

    def test_delivery_order_equals_commit_order(self):
        """The ordering guarantee. Delivered sequence must match the durable
        sequence exactly -- not merely contain the same events."""
        self._run_full_chain()
        self._drain()
        self.assertEqual(self.telegram.kinds(), self._committed())

    def test_stage_numbers_never_go_backwards_in_delivery(self):
        self._run_full_chain()
        self._drain()
        stages = [lc.stage_of(k) for k in self.telegram.kinds()]
        stages = [s for s in stages if s is not None]
        self.assertEqual(stages, sorted(stages),
                         f"chain delivered out of stage order: {stages}")

    def test_fill_never_overtakes_an_earlier_failing_event(self):
        """Head-of-line ordering under partial failure.

        A transient failure on an early event must NOT let a later one jump
        ahead: an operator seeing the fill before the submission has been told
        something untrue about the sequence.
        """
        blocked = {"on": True}
        self.telegram.fail_on = lambda m: blocked["on"] and lc.SIGNAL_ACCEPTED in m
        self._run_full_chain()
        self._drain()
        self.assertNotIn(lc.ORDER_FILL, self.telegram.kinds())
        self.assertNotIn(lc.ORDER_SUBMITTED, self.telegram.kinds())
        blocked["on"] = False
        self._drain()
        self.assertEqual(self.telegram.kinds(), self._committed())

    def test_every_delivered_message_is_paper_prefixed(self):
        self._run_full_chain()
        self._drain()
        self.assertTrue(self.telegram.sent)
        for message in self.telegram.sent:
            self.assertTrue(message.startswith(PAPER_PREFIX), message[:80])

    def test_every_event_is_durable_before_any_delivery(self):
        """Nothing is announced that is not already recorded."""
        self._run_full_chain()
        rows = self.outbox.conn.execute(
            "SELECT event_id, delivery_state FROM smc_notifications").fetchall()
        self.assertTrue(rows)
        for row in rows:
            self.assertEqual(row["delivery_state"], STATE_PENDING)
            self.assertIsNotNone(self.outbox.conn.execute(
                "SELECT 1 FROM smc_events WHERE id=?", (row["event_id"],)).fetchone())

    def test_chain_survives_restart_and_resumes_from_durable_state(self):
        """Process dies mid-chain with nothing delivered; a fresh process
        picks the obligations up and delivers them, in order."""
        self._run_full_chain()
        committed = self._committed()
        self.assertEqual(self.telegram.sent, [])          # nothing delivered yet
        self.store.close()

        store2 = SmcStateStore(self.db)
        try:
            outbox2 = NotifyOutbox(store2.conn)
            telegram2 = FakeTelegram()
            nq2 = NotifyQueue(Cfg(), outbox2, sender=telegram2)
            for _ in range(60):
                if not nq2.drain_once(outbox2):
                    break
            self.assertEqual(telegram2.kinds(), committed)
            self.assertEqual(outbox2.counts()[STATE_PENDING], 0)
        finally:
            store2.close()

    def test_no_critical_event_is_lost_under_sustained_failure(self):
        self.telegram.ok = False
        self._run_full_chain()
        critical = [k for k in self._committed() if lc.is_critical(k)]
        self.assertTrue(critical)
        for _ in range(50):
            self.notifier.drain_once(self.outbox)
        surviving = [r["kind"] for r in self.outbox.undelivered_critical()]
        self.assertEqual(sorted(surviving), sorted(critical))

    def test_no_event_is_lost_when_the_hint_queue_is_full(self):
        """A full in-memory queue defers a hint; it never drops an obligation."""
        self.notifier = NotifyQueue(Cfg(), self.outbox, sender=self.telegram, maxsize=1)
        before = len(self._committed())
        for i in range(20):
            self.notifier.publish(lc.ORDER_FILL, f"fill {i}")
        self.assertEqual(len(self._committed()), before + 20)
        self.assertGreater(self.notifier.health()["hint_deferred"], 0)

    def test_dashboard_renders_the_chain_with_delivery_state(self):
        from smc import dashboard
        self._run_full_chain()
        feed = self.outbox.recent_lifecycle()
        payload = dashboard.build_snapshot(
            runner_health=self.runner.health(), exit_monitor=self.exits,
            exit_liveness=self.liveness, notifier=self.notifier,
            positions=list(self.pipeline.positions.values()),
            lifecycle_events=feed)
        self.assertTrue(payload["lifecycle_events"])
        self.assertTrue(all(e["stage"] for e in payload["lifecycle_events"]))
        # Undelivered obligations must be visible, not merely absent.
        self.assertTrue(payload["lifecycle_undelivered"])
        self._drain()
        payload2 = dashboard.build_snapshot(
            runner_health=self.runner.health(), notifier=self.notifier,
            lifecycle_events=self.outbox.recent_lifecycle())
        self.assertEqual(payload2["lifecycle_undelivered"], [])

    def test_exit_trigger_and_exit_submit_are_separate_events(self):
        """A STOP used to announce only `stop_triggered`, so the fact that an
        exit ORDER had gone out was never reported."""
        self._run_full_chain()
        kinds = self._committed()
        self.assertIn(lc.STOP_TRIGGERED, kinds)
        self.assertIn(lc.EXIT_SUBMITTED, kinds)
        self.assertLess(kinds.index(lc.STOP_TRIGGERED), kinds.index(lc.EXIT_SUBMITTED))

    def test_partial_fill_is_distinguishable_from_a_full_fill(self):
        self._run_full_chain()
        kinds = self._committed()
        self.assertIn(lc.ORDER_PARTIAL_FILL, kinds)
        self.assertIn(lc.ORDER_FILL, kinds)


# ============================================================ daemon emitters
class DaemonEmissionTests(unittest.TestCase):
    """Stages 4, 5 and 6 are emitted by the production Daemon methods, so they
    are tested against those methods rather than a stand-in."""

    def setUp(self):
        from smc.run_daemon import Daemon
        self._tmp = tempfile.TemporaryDirectory()
        self.store = SmcStateStore(Path(self._tmp.name) / "smc.db")
        self.outbox = NotifyOutbox(self.store.conn)
        self.telegram = FakeTelegram()
        self.notifier = NotifyQueue(Cfg(), self.outbox, sender=self.telegram)
        self.broker = FakeBroker()

        self.daemon = Daemon.__new__(Daemon)
        self.daemon.config = Cfg()
        self.daemon.components = {"store": self.store, "broker": self.broker,
                                  "notifier": self.notifier, "outbox": self.outbox}
        self.daemon._entry_submission_count = 0
        self.daemon._budget_exhausted_announced = False
        from smc.entry_budget import EntryBudget
        self.daemon.entry_budget = EntryBudget(self.store.conn, max_attempts=1)

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def _kinds(self):
        return [r["kind"] for r in self.outbox.conn.execute(
            "SELECT kind FROM smc_notifications ORDER BY event_id")]

    def test_entry_submitted_is_committed_before_the_post(self):
        """The unknown-fate case: the POST times out, so we never learn the
        order's outcome -- but the fact that we TRIED must still be on record
        and announced."""
        from smc.broker import BrokerTimeout

        def timing_out(payload):
            self.assertIn(lc.ENTRY_SUBMITTED, self._kinds(),
                          "entry_submitted must be durable BEFORE the POST")
            raise BrokerTimeout("transport died")

        self.broker.submit_order = timing_out
        attempt = self._attempt()
        self.assertIsNone(self.daemon._submit_entry(attempt, Selection()))
        kinds = self._kinds()
        self.assertIn(lc.ENTRY_SUBMITTED, kinds)
        self.assertIn(lc.ENTRY_SUBMIT_UNKNOWN, kinds)

    def test_broker_ack_is_emitted_from_the_post_response(self):
        attempt = self._attempt()
        self.daemon._submit_entry(attempt, Selection())
        kinds = self._kinds()
        self.assertIn(lc.ENTRY_SUBMITTED, kinds)
        self.assertIn(lc.BROKER_ACK, kinds)
        self.assertLess(kinds.index(lc.ENTRY_SUBMITTED), kinds.index(lc.BROKER_ACK))

    def test_broker_rejection_is_announced(self):
        from smc.broker import BrokerRejected

        def rejecting(payload):
            raise BrokerRejected("403 not permitted")

        self.broker.submit_order = rejecting
        self.daemon._submit_entry(self._attempt(), Selection())
        self.assertIn(lc.ORDER_REJECTED, self._kinds())

    def test_rejection_reasons_are_specific_not_generic(self):
        """Each refusal used to return None silently; 'why did it not trade?'
        was unanswerable from Telegram."""
        ident = {"signal_key": "sk-1", "bar_close_utc": "2020-01-01T00:00:00+00:00"}
        self.daemon._schedule_for = sched
        self.assertIsNone(self.daemon._build_entry(Sig(), Selection(), ident))
        rows = self.outbox.conn.execute(
            "SELECT kind, message FROM smc_notifications ORDER BY event_id").fetchall()
        self.assertTrue(any(r["kind"] == lc.SIGNAL_REJECTED for r in rows))
        self.assertTrue(any("signal too old" in (r["message"] or "") for r in rows))

    def test_debit_cap_rejection_names_the_cap(self):
        class Rich:
            found = True
            contract = dict(Selection.contract, ask=99.0)

        ident = {"signal_key": "sk-2",
                 "bar_close_utc": dt.datetime.now(dt.timezone.utc).isoformat()}
        self.daemon._schedule_for = sched
        self.assertIsNone(self.daemon._build_entry(Sig(), Rich(), ident))
        messages = [r["message"] for r in self.outbox.conn.execute(
            "SELECT message FROM smc_notifications")]
        self.assertTrue(any("debit cap exceeded" in (m or "") for m in messages))

    def _attempt(self):
        intent = self.store.create_entry_intent(
            signal_key=f"sk-{time.time_ns()}", occ=OCC, underlying="QQQ",
            contract_right="C", signal_side="long", intended_qty=1,
            limit_price=0.94, order_type="limit",
            signal_ts=dt.datetime.now(dt.timezone.utc).isoformat())
        attempt = EntryAttempt(
            client_order_id=intent["client_order_id"], occ=OCC, limit_price=0.94,
            quantity=1, ttl_seconds=20.0, submitted_monotonic=time.monotonic(),
            submitted_ts=dt.datetime.now(dt.timezone.utc))
        attempt.position_id = intent["position_id"]
        return attempt


# ================================================================ the gate
class NotificationReadinessGateTests(unittest.TestCase):
    """Entries must wait behind a working notification path, and must resume
    on their own when it recovers."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = SmcStateStore(Path(self._tmp.name) / "smc.db")
        self.outbox = NotifyOutbox(self.store.conn)
        self.telegram = FakeTelegram()

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def _runner(self, notifier):
        r = PaperRunner(state_store=self.store, outbox=self.outbox,
                        notifier=notifier, broker=FakeBroker())
        return r

    def test_missing_notifier_blocks_entries(self):
        r = self._runner(None)
        r._gate_notifications()
        self.assertEqual(r.readiness.gates["notifications_operational"].status, RED)

    def test_notifier_that_cannot_report_health_blocks_entries(self):
        """Fail closed: 'no evidence' must not read as 'healthy'."""
        class Mute:
            def publish(self, *a, **kw):
                return 1

        r = self._runner(Mute())
        r._gate_notifications()
        self.assertEqual(r.readiness.gates["notifications_operational"].status, RED)

    def test_dead_worker_blocks_entries(self):
        nq = NotifyQueue(Cfg(), self.outbox, sender=self.telegram)
        r = self._runner(nq)
        r._gate_notifications()                      # never started
        gate = r.readiness.gates["notifications_operational"]
        self.assertEqual(gate.status, RED)
        self.assertIn("worker", gate.reason)

    def test_open_breaker_blocks_entries(self):
        nq = NotifyQueue(Cfg(), self.outbox, sender=FakeTelegram(ok=False),
                         failure_threshold=1)
        nq.start()
        self.addCleanup(nq.stop)
        nq.publish(lc.ORDER_FILL, "m")
        for _ in range(3):
            nq.drain_once(self.outbox)
        r = self._runner(nq)
        r._gate_notifications()
        gate = r.readiness.gates["notifications_operational"]
        self.assertEqual(gate.status, RED)
        self.assertIn("breaker", gate.reason)

    def test_stale_critical_backlog_blocks_entries(self):
        nq = NotifyQueue(Cfg(), self.outbox, sender=self.telegram)
        nq.start()
        self.addCleanup(nq.stop)
        eid = nq.publish(lc.ORDER_FILL, "an old unannounced fill")
        old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)).isoformat()
        self.store.conn.execute(
            "UPDATE smc_notifications SET created_ts=? WHERE event_id=?", (old, eid))
        r = self._runner(nq)
        r.max_notification_backlog_seconds = 180.0
        r._gate_notifications()
        gate = r.readiness.gates["notifications_operational"]
        self.assertEqual(gate.status, RED)
        self.assertIn("undelivered", gate.reason)

    def test_healthy_pipeline_permits_entries_and_gate_self_heals(self):
        clock = FakeClock()
        sender = FakeTelegram(ok=False)
        nq = NotifyQueue(Cfg(), self.outbox, sender=sender, failure_threshold=1,
                         cooldown_seconds=60.0, clock=clock)
        nq.start()
        self.addCleanup(nq.stop)
        nq.publish(lc.ORDER_FILL, "m")
        nq.drain_once(self.outbox)
        r = self._runner(nq)
        r._gate_notifications()
        self.assertEqual(r.readiness.gates["notifications_operational"].status, RED)

        # Telegram comes back and the cooldown elapses: no restart, no manual
        # action, the gate reopens on its own.
        sender.ok = True
        clock.advance(61)
        for _ in range(5):
            nq.drain_once(self.outbox)
        r._gate_notifications()
        self.assertEqual(r.readiness.gates["notifications_operational"].status, PASS,
                         r.readiness.gates["notifications_operational"].reason)

    def test_gate_is_in_the_entry_permission_set(self):
        self.assertIn("notifications_operational", GATES)

    def test_a_blocked_gate_does_not_stop_position_management(self):
        """The gate blocks NEW entries only. An open position must still be
        managed while notifications are down -- blocking exits would turn a
        Telegram outage into unmanaged risk."""
        nq = NotifyQueue(Cfg(), self.outbox, sender=self.telegram)
        r = self._runner(nq)
        r._gate_notifications()
        self.assertFalse(r.readiness.entries_permitted)
        exits = StreamingExitMonitor(
            open_positions=lambda: [{
                "position_id": "p1", "occ": OCC, "entry_fill_price": 1.00,
                "qty": 1, "opened_at": dt.datetime.now(dt.timezone.utc)}],
            submit_exit=lambda p, reason, q: "exit-1",
            exit_config=ExitCfg(), schedule_for=sched, config=Cfg())
        rec = exits.on_quote(SQ(bid=0.50))
        self.assertIsNotNone(rec)
        self.assertTrue(rec.submitted)


if __name__ == "__main__":
    unittest.main()
