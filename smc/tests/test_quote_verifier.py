"""Tests for smc/quote_verifier.py and smc/readiness.py."""
from __future__ import annotations

import datetime as dt
import time
import unittest

from smc.quote_verifier import CHECKS, verify_live_message
from smc.readiness import (
    GATES, LIVE_DATA_GATES, MARKET_CLOSED, NOT_TESTABLE, PASS, RED, Readiness,
)

OCC = "QQQ260803C00580000"


class LiveQuote:
    """Shaped like smc.theta_stream.StreamQuote."""

    def __init__(self, *, occ=OCC, bid=0.90, ask=0.94, strike=580.0, right="C",
                 generation=7, age=0.3, exchange_ts=None, receipt_ts=None,
                 expiration=None):
        now = dt.datetime.now(dt.timezone.utc)
        self.occ, self.bid, self.ask = occ, bid, ask
        self.strike, self.right = strike, right
        self.expiration = expiration or dt.date(2026, 8, 3)
        self.generation = generation
        self.exchange_ts = exchange_ts if exchange_ts is not None else now
        self.receipt_ts = receipt_ts if receipt_ts is not None else now
        self.receipt_monotonic = time.monotonic() - age
        self._age = age

    def age_seconds(self):
        return self._age


def verify(q=None, gen=7, **kw):
    return verify_live_message(q or LiveQuote(), expected_occ=OCC,
                               current_generation=gen, **kw)


class HappyPathTests(unittest.TestCase):
    def test_good_live_quote_passes_all_eleven(self):
        r = verify()
        self.assertTrue(r.verified, r.evidence)
        self.assertEqual(r.failures, [])
        self.assertEqual(set(r.checks), set(CHECKS))
        self.assertTrue(all(r.checks.values()))

    def test_evidence_recorded(self):
        r = verify()
        self.assertEqual(r.occ, OCC)
        self.assertEqual(r.evidence["generation"], 7)
        self.assertIsNotNone(r.evidence["verified_ts"])


class GenerationTests(unittest.TestCase):
    def test_synthetic_quote_cannot_verify_the_parser(self):
        """generation=0 is the sentinel for a hand-built StreamQuote. It must
        never satisfy a gate that claims a LIVE parser works."""
        r = verify(LiveQuote(generation=0), gen=0)
        self.assertFalse(r.verified)
        self.assertIn("stream_generation", r.failures)

    def test_pre_reconnect_quote_rejected(self):
        r = verify(LiveQuote(generation=6), gen=7)
        self.assertFalse(r.verified)
        self.assertIn("stream_generation", r.failures)

    def test_missing_current_generation_rejected(self):
        r = verify_live_message(LiveQuote(), expected_occ=OCC,
                                current_generation=None)
        self.assertIn("stream_generation", r.failures)


class UnitTests(unittest.TestCase):
    def test_strike_in_mills_rejected(self):
        r = verify(LiveQuote(strike=580000.0))
        self.assertIn("strike_scale", r.failures)

    def test_premium_in_cents_rejected(self):
        r = verify(LiveQuote(bid=90.0, ask=94.0, strike=580.0))
        self.assertNotIn("bid_ask_units", r.failures)   # 90 is plausible alone
        r2 = verify(LiveQuote(bid=9000.0, ask=9400.0))
        self.assertIn("bid_ask_units", r2.failures)

    def test_bad_right_rejected(self):
        self.assertIn("right", verify(LiveQuote(right="X")).failures)

    def test_wrong_occ_rejected(self):
        r = verify(LiveQuote(occ="QQQ260803P00500000"))
        self.assertIn("occ_reconstruction", r.failures)


class MarketQualityTests(unittest.TestCase):
    def test_crossed_market_rejected(self):
        self.assertIn("non_crossed", verify(LiveQuote(bid=1.10, ask=1.00)).failures)

    def test_locked_market_rejected(self):
        self.assertIn("non_crossed", verify(LiveQuote(bid=1.00, ask=1.00)).failures)

    def test_stale_quote_rejected(self):
        r = verify(LiveQuote(age=45.0))
        self.assertIn("age_threshold", r.failures)

    def test_future_exchange_timestamp_rejected(self):
        future = dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=10)
        self.assertIn("exchange_timestamp",
                      verify(LiveQuote(exchange_ts=future)).failures)

    def test_receipt_before_exchange_rejected(self):
        now = dt.datetime.now(dt.timezone.utc)
        r = verify(LiveQuote(exchange_ts=now,
                             receipt_ts=now - dt.timedelta(minutes=10)))
        self.assertIn("receipt_timestamp", r.failures)


class SchemaTests(unittest.TestCase):
    def test_missing_fields_rejected(self):
        class Bare:
            occ = OCC
        r = verify_live_message(Bare(), current_generation=7)
        self.assertFalse(r.verified)
        self.assertIn("schema", r.failures)

    def test_none_quote_fails_everything(self):
        r = verify_live_message(None, current_generation=7)
        self.assertFalse(r.verified)
        self.assertEqual(set(r.failures), set(CHECKS))

    def test_no_partial_credit(self):
        """One bad check fails the whole verification, regardless of how many
        others passed."""
        r = verify(LiveQuote(right="X"))
        self.assertFalse(r.verified)
        self.assertGreater(sum(1 for v in r.checks.values() if v), 5)


class ReadinessModelTests(unittest.TestCase):
    def test_only_pass_permits_entries(self):
        r = Readiness()
        for g in GATES:
            r.set(g, PASS)
        self.assertTrue(r.entries_permitted)

    def test_market_closed_is_not_a_pass(self):
        r = Readiness()
        for g in GATES:
            r.set(g, PASS)
        r.set("theta_live_quotes_fresh", MARKET_CLOSED, "weekend")
        self.assertFalse(r.entries_permitted)
        self.assertIn("theta_live_quotes_fresh", r.blocking())
        self.assertIn("theta_live_quotes_fresh", r.by_status(MARKET_CLOSED))

    def test_not_testable_is_not_a_pass(self):
        r = Readiness()
        for g in GATES:
            r.set(g, PASS)
        r.set("theta_stream_connected", NOT_TESTABLE, "offline")
        self.assertFalse(r.entries_permitted)

    def test_live_data_gates_enumerated(self):
        self.assertEqual(LIVE_DATA_GATES,
                         {"theta_quote_parser_verified", "theta_live_quotes_fresh",
                          "candidate_universe_ready"})

    def test_theta_gates_are_separate(self):
        for g in ("theta_terminal_authenticated", "theta_stream_connected",
                  "theta_quote_parser_verified", "theta_live_quotes_fresh",
                  "universe_greeks_warm", "candidate_universe_ready"):
            self.assertIn(g, GATES)

    def test_invalid_status_rejected(self):
        with self.assertRaises(ValueError):
            Readiness().set("singleton", "GREENISH")

    def test_unknown_gate_rejected(self):
        with self.assertRaises(KeyError):
            Readiness().set("nope", PASS)

    def test_summary_line_reports_counts(self):
        r = Readiness()
        r.set("singleton", PASS)
        line = r.summary_line()
        self.assertIn("entries_permitted=False", line)
        self.assertIn("PASS=1", line)


if __name__ == "__main__":
    unittest.main()
