"""Tests for smc/feeds.py -- explicit quote-feed provenance."""
from __future__ import annotations

import unittest

from smc.feeds import (
    FEED_ALPACA_INDICATIVE, FEED_ALPACA_OPRA, FEED_THETADATA_OPRA_NBBO,
    TRUSTED_NBBO_FEEDS, FeedProvenanceError, evaluate_quote_provenance,
    is_trusted_nbbo, may_price_decision, normalize_feed,
)


class AllowlistTests(unittest.TestCase):
    def test_trusted_identities(self):
        self.assertTrue(is_trusted_nbbo(FEED_ALPACA_OPRA))
        self.assertTrue(is_trusted_nbbo(FEED_THETADATA_OPRA_NBBO))

    def test_alpaca_indicative_is_not_nbbo(self):
        self.assertFalse(is_trusted_nbbo(FEED_ALPACA_INDICATIVE))

    def test_arbitrary_string_containing_theta_is_refused(self):
        """Trust must come from the allowlist, not from string shape."""
        for bad in ("theta", "thetadata_guess", "my_theta_feed", "THETA-X"):
            with self.subTest(bad=bad):
                self.assertFalse(is_trusted_nbbo(bad))

    def test_unknown_provider_refused(self):
        self.assertFalse(is_trusted_nbbo("polygon_nbbo"))

    def test_malformed_refused(self):
        for bad in (None, "", "   ", 123, object()):
            with self.subTest(bad=bad):
                self.assertFalse(is_trusted_nbbo(bad))

    def test_normalize_raises_on_unknown(self):
        with self.assertRaises(FeedProvenanceError):
            normalize_feed("polygon_nbbo")

    def test_legacy_labels_mapped_forward(self):
        self.assertEqual(normalize_feed("opra"), FEED_ALPACA_OPRA)
        self.assertEqual(normalize_feed("thetadata"), FEED_THETADATA_OPRA_NBBO)
        self.assertEqual(normalize_feed("indicative"), FEED_ALPACA_INDICATIVE)

    def test_case_and_whitespace_tolerated(self):
        self.assertEqual(normalize_feed("  Alpaca_OPRA "), FEED_ALPACA_OPRA)


class ProvenanceTests(unittest.TestCase):
    def test_fresh_trusted_quote_may_price(self):
        p = evaluate_quote_provenance(FEED_THETADATA_OPRA_NBBO, age_seconds=0.5)
        self.assertTrue(p.trusted_nbbo)
        self.assertFalse(p.stale)
        self.assertTrue(may_price_decision(p))
        self.assertIsNone(p.reason)

    def test_stale_trusted_quote_may_not_price(self):
        """A fresh-looking NBBO label on a 45s-old snapshot must not price a
        stop."""
        p = evaluate_quote_provenance(FEED_THETADATA_OPRA_NBBO, age_seconds=45.0)
        self.assertTrue(p.trusted_nbbo)
        self.assertTrue(p.stale)
        self.assertFalse(may_price_decision(p))
        self.assertIn("exceeds", p.reason)

    def test_fresh_untrusted_quote_may_not_price(self):
        p = evaluate_quote_provenance(FEED_ALPACA_INDICATIVE, age_seconds=0.1)
        self.assertFalse(may_price_decision(p))
        self.assertIn("not an NBBO feed", p.reason)

    def test_unknown_provider_records_reason(self):
        p = evaluate_quote_provenance("polygon", age_seconds=0.1)
        self.assertEqual(p.feed, "unknown")
        self.assertFalse(may_price_decision(p))
        self.assertIn("unknown quote provider", p.reason)

    def test_missing_age_treated_as_stale(self):
        p = evaluate_quote_provenance(FEED_ALPACA_OPRA, age_seconds=None)
        self.assertTrue(p.stale)
        self.assertFalse(may_price_decision(p))

    def test_provider_identity_retained_not_collapsed_to_boolean(self):
        p = evaluate_quote_provenance(FEED_THETADATA_OPRA_NBBO, age_seconds=0.2,
                                      exchange_ts="2026-08-03T14:30:00Z",
                                      receipt_ts="2026-08-03T14:30:00.1Z",
                                      stream_generation=7)
        d = p.as_dict()
        self.assertEqual(d["feed"], FEED_THETADATA_OPRA_NBBO)
        self.assertEqual(d["stream_generation"], 7)
        self.assertIsNotNone(d["exchange_ts"])
        self.assertIsNotNone(d["receipt_ts"])

    def test_custom_max_age(self):
        p = evaluate_quote_provenance(FEED_ALPACA_OPRA, age_seconds=20.0,
                                      max_age_seconds=30.0)
        self.assertFalse(p.stale)

    def test_trusted_set_is_exactly_two(self):
        self.assertEqual(TRUSTED_NBBO_FEEDS,
                         {FEED_ALPACA_OPRA, FEED_THETADATA_OPRA_NBBO})


if __name__ == "__main__":
    unittest.main()
