"""Tests for smc/candidate_universe.py -- the streaming-quote / REST-Greek
merge layer.

unittest, matching this codebase's convention (pytest is not installed and
the box is on a no-new-deps footing). No test here touches the network, the
Terminal, or a broker. The stream and Greek caches are driven through
hand-built snapshot dicts and fakes, so every timing case (mid-selection
arrival, reconnect, staleness on either clock independently) is
deterministic rather than raced.

Covers, one test each, the cases heff required before this layer may be
connected to Alpaca PAPER: quote-arrives-during-selection, Greek-refresh-
during-selection, contract-removed-during-selection, duplicate OCC
normalization, stale-quote/fresh-delta, fresh-quote/stale-delta, reconnect
generation invalidation, subscription-acked-but-no-quote, crossed and locked
markets, missing bid or ask, zero/negative prices, and excessively wide
spread.
"""
from __future__ import annotations

import datetime as dt
import time
import unittest

from smc.candidate_universe import (
    R_CROSSED, R_DELTA_TOO_OLD, R_DISCONNECTED, R_LOCKED, R_MISSING_SIDE,
    R_NO_DELTA, R_NO_QUOTE, R_NONPOSITIVE, R_NOT_SUBSCRIBED, R_QUOTE_TOO_OLD,
    R_STALE_GENERATION, R_WIDE_SPREAD, UniverseConfig, build_snapshot,
    canonical_occ, take_snapshot,
)
from smc.theta_stream import StreamQuote

OCC_A = "QQQ260803C00580000"
OCC_B = "QQQ260803C00581000"
EXP = dt.date(2026, 8, 3)


def make_quote(occ=OCC_A, bid=1.00, ask=1.04, generation=1,
               age=0.0, now_m=None, bid_size=10, ask_size=10):
    now_m = now_m if now_m is not None else time.monotonic()
    return StreamQuote(
        occ=occ, root="QQQ", expiration=EXP, strike=580.0, right="C",
        bid=bid, ask=ask, bid_size=bid_size, ask_size=ask_size,
        exchange_ts=dt.datetime(2026, 8, 3, 14, 30, tzinfo=dt.timezone.utc),
        receipt_ts=dt.datetime(2026, 8, 3, 14, 30, tzinfo=dt.timezone.utc),
        receipt_monotonic=now_m - age, generation=generation,
    )


def stream_snap(quotes, subscribed=None, generation=1, connected=True, now_m=None):
    now_m = now_m if now_m is not None else time.monotonic()
    subscribed = subscribed if subscribed is not None else {o: True for o in quotes}
    return {"generation": generation, "connected": connected,
            "taken_monotonic": now_m, "taken_ts": dt.datetime.now(dt.timezone.utc),
            "quotes": quotes, "subscribed": subscribed}


def greek_snap(deltas, now_m=None, age=0.0):
    now_m = now_m if now_m is not None else time.monotonic()
    return {"taken_monotonic": now_m, "taken_ts": dt.datetime.now(dt.timezone.utc),
            "deltas": {occ: {"delta": d, "source_monotonic": now_m - age,
                             "source_wall": dt.datetime.now(dt.timezone.utc)}
                       for occ, d in deltas.items()}}


class _Base(unittest.TestCase):
    def only(self, snapshot):
        self.assertEqual(len(snapshot.candidates), 1)
        return snapshot.candidates[0]


class HappyPathTests(_Base):
    def test_healthy_candidate_eligible_with_full_provenance(self):
        now_m = time.monotonic()
        snap = build_snapshot(
            stream_snap({OCC_A: make_quote(now_m=now_m)}, now_m=now_m),
            greek_snap({OCC_A: 0.35}, now_m=now_m), [OCC_A])
        c = self.only(snap)
        self.assertTrue(c.eligible)
        self.assertEqual(c.reasons, ())
        # Every field heff required must be populated, not merely present.
        self.assertEqual((c.occ, c.expiration, c.strike, c.right), (OCC_A, EXP, 580.0, "C"))
        self.assertEqual(c.delta, 0.35)
        self.assertIsNotNone(c.delta_source_ts)
        self.assertIsNotNone(c.delta_age_seconds)
        self.assertEqual((c.bid, c.ask, c.bid_size, c.ask_size), (1.00, 1.04, 10, 10))
        self.assertIsNotNone(c.exchange_ts)
        self.assertIsNotNone(c.receipt_ts)
        self.assertIsNotNone(c.quote_age_seconds)
        self.assertEqual((c.quote_generation, c.snapshot_generation), (1, 1))
        self.assertTrue(c.subscription_confirmed)

    def test_book_has_exactly_the_columns_the_selector_reads(self):
        now_m = time.monotonic()
        snap = build_snapshot(stream_snap({OCC_A: make_quote(now_m=now_m)}, now_m=now_m),
                              greek_snap({OCC_A: 0.35}, now_m=now_m), [OCC_A])
        self.assertEqual(list(snap.to_book().columns),
                         ["strike", "right", "expiration", "bid", "ask",
                          "bid_size", "ask_size", "delta", "quote_age_seconds"])
        self.assertEqual(len(snap.to_book()), 1)

    def test_ineligible_excluded_from_book_but_kept_for_diagnostics(self):
        now_m = time.monotonic()
        snap = build_snapshot(
            stream_snap({OCC_A: make_quote(now_m=now_m), OCC_B: None}, now_m=now_m),
            greek_snap({OCC_A: 0.35, OCC_B: 0.30}, now_m=now_m), [OCC_A, OCC_B])
        self.assertEqual(len(snap.to_book()), 1)
        self.assertEqual(len(snap.candidates), 2)
        self.assertEqual(snap.diagnostics()["rejection_counts"][R_NO_QUOTE], 1)


class AgeGateTests(_Base):
    """quote age and Greek age are SEPARATE clocks with SEPARATE ceilings."""

    def test_fresh_quote_with_stale_delta_rejected_on_greek_clock_only(self):
        now_m = time.monotonic()
        snap = build_snapshot(
            stream_snap({OCC_A: make_quote(age=0.0, now_m=now_m)}, now_m=now_m),
            greek_snap({OCC_A: 0.35}, now_m=now_m, age=999.0), [OCC_A])
        c = self.only(snap)
        self.assertFalse(c.eligible)
        self.assertIn(R_DELTA_TOO_OLD, c.reasons)
        self.assertNotIn(R_QUOTE_TOO_OLD, c.reasons)
        self.assertLess(c.quote_age_seconds, 1.0)
        self.assertGreater(c.delta_age_seconds, 900)

    def test_stale_quote_with_fresh_delta_rejected_on_quote_clock_only(self):
        now_m = time.monotonic()
        snap = build_snapshot(
            stream_snap({OCC_A: make_quote(age=45.0, now_m=now_m)}, now_m=now_m),
            greek_snap({OCC_A: 0.35}, now_m=now_m, age=0.0), [OCC_A])
        c = self.only(snap)
        self.assertFalse(c.eligible)
        self.assertIn(R_QUOTE_TOO_OLD, c.reasons)
        self.assertNotIn(R_DELTA_TOO_OLD, c.reasons)

    def test_ceilings_are_independently_configurable(self):
        """Guards against a future refactor collapsing them into one number
        -- exactly what would let a stale delta ride a fresh quote."""
        now_m = time.monotonic()
        cfg = UniverseConfig(max_quote_age_seconds=100.0, max_greek_age_seconds=1.0)
        snap = build_snapshot(
            stream_snap({OCC_A: make_quote(age=50.0, now_m=now_m)}, now_m=now_m),
            greek_snap({OCC_A: 0.35}, now_m=now_m, age=50.0), [OCC_A], cfg)
        c = self.only(snap)
        self.assertNotIn(R_QUOTE_TOO_OLD, c.reasons)   # 50s < 100s
        self.assertIn(R_DELTA_TOO_OLD, c.reasons)      # 50s > 1s

    def test_missing_delta_reported_unavailable_not_stale(self):
        now_m = time.monotonic()
        snap = build_snapshot(stream_snap({OCC_A: make_quote(now_m=now_m)}, now_m=now_m),
                              greek_snap({}, now_m=now_m), [OCC_A])
        c = self.only(snap)
        self.assertIn(R_NO_DELTA, c.reasons)
        self.assertNotIn(R_DELTA_TOO_OLD, c.reasons)


class ReconnectTests(_Base):
    def test_reconnect_generation_invalidates_pre_reconnect_quotes(self):
        """After a reconnect the Terminal re-acks every subscription, so
        subscription_confirmed goes True again -- but a quote from the
        previous connection must NOT become eligible."""
        now_m = time.monotonic()
        snap = build_snapshot(
            stream_snap({OCC_A: make_quote(generation=1, age=0.0, now_m=now_m)},
                        subscribed={OCC_A: True}, generation=2, now_m=now_m),
            greek_snap({OCC_A: 0.35}, now_m=now_m), [OCC_A])
        c = self.only(snap)
        self.assertFalse(c.eligible)
        self.assertIn(R_STALE_GENERATION, c.reasons)
        self.assertTrue(c.subscription_confirmed)      # acked, yet ineligible
        self.assertLess(c.quote_age_seconds, 1.0)      # young, yet ineligible
        self.assertEqual((c.quote_generation, c.snapshot_generation), (1, 2))

    def test_fresh_post_reconnect_quote_is_eligible_again(self):
        now_m = time.monotonic()
        snap = build_snapshot(
            stream_snap({OCC_A: make_quote(generation=2, now_m=now_m)},
                        generation=2, now_m=now_m),
            greek_snap({OCC_A: 0.35}, now_m=now_m), [OCC_A])
        self.assertTrue(self.only(snap).eligible)

    def test_disconnected_stream_makes_everything_ineligible(self):
        now_m = time.monotonic()
        snap = build_snapshot(
            stream_snap({OCC_A: make_quote(now_m=now_m)}, connected=False, now_m=now_m),
            greek_snap({OCC_A: 0.35}, now_m=now_m), [OCC_A])
        c = self.only(snap)
        self.assertFalse(c.eligible)
        self.assertIn(R_DISCONNECTED, c.reasons)


class SubscriptionTests(_Base):
    def test_acknowledged_but_no_quote_ever_received(self):
        now_m = time.monotonic()
        snap = build_snapshot(
            stream_snap({OCC_A: None}, subscribed={OCC_A: True}, now_m=now_m),
            greek_snap({OCC_A: 0.35}, now_m=now_m), [OCC_A])
        c = self.only(snap)
        self.assertFalse(c.eligible)
        self.assertIn(R_NO_QUOTE, c.reasons)
        self.assertNotIn(R_NOT_SUBSCRIBED, c.reasons)  # ack is real; data is not
        self.assertTrue(c.subscription_confirmed)

    def test_unconfirmed_subscription_rejected(self):
        now_m = time.monotonic()
        snap = build_snapshot(
            stream_snap({OCC_A: make_quote(now_m=now_m)},
                        subscribed={OCC_A: False}, now_m=now_m),
            greek_snap({OCC_A: 0.35}, now_m=now_m), [OCC_A])
        self.assertIn(R_NOT_SUBSCRIBED, self.only(snap).reasons)


class QuoteQualityTests(_Base):
    def _snap(self, bid, ask, cfg=None):
        now_m = time.monotonic()
        return build_snapshot(
            stream_snap({OCC_A: make_quote(bid=bid, ask=ask, now_m=now_m)}, now_m=now_m),
            greek_snap({OCC_A: 0.35}, now_m=now_m), [OCC_A],
            cfg or UniverseConfig())

    def test_crossed_market_rejected(self):
        self.assertIn(R_CROSSED, self.only(self._snap(1.10, 1.00)).reasons)

    def test_locked_market_rejected_and_distinguished_from_crossed(self):
        c = self.only(self._snap(1.00, 1.00))
        self.assertIn(R_LOCKED, c.reasons)
        self.assertNotIn(R_CROSSED, c.reasons)

    def test_missing_bid_or_ask_rejected(self):
        for bid, ask in ((None, 1.04), (1.00, None), (None, None)):
            with self.subTest(bid=bid, ask=ask):
                self.assertIn(R_MISSING_SIDE, self.only(self._snap(bid, ask)).reasons)

    def test_zero_or_negative_prices_rejected(self):
        for bid, ask in ((0.0, 1.04), (1.00, 0.0), (-1.0, 1.04)):
            with self.subTest(bid=bid, ask=ask):
                self.assertIn(R_NONPOSITIVE, self.only(self._snap(bid, ask)).reasons)

    def test_wide_spread_passes_by_default_left_to_tested_selector(self):
        """Default config deliberately adds NO universe-level spread gate,
        so this layer cannot silently shrink the population relative to the
        frozen Variant B candidate."""
        c = self.only(self._snap(0.50, 2.00))
        self.assertTrue(c.eligible)
        self.assertNotIn(R_WIDE_SPREAD, c.reasons)

    def test_wide_spread_rejected_when_ceiling_explicitly_configured(self):
        c = self.only(self._snap(0.50, 2.00, UniverseConfig(max_spread_pct_mid=0.15)))
        self.assertIn(R_WIDE_SPREAD, c.reasons)


class OccNormalizationTests(_Base):
    def test_duplicate_occ_variants_collapse_to_one_candidate(self):
        now_m = time.monotonic()
        snap = build_snapshot(
            stream_snap({OCC_A: make_quote(now_m=now_m)}, now_m=now_m),
            greek_snap({OCC_A: 0.35}, now_m=now_m),
            [OCC_A, OCC_A.lower(), f"  {OCC_A}  "])
        self.assertEqual(len(snap.candidates), 1)
        self.assertEqual(snap.candidates[0].occ, OCC_A)

    def test_malformed_occ_skipped_not_crashed(self):
        now_m = time.monotonic()
        snap = build_snapshot(stream_snap({}, now_m=now_m), greek_snap({}, now_m=now_m),
                              ["NOT-AN-OCC", OCC_A])
        self.assertEqual([c.occ for c in snap.candidates], [OCC_A])

    def test_canonical_occ_round_trips(self):
        self.assertEqual(canonical_occ(OCC_A.lower()), OCC_A)


class FakeStream:
    def __init__(self, quotes, generation=1, connected=True):
        self.quotes, self.generation, self.connected = dict(quotes), generation, connected

    def snapshot(self, occs):
        occs = set(occs)
        return {"generation": self.generation, "connected": self.connected,
                "taken_monotonic": time.monotonic(),
                "taken_ts": dt.datetime.now(dt.timezone.utc),
                "quotes": {o: self.quotes.get(o) for o in occs},
                "subscribed": {o: o in self.quotes for o in occs}}


class FakeGreeks:
    def __init__(self, deltas):
        self.deltas = dict(deltas)

    def greek_snapshot(self, occs):
        now_m = time.monotonic()
        return {"taken_monotonic": now_m, "taken_ts": dt.datetime.now(dt.timezone.utc),
                "deltas": {o: {"delta": self.deltas[o], "source_monotonic": now_m,
                               "source_wall": dt.datetime.now(dt.timezone.utc)}
                           for o in occs if o in self.deltas}}


class AtomicityTests(_Base):
    """A snapshot is immutable once taken: data arriving mid-selection lands
    in the NEXT snapshot, never changing a decision partway through."""

    def test_quote_arriving_after_snapshot_does_not_change_that_selection(self):
        stream = FakeStream({OCC_A: make_quote(bid=1.00, ask=1.04)})
        snap = take_snapshot(stream, FakeGreeks({OCC_A: 0.35}), [OCC_A])
        before = snap.to_book().iloc[0].to_dict()
        stream.quotes[OCC_A] = make_quote(bid=5.00, ask=5.10)   # arrives mid-selection
        after = snap.to_book().iloc[0].to_dict()
        self.assertEqual(before, after)
        self.assertEqual(after["ask"], 1.04)

    def test_greek_refresh_after_snapshot_does_not_change_that_selection(self):
        greeks = FakeGreeks({OCC_A: 0.35})
        snap = take_snapshot(FakeStream({OCC_A: make_quote()}), greeks, [OCC_A])
        greeks.deltas[OCC_A] = 0.99                             # refresh mid-selection
        self.assertEqual(self.only(snap).delta, 0.35)

    def test_contract_removed_after_snapshot_does_not_change_that_selection(self):
        stream = FakeStream({OCC_A: make_quote()})
        snap = take_snapshot(stream, FakeGreeks({OCC_A: 0.35}), [OCC_A])
        del stream.quotes[OCC_A]                                # unsubscribed mid-selection
        c = self.only(snap)
        self.assertTrue(c.eligible)
        self.assertIsNotNone(c.bid)

    def test_contract_absent_at_snapshot_time_is_ineligible(self):
        snap = take_snapshot(FakeStream({}), FakeGreeks({OCC_A: 0.35}), [OCC_A])
        c = self.only(snap)
        self.assertFalse(c.eligible)
        self.assertIn(R_NO_QUOTE, c.reasons)


if __name__ == "__main__":
    unittest.main()
