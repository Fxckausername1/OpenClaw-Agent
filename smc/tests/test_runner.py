"""Tests for smc/events.py, smc/singleton.py and smc/runner.py.

unittest, no network, no broker, no Terminal. Every collaborator is a fake,
so the whole daemon is exercised offline.
"""
from __future__ import annotations

import datetime as dt
import tempfile
import threading
import time
import unittest
from pathlib import Path

from smc import events as ev
from smc.entry_manager import CANCEL_REQUESTED, FILLED_AFTER_CANCEL_REQUEST, EntryAttempt
from smc.events import EventBus, make_event
from smc.readiness import GATES, Readiness
from smc.runner import PaperRunner
from smc.singleton import AlreadyRunning, SingleInstance


class Q:
    def __init__(self, occ="QQQ260803C00580000", bid=0.90, ask=0.94):
        self.occ, self.bid, self.ask = occ, bid, ask


class OKCall:
    ok = True


class FakeBroker:
    prewarmed = True

    def __init__(self, ok=True):
        self._ok = ok
        self.cancels = []

    def open_orders(self):
        return OKCall() if self._ok else type("C", (), {"ok": False})()

    def positions(self):
        return OKCall() if self._ok else type("C", (), {"ok": False})()

    def cancel_order(self, oid):
        self.cancels.append(oid)
        return OKCall()


class FakeStream:
    def __init__(self, connected=True):
        self._c = connected

    def is_connected(self):
        return self._c

    def health(self):
        return {"connected": self._c}

    def stop(self):
        pass


class FakeTU(FakeStream):
    def clear_needs_reconcile(self):
        pass


class FakeCache:
    def cache_status(self):
        return {"2026-08-03": {"n_rows": 10}}


class FakeState:
    def verify_integrity(self):
        return None

    def close(self):
        pass


class FakeNotifier:
    def __init__(self):
        self.published = []

    def publish(self, kind, message, detail=None, **kw):
        self.published.append((kind, message))
        return len(self.published)

    def health(self):
        return {"state": "closed"}

    def stop(self):
        pass


# ---------------------------------------------------------------- EventBus
class EventBusTests(unittest.TestCase):
    def test_priority_order_fill_before_health(self):
        bus = EventBus()
        bus.publish(make_event(ev.EV_HEALTH))
        bus.publish(make_event(ev.EV_FILL))
        self.assertEqual(bus.get(timeout=0.1).event_type, ev.EV_FILL)

    def test_shutdown_outranks_everything(self):
        bus = EventBus()
        bus.publish(make_event(ev.EV_FILL))
        bus.publish(make_event(ev.EV_SHUTDOWN))
        self.assertEqual(bus.get(timeout=0.1).event_type, ev.EV_SHUTDOWN)

    def test_critical_events_ignore_maxsize(self):
        """A full bus must never drop a fill."""
        bus = EventBus(maxsize=2)
        for _ in range(5):
            self.assertTrue(bus.publish(make_event(ev.EV_FILL)))
        self.assertEqual(bus.health()["dropped_informational"], 0)

    def test_informational_dropped_when_saturated(self):
        bus = EventBus(maxsize=2)
        results = [bus.publish(make_event(ev.EV_HEALTH)) for _ in range(5)]
        self.assertIn(False, results)
        self.assertGreater(bus.health()["dropped_informational"], 0)

    def test_informational_coalescing(self):
        bus = EventBus()
        bus.publish(make_event(ev.EV_HEALTH, payload={"n": 1}), coalesce_key="health")
        bus.publish(make_event(ev.EV_HEALTH, payload={"n": 2}), coalesce_key="health")
        self.assertEqual(bus.health()["coalesced_informational"], 1)
        self.assertEqual(bus.get(timeout=0.1).payload["n"], 2)

    def test_deadline_fires_and_is_delivered(self):
        bus = EventBus()
        bus.schedule(time.monotonic() + 0.05,
                     make_event(ev.EV_DEADLINE, payload={"kind": "entry_ttl"}))
        got = bus.get(timeout=1.0)
        self.assertIsNotNone(got)
        self.assertEqual(got.event_type, ev.EV_DEADLINE)

    def test_get_blocks_rather_than_spinning(self):
        """The loop must PARK until the deadline, not poll. If it spun, this
        would return early or burn the core."""
        bus = EventBus()
        bus.schedule(time.monotonic() + 0.20, make_event(ev.EV_DEADLINE))
        t0 = time.monotonic()
        bus.get(timeout=2.0)
        elapsed = time.monotonic() - t0
        self.assertGreaterEqual(elapsed, 0.18)
        self.assertLess(elapsed, 1.0)

    def test_cancel_scheduled_withdraws_a_deadline(self):
        bus = EventBus()
        bus.schedule(time.monotonic() + 0.05,
                     make_event(ev.EV_DEADLINE, event_id="ttl-1"))
        self.assertTrue(bus.cancel_scheduled("ttl-1"))
        self.assertIsNone(bus.get(timeout=0.15))

    def test_publish_wakes_a_blocked_consumer(self):
        bus = EventBus()
        got = []

        def consume():
            got.append(bus.get(timeout=2.0))

        t = threading.Thread(target=consume)
        t.start()
        time.sleep(0.05)
        bus.publish(make_event(ev.EV_FILL))
        t.join(timeout=2.0)
        self.assertEqual(got[0].event_type, ev.EV_FILL)

    def test_metrics_record_queue_delay_and_handler_time(self):
        bus = EventBus()
        bus.publish(make_event(ev.EV_FILL))
        e = bus.get(timeout=0.1)
        bus.record_handled(e, time.monotonic(), 0.01)
        h = bus.health()
        self.assertGreaterEqual(h["max_handler_ms"], 9.0)
        self.assertGreaterEqual(h["depth_high_water"], 1)
        self.assertIn("max_queue_delay_ms", h)


# --------------------------------------------------------------- Singleton
class SingletonTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.lock = Path(self._tmp.name) / "runner.lock"

    def tearDown(self):
        self._tmp.cleanup()

    def test_acquire_and_release(self):
        s = SingleInstance(self.lock).acquire()
        self.assertTrue(s.held)
        s.release()
        self.assertFalse(s.held)

    def test_second_instance_refused(self):
        first = SingleInstance(self.lock).acquire()
        try:
            with self.assertRaises(AlreadyRunning):
                SingleInstance(self.lock).acquire()
        finally:
            first.release()

    def test_lock_is_reusable_after_release(self):
        SingleInstance(self.lock).acquire().release()
        s = SingleInstance(self.lock).acquire()
        self.assertTrue(s.held)
        s.release()

    def test_context_manager(self):
        with SingleInstance(self.lock) as s:
            self.assertTrue(s.held)
            with self.assertRaises(AlreadyRunning):
                SingleInstance(self.lock).acquire()


# --------------------------------------------------------------- Readiness
class ReadinessTests(unittest.TestCase):
    def test_all_gates_false_initially(self):
        r = Readiness()
        self.assertFalse(r.entries_permitted)
        self.assertTrue(r.degraded)
        self.assertEqual(set(r.blocking()), set(GATES))

    def test_entries_permitted_only_when_every_gate_green(self):
        r = Readiness()
        for g in GATES[:-1]:
            r.set_bool(g, True)
        self.assertFalse(r.entries_permitted)
        r.set_bool(GATES[-1], True)
        self.assertTrue(r.entries_permitted)

    def test_unknown_gate_rejected(self):
        with self.assertRaises(KeyError):
            Readiness().set("made_up", True)

    def test_reason_recorded_and_cleared(self):
        r = Readiness()
        r.set_bool("theta_stream_connected", False, "not connected")
        self.assertIn("theta_stream_connected", r.as_dict()["reasons"])
        r.set_bool("theta_stream_connected", True)
        self.assertNotIn("theta_stream_connected", r.as_dict()["reasons"])


# ------------------------------------------------------------------ Runner
def runner(**kw):
    defaults = dict(state_store=FakeState(), outbox=object(), notifier=FakeNotifier(),
                    theta_stream=FakeStream(), greek_cache=FakeCache(),
                    broker=FakeBroker(), trade_updates=FakeTU(),
                    terminal_check=lambda: True)
    defaults.update(kw)
    return PaperRunner(**defaults)


class RunnerStartupTests(unittest.TestCase):
    def test_detector_gate_red_by_default_blocks_entries(self):
        """The persistent detector is not built; the gate must block rather
        than silently pass."""
        r = runner()
        r.start()
        self.assertFalse(r.readiness.entries_permitted)
        self.assertIn("detector_synced", r.readiness.blocking())

    def test_connected_sockets_alone_do_not_permit_entries(self):
        """THE false-green this model exists to prevent: a connected stream
        and an acked subscription are transport, not quote readiness. With no
        verified live quote, entries must stay blocked."""
        r = runner(detector=type("D", (), {"synchronized": True})())
        r.start()
        self.assertFalse(r.readiness.entries_permitted)
        blocking = r.readiness.blocking()
        self.assertIn("theta_quote_parser_verified", blocking)
        self.assertIn("theta_live_quotes_fresh", blocking)
        self.assertIn("candidate_universe_ready", blocking)

    def test_market_closed_reports_amber_not_green_and_not_broken(self):
        r = runner(detector=type("D", (), {"synchronized": True})())
        r.start(market_closed=True)
        self.assertFalse(r.readiness.entries_permitted)
        d = r.readiness.as_dict()
        for gate in ("theta_quote_parser_verified", "theta_live_quotes_fresh",
                     "candidate_universe_ready"):
            self.assertEqual(d["statuses"][gate], "MARKET_CLOSED", gate)
        self.assertEqual(sorted(d["live_data_unverified"]),
                         sorted(["theta_quote_parser_verified",
                                 "theta_live_quotes_fresh",
                                 "candidate_universe_ready"]))

    def test_offline_mode_marks_connection_gates_not_testable(self):
        r = runner(detector=type("D", (), {"synchronized": True})())
        r.start(offline=True)
        d = r.readiness.as_dict()
        self.assertIn("theta_stream_connected", d["not_testable_gates"])
        self.assertFalse(r.readiness.entries_permitted)

    def test_entries_permitted_only_with_verified_live_evidence(self):
        """Supplying the live evidence -- a verified parser result, a fresh
        quote on the current generation, eligible candidates and a warm
        Greek cache -- is what actually opens the gate."""
        class FreshCache:
            def cache_status(self):
                return {"2026-08-03": {"n_rows": 408, "age_seconds": 1.0}}

        r = runner(detector=type("D", (), {"synchronized": True})(),
                   greek_cache=FreshCache())
        r.quote_verification = type("V", (), {
            "verified": True, "failures": [], "as_dict": lambda self: {"ok": True}})()
        r.fresh_quote_count = 5
        r.candidate_ready_count = 3
        r.start()
        self.assertTrue(r.readiness.entries_permitted, r.readiness.as_dict())

    def test_disconnected_stream_blocks(self):
        r = runner(theta_stream=FakeStream(connected=False))
        r.start()
        self.assertIn("theta_stream_connected", r.readiness.blocking())

    def test_failed_reconciliation_blocks(self):
        r = runner(broker=FakeBroker(ok=False))
        r.start()
        self.assertIn("reconciled", r.readiness.blocking())

    def test_degraded_start_notifies(self):
        n = FakeNotifier()
        r = runner(notifier=n)
        r.start()
        self.assertTrue(any(k == "degraded" for k, _ in n.published))


class RunnerEventTests(unittest.TestCase):
    def test_signal_rejected_while_degraded(self):
        n = FakeNotifier()
        r = runner(notifier=n)
        r.start()
        r.bus.publish(make_event(ev.EV_SIGNAL, signal_id="sig-1"))
        r.run(max_events=1, idle_timeout=0.2)
        self.assertTrue(any(k == "signal_blocked" for k, _ in n.published))

    def test_handler_exception_does_not_kill_the_loop(self):
        r = runner()
        r.start()
        r._handlers[ev.EV_HEALTH] = lambda e: (_ for _ in ()).throw(RuntimeError("boom"))
        r.bus.publish(make_event(ev.EV_HEALTH))
        r.bus.publish(make_event(ev.EV_FILL))
        handled = r.run(max_events=2, idle_timeout=0.2)
        self.assertEqual(handled, 2)
        self.assertEqual(r.handler_errors, 1)

    def test_reconnect_forces_a_reconciliation(self):
        r = runner()
        r.start()
        r.bus.publish(make_event(ev.EV_RECONNECT, payload={"source": "theta"}))
        r.run(max_events=1, idle_timeout=0.2)
        nxt = r.bus.get(timeout=0.2)
        self.assertEqual(nxt.event_type, ev.EV_RECONCILE)

    def test_entry_ttl_is_one_scheduled_deadline(self):
        r = runner()
        broker = r.broker
        a = EntryAttempt(client_order_id="coid-1", occ="QQQ260803C00580000",
                         limit_price=0.94, quantity=1, ttl_seconds=0.05,
                         submitted_monotonic=time.monotonic(),
                         submitted_ts=dt.datetime.now(dt.timezone.utc))
        r.register_attempt(a)
        got = r.bus.get(timeout=1.0)
        self.assertEqual(got.event_type, ev.EV_DEADLINE)
        r._on_deadline(got)
        self.assertEqual(a.state, CANCEL_REQUESTED)
        self.assertEqual(len(broker.cancels), 1)

    def test_late_fill_after_cancel_request_is_reported_as_a_position(self):
        n = FakeNotifier()
        r = runner(notifier=n)
        a = EntryAttempt(client_order_id="coid-1", occ="QQQ260803C00580000",
                         limit_price=0.94, quantity=1, ttl_seconds=0.01,
                         submitted_monotonic=time.monotonic(),
                         submitted_ts=dt.datetime.now(dt.timezone.utc))
        r.register_attempt(a)
        a.tick(time.monotonic() + 1, None, cancel_fn=r._cancel_fn())
        r._on_order_event(make_event(ev.EV_FILL, client_order_id="coid-1",
                                     payload={"event": "fill", "price": 0.94,
                                              "filled_qty": 1}))
        self.assertEqual(a.state, FILLED_AFTER_CANCEL_REQUEST)
        self.assertTrue(any(k == "order_fill" for k, _ in n.published))

    def test_quote_event_samples_marketability(self):
        r = runner()
        a = EntryAttempt(client_order_id="coid-1", occ="QQQ260803C00580000",
                         limit_price=0.94, quantity=1, ttl_seconds=30.0,
                         submitted_monotonic=time.monotonic(),
                         submitted_ts=dt.datetime.now(dt.timezone.utc))
        r.attempts["coid-1"] = a
        r._on_quote(make_event(ev.EV_QUOTE, quote=Q(ask=1.00)))
        self.assertEqual(len(a.samples), 1)
        self.assertFalse(a.still_marketable)

    def test_health_snapshot_shape(self):
        r = runner()
        r.start()
        h = r.health()
        for k in ("readiness", "bus", "open_attempts", "handler_errors"):
            self.assertIn(k, h)

    def test_shutdown_is_orderly(self):
        r = runner()
        r.start()
        r.shutdown()   # must not raise


if __name__ == "__main__":
    unittest.main()
