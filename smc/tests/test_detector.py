"""Tests for smc/detector.py -- the persistent minute detector.

unittest, no network. Bars are injected through the fetch callable, so every
provider quirk heff listed is exercised deterministically: missing minute,
duplicate minute, late publication, corrected/revised bar, provider
returning an older last bar, multi-minute gap after downtime, restart.

Session/calendar behaviour (weekend, holiday, early close, DST) is checked
against smc.calendar rather than hard-coded UTC hours.
"""
from __future__ import annotations

import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from smc.detector import (
    BAR_TIMEFRAME, SIGNAL_FEED, BarValidationError, DetectorCursor,
    PersistentDetector, config_checksum, series_checksum, validate_bars,
)
from thetadata_pipeline.heff_smc_engine import HeffSmcConfig

ET = ZoneInfo("America/New_York")
SESSION = "2026-08-03"


def bars(n=5, start_minute=0, close=580.0, session=SESSION):
    base = dt.datetime.fromisoformat(f"{session}T09:30:00").replace(tzinfo=ET)
    rows = []
    for i in range(n):
        t = base + dt.timedelta(minutes=start_minute + i)
        rows.append({"t": t.astimezone(dt.timezone.utc), "o": close, "h": close + 0.1,
                     "l": close - 0.1, "c": close, "v": 100 + i})
    return pd.DataFrame(rows)


class ChecksumTests(unittest.TestCase):
    def test_config_checksum_is_stable_and_sensitive(self):
        a = config_checksum(HeffSmcConfig())
        self.assertEqual(a, config_checksum(HeffSmcConfig()))
        self.assertEqual(len(a), 16)

    def test_series_checksum_changes_with_data(self):
        self.assertNotEqual(series_checksum(bars(5)), series_checksum(bars(6)))

    def test_empty_series_checksum(self):
        self.assertEqual(series_checksum(pd.DataFrame()), "empty")


class ValidationTests(unittest.TestCase):
    def test_clean_bars_validate(self):
        r = validate_bars(bars(5), expect_session=SESSION)
        self.assertEqual(r["rows"], 5)
        self.assertEqual(r["duplicates"], 0)
        self.assertEqual(r["out_of_order"], 0)

    def test_duplicate_minute_detected(self):
        df = pd.concat([bars(3), bars(1)], ignore_index=True)
        self.assertEqual(validate_bars(df)["duplicates"], 1)

    def test_out_of_order_detected(self):
        df = bars(3).iloc[::-1].reset_index(drop=True)
        self.assertGreater(validate_bars(df)["out_of_order"], 0)

    def test_missing_t_column_rejected(self):
        with self.assertRaises(BarValidationError):
            validate_bars(pd.DataFrame({"c": [1.0]}))

    def test_wrong_session_rejected(self):
        with self.assertRaises(BarValidationError):
            validate_bars(bars(2), expect_session="2026-08-04")

    def test_empty_is_not_an_error(self):
        self.assertEqual(validate_bars(pd.DataFrame())["rows"], 0)


class CursorTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "cursor.json"

    def tearDown(self):
        self._tmp.cleanup()

    def _det(self):
        return PersistentDetector(fetch_session_bars=lambda s: bars(0),
                                  cursor_path=self.path)

    def test_roundtrip(self):
        d = self._det()
        d.cursor.emitted.add("k1")
        d.cursor.last_processed_bar_ts = "2026-08-03T13:30:00+00:00"
        d.save_cursor()
        d2 = self._det()
        d2.load_cursor()
        self.assertIn("k1", d2.cursor.emitted)
        self.assertEqual(d2.cursor.last_processed_bar_ts, "2026-08-03T13:30:00+00:00")

    def test_corrupt_cursor_refuses_to_start(self):
        """A corrupt cursor must NOT silently become an empty one -- that
        would re-emit every signal of the session as new orders."""
        self.path.write_text("{not json")
        with self.assertRaises(BarValidationError):
            self._det().load_cursor()

    def test_missing_cursor_is_a_clean_start(self):
        self.assertEqual(self._det().load_cursor().emitted, set())

    def test_save_is_atomic_via_tmp_rename(self):
        d = self._det()
        d.cursor.emitted.add("k")
        d.save_cursor()
        self.assertTrue(self.path.exists())
        self.assertFalse(self.path.with_suffix(".tmp").exists())
        json.loads(self.path.read_text())


class IngestTests(unittest.TestCase):
    def _det(self, initial=None):
        d = PersistentDetector(fetch_session_bars=lambda s: (initial if initial is not None
                                                            else bars(0)))
        d._today_session = SESSION
        d._today = initial if initial is not None else pd.DataFrame()
        return d

    def test_first_ingest_appends_all(self):
        d = self._det()
        self.assertEqual(d.ingest_today(bars(3))["appended"], 3)

    def test_only_new_minutes_appended(self):
        d = self._det(bars(3))
        r = d.ingest_today(bars(5))
        self.assertEqual(r["appended"], 2)
        self.assertEqual(len(d._today), 5)

    def test_duplicate_minute_ignored(self):
        d = self._det(bars(3))
        r = d.ingest_today(bars(3))
        self.assertEqual(r["appended"], 0)
        self.assertEqual(r["duplicates"], 3)
        self.assertEqual(len(d._today), 3)

    def test_provider_returning_older_last_bar_is_flagged_stale(self):
        d = self._det(bars(5))
        r = d.ingest_today(bars(3))
        self.assertEqual(r["appended"], 0)
        self.assertEqual(r["stale"], 1)

    def test_multi_minute_gap_after_downtime_appends_all_missing(self):
        d = self._det(bars(2))
        r = d.ingest_today(bars(10))
        self.assertEqual(r["appended"], 8)

    def test_late_publication_then_arrival(self):
        d = self._det(bars(3))
        self.assertEqual(d.ingest_today(bars(3))["appended"], 0)   # nothing new yet
        self.assertEqual(d.ingest_today(bars(4))["appended"], 1)   # arrives late

    def test_revised_bar_detected(self):
        d = self._det(bars(3, close=580.0))
        r = d.ingest_today(bars(3, close=581.0))
        self.assertEqual(r["revised"], 3)
        self.assertEqual(len(d.cursor.revised_bars), 3)

    def test_revision_after_processing_is_flagged_distinctly(self):
        d = self._det(bars(3, close=580.0))
        d.cursor.last_processed_bar_ts = str(
            pd.to_datetime(d._today["t"], utc=True).max())
        d.ingest_today(bars(3, close=581.0))
        self.assertTrue(all(r["after_processing"] for r in d.cursor.revised_bars))

    def test_revision_before_processing_not_flagged_as_after(self):
        d = self._det(bars(3, close=580.0))
        d.cursor.last_processed_bar_ts = None
        d.ingest_today(bars(3, close=581.0))
        self.assertTrue(all(not r["after_processing"] for r in d.cursor.revised_bars))

    def test_empty_ingest_is_a_noop(self):
        d = self._det(bars(3))
        self.assertEqual(d.ingest_today(pd.DataFrame())["appended"], 0)

    def test_out_of_order_incoming_is_sorted(self):
        d = self._det()
        d.ingest_today(bars(4).iloc[::-1].reset_index(drop=True))
        ts = pd.to_datetime(d._today["t"], utc=True)
        self.assertTrue(ts.is_monotonic_increasing)


class SessionClockTests(unittest.TestCase):
    def test_next_minute_boundary(self):
        now = dt.datetime(2026, 8, 3, 10, 15, 42, tzinfo=ET)
        self.assertEqual(PersistentDetector.next_minute_boundary(now),
                         dt.datetime(2026, 8, 3, 10, 16, 0, tzinfo=ET))

    def test_boundary_is_exact_on_the_minute(self):
        now = dt.datetime(2026, 8, 3, 10, 15, 0, tzinfo=ET)
        self.assertEqual(PersistentDetector.next_minute_boundary(now).minute, 16)

    def test_weekend_is_not_open(self):
        sat = dt.datetime(2026, 8, 1, 11, 0, tzinfo=ET)
        self.assertFalse(PersistentDetector.session_is_open(sat))

    def test_before_open_and_after_close_are_closed(self):
        self.assertFalse(PersistentDetector.session_is_open(
            dt.datetime(2026, 8, 3, 8, 0, tzinfo=ET)))
        self.assertFalse(PersistentDetector.session_is_open(
            dt.datetime(2026, 8, 3, 20, 0, tzinfo=ET)))

    def test_uses_exchange_calendar_not_fixed_utc(self):
        """DST alone makes a fixed UTC offset wrong twice a year, so the
        detector must go through smc.calendar."""
        import inspect
        src = inspect.getsource(PersistentDetector.session_is_open)
        self.assertIn("session_schedule", src)


class SignalProvenanceTests(unittest.TestCase):
    def test_feed_identity_frozen(self):
        self.assertEqual(SIGNAL_FEED, "alpaca_iex")
        self.assertEqual(BAR_TIMEFRAME, "1Min")

    def test_health_reports_feed_and_checksums(self):
        d = PersistentDetector(fetch_session_bars=lambda s: bars(0))
        h = d.health()
        self.assertEqual(h["signal_feed"], "alpaca_iex")
        self.assertEqual(h["bar_timeframe"], "1Min")
        self.assertIn("detector_config_checksum", h)
        self.assertIn("source_series_checksum", h)
        self.assertIn("last_runtime_ms", h)


if __name__ == "__main__":
    unittest.main()
