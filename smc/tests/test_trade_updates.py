"""Tests for smc/trade_updates.py.

unittest, no network. Messages are driven straight into _handle_message so
every lifecycle event is deterministic.

These prove the PARSING CONTRACT WE ASSUME, not the contract Alpaca actually
sends. That distinction is the whole ThetaData subscribe-ack lesson: a
synthetic test built on a docs example passed while the real socket behaved
differently. Confirming the real shape against a live paper socket is a
Monday gate item; until then these tests are necessary and not sufficient.
"""
from __future__ import annotations

import datetime as dt
import time
import unittest

from smc.paper_guard import PaperGuardViolation
from smc.trade_updates import (
    AlpacaTradeUpdatesClient, TradeUpdate, paper_stream_url,
)

PAPER = "https://paper-api.alpaca.markets"
LIVE = "https://api.alpaca.markets"
COID = "smc-abc123-entry-1"
OCC = "QQQ260803C00580000"


class FakeQuote:
    def __init__(self, bid=1.00, ask=1.04, age=0.0, generation=7):
        self.bid, self.ask = bid, ask
        self.receipt_monotonic = time.monotonic() - age
        self.exchange_ts = dt.datetime(2026, 8, 3, 14, 30, tzinfo=dt.timezone.utc)
        self.generation = generation


def order(**over):
    o = {"id": "brk-1", "client_order_id": COID, "symbol": OCC, "side": "buy",
         "status": "new", "type": "limit", "limit_price": "0.95", "qty": "1",
         "filled_qty": "0", "filled_avg_price": None}
    o.update(over)
    return o


def msg(event, price=None, qty=None, position_qty=None, **over):
    return {"stream": "trade_updates",
            "data": {"event": event, "order": order(**over), "price": price,
                     "qty": qty, "position_qty": position_qty,
                     "timestamp": "2026-08-03T14:30:00.123Z"}}


def client(quote=None, on_event=None):
    return AlpacaTradeUpdatesClient(PAPER, "PKTEST", "sec",
                                    quote_source=(lambda occ: quote) if quote else None,
                                    on_event=on_event)


class UrlGuardTests(unittest.TestCase):
    def test_paper_url_accepted(self):
        self.assertEqual(paper_stream_url(PAPER), "wss://paper-api.alpaca.markets/stream")

    def test_live_url_rejected(self):
        with self.assertRaises(PaperGuardViolation):
            paper_stream_url(LIVE)

    def test_lookalike_rejected(self):
        for u in ("https://paper-api.alpaca.markets.evil.com",
                  "https://api.alpaca.markets/?x=paper-api.alpaca.markets"):
            with self.subTest(u=u):
                with self.assertRaises(PaperGuardViolation):
                    paper_stream_url(u)

    def test_constructor_refuses_live(self):
        with self.assertRaises(PaperGuardViolation):
            AlpacaTradeUpdatesClient(LIVE, "PKTEST", "sec")


class ParsingTests(unittest.TestCase):
    def test_new_event_recorded(self):
        c = client()
        c._handle_message(__import__("json").dumps(msg("new")))
        u = c.latest(COID)
        self.assertEqual(u.event, "new")
        self.assertEqual(u.client_order_id, COID)
        self.assertEqual(u.broker_order_id, "brk-1")
        self.assertEqual(u.occ, OCC)
        self.assertEqual(u.limit_price, 0.95)
        self.assertFalse(u.is_terminal)

    def test_fill_event_is_terminal_and_carries_prices(self):
        c = client()
        c._handle_message(__import__("json").dumps(
            msg("fill", price="0.93", qty="1", position_qty="1",
                status="filled", filled_qty="1", filled_avg_price="0.93")))
        u = c.latest(COID)
        self.assertTrue(u.is_terminal)
        self.assertEqual(u.event_price, 0.93)
        self.assertEqual(u.filled_avg_price, 0.93)
        self.assertEqual(u.position_qty, 1.0)

    def test_partial_fill_is_not_terminal(self):
        c = client()
        c._handle_message(__import__("json").dumps(
            msg("partial_fill", price="0.93", qty="1", filled_qty="1", qty_="2")))
        self.assertFalse(c.latest(COID).is_terminal)

    def test_all_terminal_events(self):
        for ev in ("fill", "canceled", "rejected", "expired", "replaced", "done_for_day"):
            with self.subTest(ev=ev):
                c = client()
                c._handle_message(__import__("json").dumps(msg(ev)))
                self.assertTrue(c.latest(COID).is_terminal)

    def test_event_history_is_ordered_and_complete(self):
        c = client()
        for ev in ("new", "partial_fill", "fill"):
            c._handle_message(__import__("json").dumps(msg(ev)))
        self.assertEqual([e.event for e in c.events_for(COID)],
                         ["new", "partial_fill", "fill"])

    def test_unknown_event_counted_not_dropped(self):
        """An unrecognized event must stay visible rather than vanish."""
        c = client()
        c._handle_message(__import__("json").dumps(msg("some_future_event")))
        self.assertEqual(c.health()["unknown_event_count"], 1)
        self.assertIsNotNone(c.latest(COID))
        self.assertFalse(c.latest(COID).is_terminal)

    def test_malformed_json_counted(self):
        c = client()
        c._handle_message("not json {{{")
        self.assertEqual(c.health()["malformed_count"], 1)

    def test_missing_order_counted_as_malformed(self):
        c = client()
        c._handle_message(__import__("json").dumps(
            {"stream": "trade_updates", "data": {"event": "fill"}}))
        self.assertEqual(c.health()["malformed_count"], 1)

    def test_non_trade_update_stream_ignored(self):
        c = client()
        c._handle_message(__import__("json").dumps({"stream": "other", "data": {}}))
        self.assertEqual(c.health()["event_count"], 0)

    def test_authorization_message_sets_authenticated(self):
        c = client()
        c._handle_message(__import__("json").dumps(
            {"stream": "authorization", "data": {"status": "authorized", "action": "authenticate"}}))
        self.assertTrue(c.health()["authenticated"])

    def test_failed_authorization_does_not_set_authenticated(self):
        c = client()
        c._handle_message(__import__("json").dumps(
            {"stream": "authorization", "data": {"status": "unauthorized"}}))
        self.assertFalse(c.health()["authenticated"])


class ThetaAttachmentTests(unittest.TestCase):
    def test_theta_nbbo_attached_to_event(self):
        c = client(quote=FakeQuote(bid=0.90, ask=0.94, age=0.2, generation=7))
        c._handle_message(__import__("json").dumps(msg("fill", price="0.93")))
        u = c.latest(COID)
        self.assertEqual((u.theta_bid, u.theta_ask), (0.90, 0.94))
        self.assertIsNotNone(u.theta_quote_age_seconds)
        self.assertLess(u.theta_quote_age_seconds, 1.0)
        self.assertEqual(u.theta_generation, 7)
        self.assertIsNotNone(u.theta_exchange_ts)

    def test_buy_slippage_positive_when_paying_above_mid(self):
        c = client(quote=FakeQuote(bid=0.90, ask=0.94))      # mid 0.92
        c._handle_message(__import__("json").dumps(msg("fill", price="0.93", side="buy")))
        self.assertAlmostEqual(c.latest(COID).slippage_vs_theta_mid(), 0.01, places=6)

    def test_sell_slippage_positive_when_selling_below_mid(self):
        c = client(quote=FakeQuote(bid=0.90, ask=0.94))      # mid 0.92
        c._handle_message(__import__("json").dumps(msg("fill", price="0.91", side="sell")))
        self.assertAlmostEqual(c.latest(COID).slippage_vs_theta_mid(), 0.01, places=6)

    def test_slippage_none_without_quote_never_zero(self):
        c = client()
        c._handle_message(__import__("json").dumps(msg("fill", price="0.93")))
        self.assertIsNone(c.latest(COID).slippage_vs_theta_mid())

    def test_slippage_none_without_event_price(self):
        c = client(quote=FakeQuote())
        c._handle_message(__import__("json").dumps(msg("new")))
        self.assertIsNone(c.latest(COID).slippage_vs_theta_mid())


class CallbackTests(unittest.TestCase):
    def test_callback_invoked_after_caching(self):
        seen = []
        c = client(on_event=lambda u: seen.append(c.latest(u.client_order_id)))
        c._handle_message(__import__("json").dumps(msg("fill", price="0.93")))
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0].event, "fill")   # already cached when called

    def test_callback_exception_does_not_kill_stream_and_is_counted(self):
        def boom(_):
            raise RuntimeError("callback blew up")
        c = client(on_event=boom)
        c._handle_message(__import__("json").dumps(msg("fill", price="0.93")))
        self.assertEqual(c.health()["callback_error_count"], 1)
        self.assertIsNotNone(c.latest(COID))      # event still recorded


class ReconcileTests(unittest.TestCase):
    def test_needs_reconcile_starts_false_and_is_clearable(self):
        c = client()
        self.assertFalse(c.needs_reconcile())
        c._needs_reconcile.set()
        self.assertTrue(c.needs_reconcile())
        c.clear_needs_reconcile()
        self.assertFalse(c.needs_reconcile())

    def test_generation_stamped_on_events(self):
        c = client()
        c._generation = 3
        c._handle_message(__import__("json").dumps(msg("new")))
        self.assertEqual(c.latest(COID).generation, 3)

    def test_health_shape(self):
        c = client()
        h = c.health()
        for k in ("connected", "authenticated", "generation", "tracked_orders",
                  "event_count", "unknown_event_count", "malformed_count",
                  "reconnect_count", "callback_error_count", "needs_reconcile",
                  "seconds_since_last_message"):
            self.assertIn(k, h)


if __name__ == "__main__":
    unittest.main()
