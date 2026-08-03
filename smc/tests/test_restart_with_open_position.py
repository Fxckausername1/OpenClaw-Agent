"""Restart while holding a PAPER position.

The scenario the daemon had never been through: the process dies with a filled
position open, systemd restarts it, and the new process has to pick the
position back up from durable state alone.

What is proven here, in heff's words:

    * broker reconciliation runs BEFORE entries are permitted
    * the position is rebuilt and MANAGED (its stop actually fires)
    * no duplicate entry or exit is submitted
    * Telegram and dashboard resume from durable state

WHY THIS FAILED BEFORE. Two independent gaps, either one sufficient to abandon
a live position:

  1. `PaperRunner.reconcile()` checked only that the broker answered and
     returned True. The real reconciliation (smc/reconcile.py) was reachable
     only from smc/pipeline.py, which the daemon does not use. The
     `reconciled` gate therefore went green having adopted nothing.
  2. `pipeline.positions` was populated ONLY by a fill event arriving in the
     current process. Nothing rehydrated it, so after a restart the exit
     monitor iterated an empty list -- an open position with every gate green
     and no stop being evaluated.

"Restart" here means what it means in production: the old store/outbox/queue
objects are closed and thrown away, and a brand-new set is opened against the
SAME database file. Nothing is carried across in memory.
"""
from __future__ import annotations

import datetime as dt
import sqlite3
import tempfile
import unittest
from pathlib import Path

from smc import dashboard
from smc import lifecycle_events as lc
from smc.assembly import build_pipeline
from smc.exit_liveness import ExitLivenessTracker
from smc.exit_monitor import StreamingExitMonitor
from smc.notify_outbox import STATE_PENDING, NotifyOutbox
from smc.notify_queue import PAPER_PREFIX, NotifyQueue
from smc.readiness import GATES, PASS
from smc.run_daemon import Daemon
from smc.runner import PaperRunner
from smc.state import OPEN, SmcStateStore

OCC = "QQQ260803C00580000"
SIGNAL_KEY = "QQQ|1Min|2026-08-03T13:32:00+00:00|long|MSS"


class FakeTelegram:
    def __init__(self, ok=True):
        self.sent = []
        self.ok = ok

    def __call__(self, message, config):
        if not self.ok:
            return False
        self.sent.append(message)
        return True

    def kinds(self):
        return [m[len(PAPER_PREFIX):].strip().split(":", 1)[0] for m in self.sent]


class SQ:
    """A ThetaData stream quote for the held contract."""

    def __init__(self, occ=OCC, bid=0.70, ask=0.74, age=0.1, generation=3):
        self.occ, self.bid, self.ask = occ, bid, ask
        self.bid_size = self.ask_size = 10
        self._age, self.generation = age, generation
        self.exchange_ts = dt.datetime(2026, 8, 3, 15, 0, tzinfo=dt.timezone.utc)

    def age_seconds(self):
        return self._age


class RestartBroker:
    """Alpaca as it looks to the RESTARTED process: still holding the
    position, no working orders. Records everything submitted so a duplicate
    is impossible to miss."""

    prewarmed = True

    def __init__(self, qty=1, occ=OCC):
        self.qty, self.occ = qty, occ
        self.submitted = []
        self.cancelled = []

    # --- fast-path shape (BrokerCall envelopes) ---
    def positions(self):
        body = ([{"symbol": self.occ, "qty": str(self.qty),
                  "asset_class": "us_option"}] if self.qty else [])
        return type("C", (), {"ok": True, "status": 200, "body": body})()

    def open_orders(self):
        return type("C", (), {"ok": True, "status": 200, "body": []})()

    def get_order_by_client_id(self, coid):
        for payload in self.submitted:
            if payload.get("client_order_id") == coid:
                return type("C", (), {"ok": True, "status": 200, "body": {
                    "id": "brk-x", "client_order_id": coid,
                    "symbol": payload["symbol"], "status": "filled",
                    "filled_qty": payload["qty"], "qty": payload["qty"],
                    "filled_avg_price": "0.70"}})()
        return type("C", (), {"ok": False, "status": 404, "body": None})()

    def get_position_qty(self, occ):
        return self.qty if occ == self.occ else 0

    def submit_order(self, payload):
        self.submitted.append(payload)
        return type("C", (), {"ok": True, "status": 200, "latency_ms": 9.0,
                              # Distinct from the seeded entry's "brk-1":
                              # smc_orders.broker_order_id is UNIQUE.
                              "body": {"id": f"brk-session2-{len(self.submitted)}",
                                       "status": "new"}})()

    def cancel_order(self, oid):
        self.cancelled.append(oid)
        return type("C", (), {"ok": True, "status": 200})()

    def sells(self):
        return [p for p in self.submitted if p.get("side") == "sell"]

    def buys(self):
        return [p for p in self.submitted if p.get("side") == "buy"]


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
    # The risk gate reads all of these.
    entry_window_minutes = 60
    max_entries_per_window = 5
    max_concurrent_positions = 2
    max_consecutive_losses = 3
    max_correlated_qqq_contracts = 4
    max_daily_realized_loss = 100.0
    max_execution_failures = 5
    max_open_premium_at_risk = 500.0

    def market_orders_permitted(self):
        return False, "limit only in tests"


def sched(now=None):
    from zoneinfo import ZoneInfo

    from smc.calendar import SessionSchedule
    et = ZoneInfo("America/New_York")
    n = dt.datetime.now(dt.timezone.utc).astimezone(et)
    return SessionSchedule(session_date=n.date(), is_trading_day=True,
                           open_et=n - dt.timedelta(hours=1),
                           close_et=n + dt.timedelta(hours=6),
                           is_early_close=False, source="test", degraded=False)


def make_dashboard_db(path: Path) -> Path:
    """The shared strategy ledger reconciliation reads to establish FOREIGN
    ownership of a contract. If it is unreadable, reconcile correctly refuses
    to attribute broker exposure and halts -- so a realistic restart test has
    to provide it."""
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE IF NOT EXISTS trades_ledger ("
                 "strategy_id TEXT, legs_metadata TEXT, status TEXT)")
    conn.commit()
    conn.close()
    return path


class RestartWithOpenPositionTests(unittest.TestCase):
    # ------------------------------------------------------------------ setup
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.db = self.root / "smc_state.db"
        self.dashboard_db = make_dashboard_db(self.root / "options_eval.db")
        self.broker = RestartBroker(qty=1)
        self._seed_open_position()

    def tearDown(self):
        self._tmp.cleanup()

    def _seed_open_position(self):
        """Session one: a position is opened and filled, and its lifecycle
        events are committed to the outbox but NEVER delivered -- the process
        died before the notify worker drained them."""
        store = SmcStateStore(self.db)
        outbox = NotifyOutbox(store.conn)
        nq = NotifyQueue(Cfg(), outbox, sender=FakeTelegram())
        intent = store.create_entry_intent(
            signal_key=SIGNAL_KEY, occ=OCC, underlying="QQQ", contract_right="C",
            signal_side="long", intended_qty=1, limit_price=1.00,
            order_type="limit",
            signal_ts=dt.datetime.now(dt.timezone.utc).isoformat())
        self.coid = intent["client_order_id"]
        self.position_id = intent["position_id"]
        store.mark_order_submitted(self.coid)
        nq.publish(lc.ENTRY_SUBMITTED,
                   lc.format_message(lc.ENTRY_SUBMITTED, "pre-crash"))
        store.record_broker_ack(self.coid, "brk-1")
        nq.publish(lc.BROKER_ACK, lc.format_message(lc.BROKER_ACK, "pre-crash"))
        store.record_order_fill(self.coid, 1, 1.00, OPEN)
        store.record_entry_filled(self.position_id, 1, 1.00, True)
        nq.publish(lc.ORDER_FILL,
                   lc.format_message(lc.ORDER_FILL, "filled 1 @ 1.00 pre-crash"))
        self.pre_crash_kinds = [r["kind"] for r in outbox.conn.execute(
            "SELECT kind FROM smc_notifications ORDER BY event_id")]
        store.close()          # the crash

    # -------------------------------------------------------------- restart
    def _restart(self, telegram=None):
        """Session two: an entirely new object graph over the same files."""
        self.store = SmcStateStore(self.db)
        self.addCleanup(self.store.close)
        self.outbox = NotifyOutbox(self.store.conn)
        self.telegram = telegram or FakeTelegram()
        self.notifier = NotifyQueue(Cfg(), self.outbox, sender=self.telegram)

        self.daemon = Daemon.__new__(Daemon)
        self.daemon.config = Cfg()
        self.daemon.mode = "paper-forward"
        self.daemon.components = {"store": self.store, "broker": self.broker,
                                  "notifier": self.notifier, "outbox": self.outbox,
                                  "stream": None}
        self.daemon.runner = None
        self.daemon.pipeline = None
        self.daemon._schedule_for = sched

        self.runner = PaperRunner(
            state_store=self.store, outbox=self.outbox, notifier=self.notifier,
            broker=self.broker,
            full_reconcile=self._full_reconcile,
            rebuild_positions=self.daemon._rebuild_supervised_positions)
        self.daemon.runner = self.runner

        self.liveness = ExitLivenessTracker(
            rest_lookup=lambda c: None,
            raise_incident=lambda k, d: self.notifier.publish(k, str(d), detail=d))
        self.exits = StreamingExitMonitor(
            open_positions=lambda: [], submit_exit=self._submit_exit,
            exit_config=ExitCfg(), schedule_for=sched, config=Cfg())
        self.pipeline = build_pipeline(
            runner=self.runner, exit_monitor=self.exits, exit_liveness=self.liveness,
            select_contract=lambda book, sig: None,
            build_universe=lambda sig: None,
            broker=self.broker, notifier=self.notifier,
            entry_builder=self.daemon._build_entry,
            entry_submitter=self.daemon._submit_entry)
        self.daemon.pipeline = self.pipeline
        return self.runner

    def _full_reconcile(self):
        from smc.broker_adapter import ReconcileBrokerAdapter
        from smc.reconcile import reconcile
        return reconcile(self.store, ReconcileBrokerAdapter(self.broker),
                         self.daemon.config, dashboard_db=self.dashboard_db)

    def _submit_exit(self, position, reason, quote):
        return self.daemon._submit_exit(position, reason, quote)

    def _drain(self):
        for _ in range(80):
            if not self.notifier.drain_once(self.outbox):
                break

    # =================================================================== tests
    def test_reconciliation_runs_before_entries_are_permitted(self):
        """The ordering requirement: `reconciled` cannot be green until the
        real reconciliation has actually run against broker truth."""
        r = self._restart()
        calls = []
        original = self._full_reconcile

        def recording():
            calls.append(r.readiness.entries_permitted)
            return original()

        r.full_reconcile = recording
        r.start(offline=False, market_closed=True)

        self.assertTrue(calls, "reconciliation never ran during startup")
        # It ran, and entries were NOT permitted at the moment it ran.
        self.assertFalse(any(calls))
        self.assertEqual(r.readiness.gates["reconciled"].status, PASS,
                         r.readiness.gates["reconciled"].reason)

    def test_position_is_rebuilt_from_durable_state(self):
        r = self._restart()
        self.assertEqual(self.pipeline.positions, {})     # nothing in memory yet
        r.reconcile(reason="startup")
        self.assertIn(self.position_id, self.pipeline.positions)
        rebuilt = self.pipeline.positions[self.position_id]
        self.assertEqual(rebuilt["occ"], OCC)
        self.assertEqual(rebuilt["qty"], 1)
        self.assertEqual(rebuilt["entry_fill_price"], 1.00)

    def test_rebuilt_position_keeps_its_original_opened_at(self):
        """The time-stop clock must not restart with the process. Resetting it
        would silently extend every time-stop past its intended horizon."""
        r = self._restart()
        r.reconcile(reason="startup")
        opened = self.pipeline.positions[self.position_id]["opened_at"]
        row = self.store.get_position(self.position_id)
        self.assertIsNotNone(row["entry_filled_ts"])
        self.assertEqual(opened.isoformat(),
                         dt.datetime.fromisoformat(row["entry_filled_ts"]).isoformat())

    def test_rebuilt_position_is_actually_managed_by_the_exit_monitor(self):
        """Rebuilt is not the claim -- MANAGED is. A stop-triggering quote must
        produce a real exit order."""
        r = self._restart()
        r.reconcile(reason="startup")
        rec = self.pipeline.process_exit_quote(SQ(bid=0.70))   # -30% vs 1.00 entry
        self.assertIsNotNone(rec, "no exit decision for a rebuilt position")
        self.assertEqual(rec.reason, "STOP")
        self.assertTrue(rec.submitted)
        self.assertEqual(len(self.broker.sells()), 1)

    def test_no_duplicate_exit_is_submitted(self):
        """A second stop quote while an exit is already working must not
        produce a second sell -- the 07-31 re-pricing ladder."""
        r = self._restart()
        r.reconcile(reason="startup")
        self.pipeline.process_exit_quote(SQ(bid=0.70))
        for _ in range(5):
            self.pipeline.process_exit_quote(SQ(bid=0.68))
        self.assertEqual(len(self.broker.sells()), 1)
        self.assertGreater(self.exits.health()["suppressed_inflight"], 0)

    def test_repeated_reconciliation_does_not_duplicate_supervision(self):
        r = self._restart()
        for _ in range(5):
            r.reconcile(reason="periodic")
        self.assertEqual(len(self.pipeline.positions), 1)

    def test_no_duplicate_entry_for_the_same_signal(self):
        """The pre-crash signal must not be able to open a SECOND position.
        The durable UNIQUE signal_key is what stops it, so this survives the
        loss of all in-memory dedup state."""
        from smc.state import DuplicateSignal

        self._restart()
        with self.assertRaises(DuplicateSignal):
            self.store.create_entry_intent(
                signal_key=SIGNAL_KEY, occ=OCC, underlying="QQQ",
                contract_right="C", signal_side="long", intended_qty=1,
                limit_price=1.00, order_type="limit",
                signal_ts=dt.datetime.now(dt.timezone.utc).isoformat())
        self.assertEqual(len(self.broker.buys()), 0)

    def test_replayed_signal_is_rejected_and_announced_not_silently_dropped(self):
        """Driving the same signal through the REAL entry builder after a
        restart must refuse it, and say why."""
        r = self._restart()
        r.reconcile(reason="startup")

        class Selection:
            found = True
            contract = {"strike": 580.0, "right": "C", "ask": 0.94, "bid": 0.90,
                        "delta": 0.35, "expiration": dt.date(2026, 8, 3)}

        class Sig:
            side = "long"
            trigger = "MSS"

        ident = {"signal_key": SIGNAL_KEY,
                 "bar_close_utc": dt.datetime.now(dt.timezone.utc).isoformat()}
        self.assertIsNone(self.daemon._build_entry(Sig(), Selection(), ident))
        self.assertEqual(len(self.broker.buys()), 0)
        messages = [row["message"] for row in self.outbox.conn.execute(
            "SELECT message FROM smc_notifications")]
        self.assertTrue(any(lc.SIGNAL_REJECTED in (m or "") for m in messages))

    def test_telegram_resumes_from_durable_state(self):
        """Events committed before the crash and never delivered are picked up
        by the new process and delivered in order."""
        self._restart()
        for kind in self.pre_crash_kinds:
            self.assertIn(kind, [r["kind"] for r in self.outbox.conn.execute(
                "SELECT kind FROM smc_notifications ORDER BY event_id")])
        pending_before = self.outbox.counts()[STATE_PENDING]
        self.assertGreaterEqual(pending_before, len(self.pre_crash_kinds))

        self._drain()
        delivered = self.telegram.kinds()
        self.assertEqual(delivered[:len(self.pre_crash_kinds)], self.pre_crash_kinds)
        for message in self.telegram.sent:
            self.assertTrue(message.startswith(PAPER_PREFIX))
        self.assertEqual(self.outbox.counts()[STATE_PENDING], 0)

    def test_reconciliation_result_is_announced_after_restart(self):
        r = self._restart()
        r.reconcile(reason="startup")
        self._drain()
        self.assertIn(lc.RECONCILIATION_RESULT, self.telegram.kinds())

    def test_dashboard_resumes_from_durable_state(self):
        r = self._restart()
        r.reconcile(reason="startup")
        payload = dashboard.build_snapshot(
            runner_health=r.health(), exit_monitor=self.exits,
            exit_liveness=self.liveness, notifier=self.notifier,
            positions=list(self.pipeline.positions.values()),
            lifecycle_events=self.outbox.recent_lifecycle(),
            reconciliation={"clean": True, "halt_reasons": []})
        self.assertEqual(len(payload["positions"]), 1)
        self.assertEqual(payload["positions"][0]["occ"], OCC)
        self.assertTrue(payload["lifecycle_events"])
        self.assertIsNotNone(payload["reconciliation"])
        # The pre-crash events are visible AND flagged as not yet delivered.
        self.assertTrue(payload["lifecycle_undelivered"])

    def test_broker_flat_but_local_open_blocks_entries_and_keeps_supervising(self):
        """The dangerous disagreement: we think we hold it, the broker says
        flat. Entries must halt, and the position must NOT be quietly dropped."""
        self.broker = RestartBroker(qty=0)
        r = self._restart()
        ok = r.reconcile(reason="startup")
        self.assertFalse(ok)
        r.readiness.set_bool("reconciled", ok)
        self.assertFalse(r.readiness.entries_permitted)
        self.assertIn(self.position_id,
                      [p["position_id"] for p in self.store.open_positions()])
        self._drain()
        self.assertIn(lc.RECONCILIATION_MISMATCH, self.telegram.kinds())

    def test_unreadable_broker_is_not_treated_as_flat(self):
        """A failed position read must never look like 'no positions' -- that
        is exactly how an open position becomes invisible."""
        from smc.broker import BrokerTimeout
        from smc.broker_adapter import ReconcileBrokerAdapter

        class Unreadable(RestartBroker):
            def positions(self):
                return type("C", (), {"ok": False, "status": 500,
                                      "body": None, "error": "boom"})()

        adapter = ReconcileBrokerAdapter(Unreadable())
        with self.assertRaises(BrokerTimeout):
            adapter.list_option_positions()

    def test_entries_stay_blocked_until_every_gate_including_recon_is_green(self):
        r = self._restart()
        r.start(offline=False, market_closed=True)
        self.assertFalse(r.readiness.entries_permitted)
        for gate in GATES:
            r.readiness.set_bool(gate, True)
        self.assertTrue(r.readiness.entries_permitted)


if __name__ == "__main__":
    unittest.main()
