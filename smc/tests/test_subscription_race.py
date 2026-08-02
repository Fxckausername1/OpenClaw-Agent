"""Regression tests for the startup subscription race and stale readiness.

Both failures were the same shape: a gate captured at t=0 that could never
improve. The daemon must be able to wait behind gates and go green on its
own, because nobody is going to SSH in on Monday morning to restart it.
"""
from __future__ import annotations

import threading
import time
import unittest

from smc import events as ev
from smc.events import make_event
from smc.readiness import GATES, MARKET_CLOSED, PASS, RED, Readiness
from smc.runner import PaperRunner
from smc.theta_stream import ThetaStreamClient

OCCS = {f"QQQ260803C{i:08d}" for i in range(500000, 500826)}   # 826, like production


class SubscriptionBarrierTests(unittest.TestCase):
    """wait_for_subscriptions is the barrier that turns 'we hope the ordering
    held' into 'the acks are in'."""

    def client(self):
        return ThetaStreamClient()

    def test_empty_universe_returns_immediately(self):
        c = self.client()
        t0 = time.monotonic()
        self.assertTrue(c.wait_for_subscriptions(timeout=5.0))
        self.assertLess(time.monotonic() - t0, 1.0)

    def test_zero_of_n_times_out_when_nothing_acks(self):
        """The pre-fix state: universe desired, socket never acknowledges."""
        c = self.client()
        c.set_desired_universe(OCCS)
        t0 = time.monotonic()
        self.assertFalse(c.wait_for_subscriptions(timeout=1.0))
        self.assertGreaterEqual(time.monotonic() - t0, 0.9)
        self.assertEqual(c.health()["n_subscribed"], 0)
        self.assertEqual(c.health()["n_desired"], 826)

    def test_zero_of_n_transitions_to_n_of_n(self):
        """THE regression: 0/826 -> 826/826 unblocks the barrier."""
        c = self.client()
        c.set_desired_universe(OCCS)
        self.assertEqual(c.health()["n_subscribed"], 0)

        def ack_all():
            time.sleep(0.3)
            with c._lock:
                c._connected = True
                c._subscribed = set(c._desired)

        threading.Thread(target=ack_all, daemon=True).start()
        self.assertTrue(c.wait_for_subscriptions(timeout=10.0))
        h = c.health()
        self.assertEqual(h["n_subscribed"], 826)
        self.assertEqual(h["n_desired"], 826)

    def test_partial_acks_can_satisfy_a_fraction(self):
        """A venue refusing a handful must not block the daemon forever."""
        c = self.client()
        c.set_desired_universe(OCCS)
        with c._lock:
            c._connected = True
            c._subscribed = set(list(OCCS)[:800])
        self.assertFalse(c.wait_for_subscriptions(timeout=0.5, min_fraction=1.0))
        self.assertTrue(c.wait_for_subscriptions(timeout=0.5, min_fraction=0.95))

    def test_barrier_requires_connection_not_just_count(self):
        c = self.client()
        c.set_desired_universe(OCCS)
        with c._lock:
            c._connected = False
            c._subscribed = set(OCCS)
        self.assertFalse(c.wait_for_subscriptions(timeout=0.5))

    def test_universe_set_before_start_is_visible_to_resubscribe(self):
        """The ordering fix: _resubscribe_all reads _desired, so setting the
        universe BEFORE start() means connect covers all 826 with no
        reconcile wait."""
        c = self.client()
        c.set_desired_universe(OCCS)
        with c._lock:
            self.assertEqual(len(c._desired), 826)


class FakeStream:
    def __init__(self, subscribed, desired, connected=True, generation=1):
        self._s, self._d, self._c, self._g = subscribed, desired, connected, generation

    def is_connected(self):
        return self._c

    def health(self):
        return {"connected": self._c, "n_subscribed": self._s,
                "n_desired": self._d, "generation": self._g}

    def stop(self):
        pass


class FakeState:
    def verify_integrity(self):
        return None

    def close(self):
        pass


class RevalidationTests(unittest.TestCase):
    """A gate stale at startup must be able to go green WITHOUT a restart."""

    def runner(self, stream):
        return PaperRunner(state_store=FakeState(), outbox=object(),
                           theta_stream=stream, terminal_check=lambda: True)

    def test_stale_startup_gate_recovers_on_revalidate(self):
        """THE regression: gate captured at 0/826 must not stay red once the
        subscriptions land."""
        stream = FakeStream(subscribed=0, desired=826)
        r = self.runner(stream)
        r.start(market_closed=True)
        self.assertIn("theta_stream_connected", r.readiness.blocking())

        stream._s = 826                      # acks arrive after startup
        r.revalidate()
        self.assertEqual(r.readiness.gates["theta_stream_connected"].status, PASS)

    def test_revalidate_does_not_reacquire_singleton(self):
        """It must be safe to call on every health tick."""
        import inspect
        src = inspect.getsource(PaperRunner.revalidate)
        self.assertNotIn("_gate_singleton", src)
        self.assertNotIn("_gate_state", src)

    def test_revalidate_is_idempotent(self):
        stream = FakeStream(subscribed=826, desired=826)
        r = self.runner(stream)
        r.start(market_closed=True)
        first = r.readiness.as_dict()["statuses"]
        r.revalidate()
        r.revalidate()
        self.assertEqual(r.readiness.as_dict()["statuses"], first)

    def test_health_event_triggers_revalidation(self):
        stream = FakeStream(subscribed=0, desired=826)
        r = self.runner(stream)
        r.start(market_closed=True)
        stream._s = 826
        r.bus.publish(make_event(ev.EV_HEALTH, payload={"periodic": True}))
        r.run(max_events=1, idle_timeout=0.2)
        self.assertEqual(r.readiness.gates["theta_stream_connected"].status, PASS)

    def test_revalidation_cannot_green_live_gates_on_a_weekend(self):
        """Self-healing must NOT become a way to fake live-data readiness."""
        stream = FakeStream(subscribed=826, desired=826)
        r = self.runner(stream)
        r.start(market_closed=True)
        r.revalidate()
        for gate in ("theta_quote_parser_verified", "theta_live_quotes_fresh",
                     "candidate_universe_ready"):
            self.assertEqual(r.readiness.gates[gate].status, MARKET_CLOSED, gate)
        self.assertFalse(r.readiness.entries_permitted)

    def test_gate_exception_does_not_kill_the_loop(self):
        class Exploding:
            def is_connected(self):
                raise RuntimeError("stream blew up")

            def health(self):
                raise RuntimeError("stream blew up")

            def stop(self):
                pass
        r = self.runner(Exploding())
        r.start(market_closed=True)
        r.revalidate()                       # must not raise
        self.assertFalse(r.readiness.entries_permitted)


if __name__ == "__main__":
    unittest.main(verbosity=2)
