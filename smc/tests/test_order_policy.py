"""Tests for smc/order_policy.py. unittest, no network, no broker."""
from __future__ import annotations

import unittest

from smc.order_policy import (
    DEBIT_CAP_DOLLARS, ENTRY_TTL_SECONDS, ExitMethod, OrderPolicyError,
    build_entry_order, build_exit_order, max_affordable_limit, round_to_tick,
    total_debit_dollars,
)


class Q:
    def __init__(self, bid=0.90, ask=0.94, age=0.3, generation=4):
        self.bid, self.ask, self._age, self.generation = bid, ask, age, generation
        self.exchange_ts = "2026-08-03T14:30:00Z"

    def age_seconds(self):
        return self._age


class TickTests(unittest.TestCase):
    def test_round_up_and_down(self):
        self.assertEqual(round_to_tick(0.941, up=True), 0.95)
        self.assertEqual(round_to_tick(0.949, up=False), 0.94)

    def test_exact_tick_is_stable_in_both_directions(self):
        self.assertEqual(round_to_tick(0.94, up=True), 0.94)
        self.assertEqual(round_to_tick(0.94, up=False), 0.94)


class CapTests(unittest.TestCase):
    def test_total_debit_includes_fees(self):
        self.assertEqual(total_debit_dollars(0.95, 1), 95.05)

    def test_max_affordable_limit_is_99_cents(self):
        """(100.00 - 0.05) / 100 = 0.9995 -> 0.99, since $1.00 would be
        $100.05 including the fee."""
        self.assertEqual(max_affordable_limit(1), 0.99)

    def test_dollar_ask_is_capped_not_rejected(self):
        o = build_entry_order("QQQ260803C00580000", Q(bid=0.99, ask=1.00))
        self.assertTrue(o.capped)
        self.assertEqual(o.limit_price, 0.99)
        self.assertLessEqual(o.intended_debit, DEBIT_CAP_DOLLARS)

    def test_debit_never_exceeds_cap_across_a_sweep_of_asks(self):
        for cents in range(1, 400):
            ask = cents / 100.0
            try:
                o = build_entry_order("QQQ260803C00580000", Q(bid=max(ask - 0.02, 0.01), ask=ask))
            except OrderPolicyError:
                continue
            with self.subTest(ask=ask):
                self.assertLessEqual(o.intended_debit, DEBIT_CAP_DOLLARS + 1e-9)


class EntryTests(unittest.TestCase):
    def test_priced_at_the_ask_to_be_marketable(self):
        o = build_entry_order("QQQ260803C00580000", Q(bid=0.90, ask=0.94))
        self.assertEqual(o.limit_price, 0.94)
        self.assertFalse(o.capped)
        self.assertEqual(o.side, "buy")

    def test_carries_quote_provenance(self):
        o = build_entry_order("QQQ260803C00580000", Q(bid=0.90, ask=0.94, generation=4))
        self.assertEqual((o.quote_bid, o.quote_ask), (0.90, 0.94))
        self.assertEqual(o.quote_age_seconds, 0.3)
        self.assertEqual(o.quote_generation, 4)
        self.assertIsNotNone(o.quote_exchange_ts)

    def test_default_ttl_is_twenty_seconds(self):
        self.assertEqual(ENTRY_TTL_SECONDS, 20.0)
        self.assertEqual(build_entry_order("X", Q()).ttl_seconds, 20.0)

    def test_alpaca_payload_shape(self):
        p = build_entry_order("QQQ260803C00580000", Q()).as_alpaca_payload("coid-1")
        self.assertEqual(p["symbol"], "QQQ260803C00580000")
        self.assertEqual(p["side"], "buy")
        self.assertEqual(p["type"], "limit")
        self.assertEqual(p["limit_price"], "0.94")
        self.assertEqual(p["client_order_id"], "coid-1")

    def test_rejects_missing_side(self):
        for bid, ask in ((None, 0.94), (0.90, None)):
            with self.subTest(bid=bid, ask=ask):
                with self.assertRaises(OrderPolicyError):
                    build_entry_order("X", Q(bid=bid, ask=ask))

    def test_rejects_nonpositive_and_crossed(self):
        with self.assertRaises(OrderPolicyError):
            build_entry_order("X", Q(bid=0.0, ask=0.94))
        with self.assertRaises(OrderPolicyError):
            build_entry_order("X", Q(bid=1.10, ask=1.00))


class ExitTests(unittest.TestCase):
    def test_market_exit_has_no_limit(self):
        o = build_exit_order("X", Q(), ExitMethod.MARKET)
        self.assertEqual(o.order_type, "market")
        self.assertIsNone(o.limit_price)
        self.assertNotIn("limit_price", o.as_alpaca_payload("c"))

    def test_marketable_limit_crosses_through_the_bid(self):
        """Pricing AT the displayed bid is the 2026-07-31 failure; the exit
        must cross through it."""
        o = build_exit_order("X", Q(bid=0.90), ExitMethod.MARKETABLE_LIMIT,
                             aggression_ticks=2)
        self.assertEqual(o.order_type, "limit")
        self.assertEqual(o.limit_price, 0.88)
        self.assertLess(o.limit_price, 0.90)

    def test_marketable_limit_never_goes_below_one_tick(self):
        o = build_exit_order("X", Q(bid=0.01), ExitMethod.MARKETABLE_LIMIT,
                             aggression_ticks=5)
        self.assertEqual(o.limit_price, 0.01)

    def test_marketable_limit_needs_a_positive_bid(self):
        with self.assertRaises(OrderPolicyError):
            build_exit_order("X", Q(bid=0.0), ExitMethod.MARKETABLE_LIMIT)

    def test_market_exit_works_without_a_bid(self):
        """A protective market exit must not depend on quote quality."""
        o = build_exit_order("X", Q(bid=None), ExitMethod.MARKET)
        self.assertEqual(o.order_type, "market")

    def test_unknown_method_rejected(self):
        with self.assertRaises(OrderPolicyError):
            build_exit_order("X", Q(), "hope")

    def test_both_methods_are_still_candidates(self):
        """Neither is frozen yet -- the PAPER benchmark picks one."""
        self.assertEqual(set(ExitMethod.ALL), {"market", "marketable_limit"})


if __name__ == "__main__":
    unittest.main()
