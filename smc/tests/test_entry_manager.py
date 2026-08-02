"""Tests for smc/entry_manager.py and smc/fast_broker.py.

unittest, no network. The clock is an explicit monotonic value passed in, so
the cancel/fill race is deterministic rather than timing-dependent.
"""
from __future__ import annotations

import datetime as dt
import unittest

from smc.entry_manager import (
    CANCEL_REQUESTED, CANCELLED, FILLED, FILLED_AFTER_CANCEL_REQUEST, REJECTED,
    SUBMITTED, EntryAttempt, fill_latency_bucket,
)
from smc.fast_broker import FastPaperBroker
from smc.paper_guard import PaperGuardViolation

PAPER = "https://paper-api.alpaca.markets"
LIVE = "https://api.alpaca.markets"


class Q:
    def __init__(self, bid=0.90, ask=0.94):
        self.bid, self.ask = bid, ask


class OK:
    ok = True


class NotOK:
    ok = False


def attempt(limit=0.94, ttl=20.0, t0=100.0):
    return EntryAttempt(client_order_id="coid-1", occ="QQQ260803C00580000",
                        limit_price=limit, quantity=1, ttl_seconds=ttl,
                        submitted_monotonic=t0,
                        submitted_ts=dt.datetime.now(dt.timezone.utc))


class BucketTests(unittest.TestCase):
    def test_buckets_split_the_observed_bimodal_distribution(self):
        for secs, want in ((0.0, "0-1s"), (0.9, "0-1s"), (1.5, "1-2s"),
                           (2.0, "2-5s"), (10.0, "5-20s"), (66.0, "over-20s"),
                           (437.0, "over-20s")):
            with self.subTest(secs=secs):
                self.assertEqual(fill_latency_bucket(secs), want)

    def test_none_latency_has_no_bucket(self):
        self.assertIsNone(fill_latency_bucket(None))


class HappyPathTests(unittest.TestCase):
    def test_immediate_fill(self):
        a = attempt()
        a.on_trade_update("fill", price=0.94, filled_qty=1, now_monotonic=100.3)
        self.assertEqual(a.state, FILLED)
        self.assertTrue(a.terminal)
        self.assertTrue(a.has_position)
        self.assertAlmostEqual(a.fill_latency_seconds, 0.3, places=4)
        self.assertEqual(a.fill_latency_bucket, "0-1s")

    def test_ttl_is_a_maximum_not_a_delay(self):
        """Nothing in tick() defers submission; an attempt is live from t0."""
        a = attempt(ttl=20.0)
        self.assertEqual(a.tick(100.0, Q(), lambda oid: OK()), SUBMITTED)
        self.assertFalse(a.ttl_expired(119.9))
        self.assertTrue(a.ttl_expired(120.0))


class MarketabilityTests(unittest.TestCase):
    def test_marketable_while_limit_covers_the_ask(self):
        a = attempt(limit=0.94)
        a.tick(100.5, Q(ask=0.94))
        self.assertTrue(a.still_marketable)

    def test_becomes_unmarketable_when_ask_moves_away(self):
        a = attempt(limit=0.94)
        a.tick(100.5, Q(ask=0.94))
        a.tick(101.0, Q(ask=1.00))
        self.assertFalse(a.still_marketable)

    def test_quote_movement_records_adverse_drift(self):
        a = attempt(limit=0.94)
        for t, ask in ((100.5, 0.94), (101.0, 0.97), (102.0, 1.00)):
            a.tick(t, Q(ask=ask))
        mv = a.quote_movement()
        self.assertEqual(mv["samples"], 3)
        self.assertEqual(mv["ask_first"], 0.94)
        self.assertEqual(mv["ask_last"], 1.00)
        self.assertAlmostEqual(mv["ask_max_adverse"], 0.06, places=4)
        self.assertEqual(mv["ticks_ever_unmarketable"], 2)

    def test_no_sampling_after_terminal(self):
        a = attempt()
        a.on_trade_update("fill", price=0.94, filled_qty=1, now_monotonic=100.2)
        before = len(a.samples)
        a.tick(101.0, Q())
        self.assertEqual(len(a.samples), before)


class CancelRaceTests(unittest.TestCase):
    def test_ttl_expiry_requests_cancel(self):
        a = attempt(ttl=20.0)
        a.tick(120.0, Q(), lambda oid: OK())
        self.assertEqual(a.state, CANCEL_REQUESTED)
        self.assertTrue(a.cancel_request_accepted)

    def test_cancel_requested_is_not_cancel_confirmed(self):
        """The core rule: an accepted DELETE does not end the attempt."""
        a = attempt(ttl=20.0)
        a.tick(120.0, Q(), lambda oid: OK())
        self.assertEqual(a.state, CANCEL_REQUESTED)
        self.assertFalse(a.terminal)
        self.assertFalse(a.summary()["cancel_confirmed"])

    def test_cancel_confirmed_only_on_terminal_event(self):
        a = attempt(ttl=20.0)
        a.tick(120.0, Q(), lambda oid: OK())
        a.on_trade_update("canceled", now_monotonic=120.4)
        self.assertEqual(a.state, CANCELLED)
        self.assertTrue(a.terminal)
        self.assertFalse(a.has_position)
        self.assertTrue(a.summary()["cancel_confirmed"])

    def test_late_fill_after_cancel_request_creates_a_managed_position(self):
        """The failure class that produced an unmanaged position on 07-31."""
        a = attempt(ttl=20.0)
        a.tick(120.0, Q(), lambda oid: OK())
        a.on_trade_update("fill", price=0.94, filled_qty=1, now_monotonic=120.2)
        self.assertEqual(a.state, FILLED_AFTER_CANCEL_REQUEST)
        self.assertTrue(a.has_position)      # MUST be managed, not discarded
        self.assertTrue(a.terminal)

    def test_partial_then_cancel_leaves_a_position(self):
        a = attempt(ttl=20.0)
        a.on_trade_update("partial_fill", price=0.94, filled_qty=1, now_monotonic=105.0)
        a.tick(120.0, Q(), lambda oid: OK())
        a.on_trade_update("canceled", now_monotonic=120.5)
        self.assertTrue(a.has_position)
        self.assertEqual(a.filled_qty, 1.0)

    def test_failed_cancel_request_keeps_the_attempt_live(self):
        """A cancel that could not even be requested must not be mistaken for
        a dead order."""
        a = attempt(ttl=20.0)
        a.tick(120.0, Q(), lambda oid: NotOK())
        self.assertEqual(a.state, CANCEL_REQUESTED)
        self.assertFalse(a.cancel_request_accepted)
        self.assertFalse(a.terminal)

    def test_cancel_fn_raising_does_not_terminate_the_attempt(self):
        def boom(_):
            raise RuntimeError("network gone")
        a = attempt(ttl=20.0)
        a.tick(120.0, Q(), boom)
        self.assertFalse(a.terminal)
        self.assertFalse(a.cancel_request_accepted)

    def test_cancel_requested_only_once(self):
        calls = []
        a = attempt(ttl=20.0)
        a.tick(120.0, Q(), lambda oid: calls.append(oid) or OK())
        a.tick(121.0, Q(), lambda oid: calls.append(oid) or OK())
        self.assertEqual(len(calls), 1)


class RejectTests(unittest.TestCase):
    def test_rejected_is_terminal_with_no_position(self):
        a = attempt()
        a.on_trade_update("rejected", now_monotonic=100.1, reason="buying power")
        self.assertEqual(a.state, REJECTED)
        self.assertTrue(a.terminal)
        self.assertFalse(a.has_position)
        self.assertEqual(a.reject_reason, "buying power")


class FastBrokerTests(unittest.TestCase):
    def test_refuses_live_endpoint_at_construction(self):
        """Raises rather than terminating: the process-level decision belongs
        to the entry point (fatal_guard -> SystemExit) or, post-init, to the
        daemon orderly shutdown."""
        with self.assertRaises(PaperGuardViolation):
            FastPaperBroker(LIVE, {"APCA-API-KEY-ID": "PKX"}, prewarm=False)

    def test_session_has_pooling_and_no_retries(self):
        b = FastPaperBroker(PAPER, {"APCA-API-KEY-ID": "PKX"}, prewarm=False)
        adapter = b.session.get_adapter("https://paper-api.alpaca.markets")
        self.assertGreaterEqual(adapter._pool_maxsize, 2)
        # Never auto-retry a POST: a replayed order submission is a duplicate
        # position. Idempotency comes from client_order_id, not from retries.
        self.assertEqual(adapter.max_retries.total, 0)

    def test_submit_without_client_order_id_refused(self):
        b = FastPaperBroker(PAPER, {"APCA-API-KEY-ID": "PKX"}, prewarm=False)
        with self.assertRaises(ValueError):
            b.submit_order({"symbol": "QQQ", "qty": "1"})


if __name__ == "__main__":
    unittest.main()
