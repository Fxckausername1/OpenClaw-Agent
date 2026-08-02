"""Tests for smc/signal_identity.py -- the frozen signal-key schema.

Every test here maps to a way a key could betray us: the same signal minting
two keys (double order) or two signals collapsing to one key (silent drop).
"""
from __future__ import annotations

import datetime as dt
import unittest
from zoneinfo import ZoneInfo

from smc.signal_identity import (
    MODEL_VERSION, SCHEMA_VERSION, STRATEGY, SignalIdentityError,
    bar_close_utc, canonical_utc, make_signal_identity, occurrence_for,
)

ET = ZoneInfo("America/New_York")
BAR = dt.datetime(2026, 8, 3, 9, 32, tzinfo=ET)


def ident(**kw):
    base = dict(symbol="QQQ", timeframe="1Min", bar_open=BAR, side="long",
                trigger="MSS")
    base.update(kw)
    return make_signal_identity(**base)


class NormalizationTests(unittest.TestCase):
    def test_equivalent_timestamp_renderings_hash_identically(self):
        """Z suffix, +00:00 offset, ET-local and aware-UTC are the same
        instant and must produce the same key."""
        utc = BAR.astimezone(dt.timezone.utc)
        forms = [BAR, utc, utc.isoformat(), utc.isoformat().replace("+00:00", "Z")]
        keys = {ident(bar_open=f).signal_key for f in forms}
        self.assertEqual(len(keys), 1)

    def test_microseconds_are_discarded(self):
        a = ident(bar_open=BAR)
        b = ident(bar_open=BAR.replace(microsecond=123456))
        self.assertEqual(a.signal_key, b.signal_key)

    def test_bar_close_is_open_plus_timeframe(self):
        self.assertEqual(bar_close_utc(dt.datetime(2026, 8, 3, 13, 32,
                                                   tzinfo=dt.timezone.utc), "1Min"),
                         "2026-08-03T13:33:00Z")

    def test_naive_treated_as_utc(self):
        self.assertEqual(canonical_utc(dt.datetime(2026, 8, 3, 13, 32)),
                         "2026-08-03T13:32:00Z")

    def test_unparseable_timestamp_rejected(self):
        with self.assertRaises(SignalIdentityError):
            canonical_utc("not a time")


class StabilityTests(unittest.TestCase):
    def test_same_event_before_and_after_restart(self):
        """Nothing process-local is in the key, so a restart cannot change
        it."""
        self.assertEqual(ident().signal_key, ident().signal_key)

    def test_key_is_independent_of_rolling_window_composition(self):
        """The schema has no bar_index, so a 6-session window and a
        3-session window mint the same key for the same bar -- the exact
        failure the old key had (offsets of 390 per session)."""
        a = ident()
        b = ident()
        self.assertEqual(a.signal_key, b.signal_key)
        self.assertNotIn("bar_index", a.as_dict())

    def test_dst_transition_is_stable(self):
        """US DST ends 2026-11-01. A bar either side must still hash by
        instant, not by local wall-clock text."""
        for wall in (dt.datetime(2026, 11, 1, 1, 30, tzinfo=ET),
                     dt.datetime(2026, 11, 2, 9, 32, tzinfo=ET)):
            with self.subTest(wall=wall):
                utc = wall.astimezone(dt.timezone.utc)
                self.assertEqual(ident(bar_open=wall).signal_key,
                                 ident(bar_open=utc).signal_key)

    def test_early_close_session_is_stable(self):
        """Nothing about session length enters the key."""
        early = dt.datetime(2026, 11, 27, 12, 59, tzinfo=ET)
        self.assertEqual(ident(bar_open=early).signal_key,
                         ident(bar_open=early.astimezone(dt.timezone.utc)).signal_key)

    def test_repeated_processing_yields_one_key(self):
        keys = {ident().signal_key for _ in range(50)}
        self.assertEqual(len(keys), 1)


class DiscriminationTests(unittest.TestCase):
    def test_long_and_short_on_same_bar_differ(self):
        self.assertNotEqual(ident(side="long").signal_key,
                            ident(side="short").signal_key)

    def test_different_trigger_same_bar_differs(self):
        self.assertNotEqual(ident(trigger="MSS").signal_key,
                            ident(trigger="BOS").signal_key)

    def test_different_symbol_differs(self):
        self.assertNotEqual(ident(symbol="QQQ").signal_key,
                            ident(symbol="SPY").signal_key)

    def test_different_timeframe_differs(self):
        self.assertNotEqual(ident(timeframe="1Min").signal_key,
                            ident(timeframe="5Min").signal_key)

    def test_different_bar_differs(self):
        self.assertNotEqual(
            ident().signal_key,
            ident(bar_open=BAR + dt.timedelta(minutes=1)).signal_key)

    def test_model_version_change_differs(self):
        self.assertNotEqual(ident().signal_key,
                            ident(model_version="v2.3").signal_key)

    def test_occurrence_discriminates_same_side_same_bar(self):
        self.assertNotEqual(ident(occurrence=0).signal_key,
                            ident(occurrence=1).signal_key)

    def test_occurrence_for_picks_lowest_unused(self):
        seen = {ident(occurrence=0).signal_key}
        self.assertEqual(occurrence_for(seen, symbol="QQQ", timeframe="1Min",
                                        bar_open=BAR, side="long", trigger="MSS"), 1)


class ValidationTests(unittest.TestCase):
    def test_bad_side_rejected(self):
        for bad in ("buy", "", None, "LONGISH"):
            with self.subTest(bad=bad):
                with self.assertRaises(SignalIdentityError):
                    ident(side=bad)

    def test_bad_symbol_rejected(self):
        for bad in ("", "qqq123", "TOOLONGSYM", None):
            with self.subTest(bad=bad):
                with self.assertRaises(SignalIdentityError):
                    ident(symbol=bad)

    def test_missing_trigger_rejected(self):
        with self.assertRaises(SignalIdentityError):
            ident(trigger="")

    def test_unknown_timeframe_rejected(self):
        with self.assertRaises(SignalIdentityError):
            ident(timeframe="3Min")

    def test_negative_occurrence_rejected(self):
        with self.assertRaises(SignalIdentityError):
            ident(occurrence=-1)


class DiagnosabilityTests(unittest.TestCase):
    def test_unhashed_fields_stored_alongside_hash(self):
        i = ident()
        d = i.as_dict()
        for f in ("strategy", "model_version", "symbol", "timeframe",
                  "bar_close_utc", "side", "trigger", "occurrence", "signal_key"):
            self.assertIn(f, d)
        self.assertEqual(i.strategy, STRATEGY)
        self.assertEqual(i.model_version, MODEL_VERSION)
        self.assertEqual(i.schema, SCHEMA_VERSION)

    def test_fields_tuple_is_exactly_what_was_hashed(self):
        i = ident()
        self.assertEqual(i.fields[0], SCHEMA_VERSION)
        self.assertIn(i.bar_close_utc, i.fields)
        self.assertEqual(len(i.fields), 9)

    def test_excluded_fields_absent(self):
        d = ident().as_dict()
        for banned in ("bar_index", "sequence", "seq", "rowid", "id",
                       "received_ts", "receipt_ts"):
            self.assertNotIn(banned, d)


if __name__ == "__main__":
    unittest.main()
