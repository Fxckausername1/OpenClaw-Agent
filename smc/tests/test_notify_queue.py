"""Tests for smc/notify_outbox.py and smc/notify_queue.py.

unittest. No network and no subprocess: the sender is always injected, so
nothing here ever spawns the openclaw CLI.

The property under test throughout is DURABILITY: a critical trade event
must survive a full queue, an open circuit breaker, repeated delivery
failure and a restart -- while never being able to delay trading.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from smc.notify_outbox import (
    CRITICAL_KINDS, STATE_ABANDONED, STATE_DELIVERED, STATE_FAILED,
    STATE_PENDING, NotifyOutbox, classify,
)
from smc.notify_queue import PAPER_PREFIX, STATE_OPEN, NotifyQueue, with_paper_prefix
from smc.state import SmcStateStore


class Cfg:
    notifications_enabled = True
    telegram_target = "123"
    telegram_timeout_seconds = 5.0


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Path(self._tmp.name) / "smc_state.db"
        self.store = SmcStateStore(self.db)
        self.outbox = NotifyOutbox(self.store.conn)

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def q(self, sender=None, **kw):
        return NotifyQueue(Cfg(), self.outbox,
                           sender=sender or (lambda m, c: True), **kw)


class ClassificationTests(_Base):
    def test_critical_kinds_are_priority_zero_and_flagged(self):
        for kind in ("order_fill", "exit_filled", "reconciliation_mismatch",
                     "order_rejected", "stop_triggered"):
            with self.subTest(kind=kind):
                priority, critical = classify(kind)
                self.assertEqual(priority, 0)
                self.assertEqual(critical, 1)

    def test_health_is_informational(self):
        priority, critical = classify("health")
        self.assertEqual(critical, 0)
        self.assertGreater(priority, 0)

    def test_every_kind_heff_listed_is_critical(self):
        for kind in ("order_submitted", "order_rejected", "order_partial_fill",
                     "order_fill", "stop_triggered", "target_triggered",
                     "exit_submitted", "exit_filled", "reconciliation_mismatch"):
            self.assertIn(kind, CRITICAL_KINDS)


class DurabilityTests(_Base):
    def test_publish_commits_durably_before_queueing(self):
        nq = self.q()
        eid = nq.publish("order_fill", "filled 1 @ 0.93")
        self.assertIsNotNone(eid)
        row = self.outbox.get(eid)
        self.assertEqual(row["delivery_state"], STATE_PENDING)
        self.assertEqual(row["attempts"], 0)
        self.assertTrue(row["message"].startswith(PAPER_PREFIX))

    def test_canonical_event_row_also_written(self):
        nq = self.q()
        eid = nq.publish("order_fill", "filled")
        ev = self.store.conn.execute(
            "SELECT kind FROM smc_events WHERE id=?", (eid,)).fetchone()
        self.assertEqual(ev["kind"], "order_fill")

    def test_full_hint_queue_does_not_lose_the_event(self):
        """The whole point: a full in-memory queue defers a HINT, it does not
        drop an obligation."""
        nq = self.q(maxsize=1)
        ids = [nq.publish("order_fill", f"fill {i}") for i in range(5)]
        self.assertTrue(all(i is not None for i in ids))
        for eid in ids:
            self.assertEqual(self.outbox.get(eid)["delivery_state"], STATE_PENDING)
        self.assertGreater(nq.health()["hint_deferred"], 0)

    def test_dedup_by_canonical_event_id(self):
        eid = self.outbox.commit_event("order_fill", "m")
        self.outbox.conn.execute(
            "INSERT OR IGNORE INTO smc_notifications"
            "(event_id, created_ts, kind, priority, critical, message, "
            " delivery_state, attempts) VALUES(?,?,?,?,?,?,?,0)",
            (eid, "ts", "order_fill", 0, 1, "duplicate", STATE_PENDING))
        n = self.outbox.conn.execute(
            "SELECT COUNT(*) c FROM smc_notifications WHERE event_id=?", (eid,)
        ).fetchone()["c"]
        self.assertEqual(n, 1)

    def test_survives_restart_as_pending(self):
        nq = self.q()
        eid = nq.publish("exit_filled", "closed")
        self.store.close()
        store2 = SmcStateStore(self.db)
        try:
            outbox2 = NotifyOutbox(store2.conn)
            self.assertEqual(outbox2.get(eid)["delivery_state"], STATE_PENDING)
            self.assertEqual(len(outbox2.undelivered_critical()), 1)
        finally:
            store2.close()


class DeliveryStateTests(_Base):
    def test_delivered_marks_state_and_timestamps(self):
        nq = self.q()
        eid = nq.publish("order_fill", "m")
        nq.drain_once(self.outbox)
        row = self.outbox.get(eid)
        self.assertEqual(row["delivery_state"], STATE_DELIVERED)
        self.assertEqual(row["attempts"], 1)
        self.assertIsNotNone(row["delivered_ts"])
        self.assertIsNone(row["last_error"])

    def test_failure_records_attempt_count_and_last_error(self):
        nq = self.q(sender=lambda m, c: False, failure_threshold=99)
        eid = nq.publish("order_fill", "m")
        nq.drain_once(self.outbox)
        nq.drain_once(self.outbox)
        row = self.outbox.get(eid)
        self.assertEqual(row["delivery_state"], STATE_FAILED)
        self.assertEqual(row["attempts"], 2)
        self.assertIn("send returned False", row["last_error"])
        self.assertIsNotNone(row["last_attempt_ts"])

    def test_sender_exception_is_captured_not_propagated(self):
        def boom(m, c):
            raise RuntimeError("cli wedged")
        nq = self.q(sender=boom, failure_threshold=99)
        eid = nq.publish("order_fill", "m")
        nq.drain_once(self.outbox)
        self.assertIn("cli wedged", self.outbox.get(eid)["last_error"])

    def test_noncritical_abandoned_after_max_attempts(self):
        outbox = NotifyOutbox(self.store.conn, max_attempts=3)
        nq = self.q(sender=lambda m, c: False, failure_threshold=99)
        eid = outbox.commit_event("health", "heartbeat")
        for _ in range(3):
            nq.drain_once(outbox)
        self.assertEqual(outbox.get(eid)["delivery_state"], STATE_ABANDONED)

    def test_critical_is_never_abandoned_however_many_failures(self):
        """An undelivered fill notice stays an outstanding obligation."""
        outbox = NotifyOutbox(self.store.conn, max_attempts=2)
        nq = self.q(sender=lambda m, c: False, failure_threshold=99)
        eid = outbox.commit_event("order_fill", "filled")
        for _ in range(10):
            nq.drain_once(outbox)
        row = outbox.get(eid)
        self.assertEqual(row["delivery_state"], STATE_FAILED)
        self.assertGreaterEqual(row["attempts"], 10)
        self.assertEqual(len(outbox.undelivered_critical()), 1)


class PriorityTests(_Base):
    def test_critical_drains_before_informational_regardless_of_age(self):
        sent = []
        nq = self.q(sender=lambda m, c: sent.append(m) or True)
        self.outbox.commit_event("health", "OLD health")
        self.outbox.commit_event("order_fill", "NEW fill")
        nq.drain_once(self.outbox)
        self.assertIn("fill", sent[0])

    def test_coalesce_collapses_health_but_never_critical(self):
        for i in range(5):
            self.outbox.commit_event("health", f"h{i}")
        crit = [self.outbox.commit_event("order_fill", f"f{i}") for i in range(3)]
        collapsed = self.outbox.coalesce_informational(keep_latest=1)
        self.assertEqual(collapsed, 4)
        for eid in crit:
            self.assertEqual(self.outbox.get(eid)["delivery_state"], STATE_PENDING)


class BreakerTests(_Base):
    def test_breaker_opens_and_leaves_rows_pending_not_lost(self):
        nq = self.q(sender=lambda m, c: False, failure_threshold=2)
        eids = [nq.publish("order_fill", f"m{i}") for i in range(4)]
        # Two passes, not one: ordered delivery now stops a batch at the first
        # failure so a later event cannot overtake an earlier one, which means
        # one drain makes exactly one attempt. The invariant this test exists
        # for -- nothing is lost or abandoned -- is unchanged.
        nq.drain_once(self.outbox)
        nq.drain_once(self.outbox)
        self.assertEqual(nq.state(), STATE_OPEN)
        states = {self.outbox.get(e)["delivery_state"] for e in eids}
        self.assertTrue(states <= {STATE_PENDING, STATE_FAILED})
        self.assertNotIn(STATE_ABANDONED, states)

    def test_open_breaker_attempts_nothing(self):
        attempts = []
        nq = self.q(sender=lambda m, c: attempts.append(m) or False,
                    failure_threshold=1)
        nq.publish("order_fill", "m")
        nq.drain_once(self.outbox)
        before = len(attempts)
        nq.publish("order_fill", "m2")
        nq.drain_once(self.outbox)
        self.assertEqual(len(attempts), before)
        self.assertGreater(nq.health()["suppressed"], 0)

    def test_retry_after_cooldown_delivers_the_retained_event(self):
        """The requirement in one test: fails, breaker opens, cooldown
        elapses, and the SAME durable event is delivered on retry."""
        clk = FakeClock()
        ok = {"v": False}
        nq = self.q(sender=lambda m, c: ok["v"], failure_threshold=1,
                    cooldown_seconds=60.0, clock=clk)
        eid = nq.publish("exit_filled", "closed 1 @ 0.45")
        nq.drain_once(self.outbox)
        self.assertEqual(self.outbox.get(eid)["delivery_state"], STATE_FAILED)
        clk.advance(61)
        ok["v"] = True
        nq.drain_once(self.outbox)
        self.assertEqual(self.outbox.get(eid)["delivery_state"], STATE_DELIVERED)


class PrefixTests(_Base):
    def test_prefix_applied_by_module_not_caller(self):
        nq = self.q()
        eid = nq.publish("order_fill", "no banner here")
        self.assertTrue(self.outbox.get(eid)["message"].startswith(PAPER_PREFIX))

    def test_prefix_idempotent(self):
        once = with_paper_prefix("hi")
        self.assertEqual(with_paper_prefix(once), once)
        self.assertEqual(once.count(PAPER_PREFIX), 1)


class GuardTests(_Base):
    def test_notify_after_commit_refuses_before_commit(self):
        nq = self.q()
        self.assertIsNone(nq.notify_after_commit(False, "order_fill", "premature"))
        self.assertEqual(self.outbox.counts()[STATE_PENDING], 0)

    def test_health_shape(self):
        h = self.q().health()
        for k in ("state", "hint_queue_depth", "sent", "failed", "suppressed",
                  "hint_deferred", "coalesced", "outbox", "undelivered_critical"):
            self.assertIn(k, h)


if __name__ == "__main__":
    unittest.main()
