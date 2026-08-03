import datetime as dt
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd

from thetadata_pipeline import collector as coll


def _fixture_df(n_rows=1):
    return pd.DataFrame([{"strike": 745.0, "right": "CALL", "price": 1.0} for _ in range(n_rows)])


class TimeChunksTests(unittest.TestCase):
    def test_splits_into_expected_number_of_chunks(self):
        chunks = coll._time_chunks(dt.time(9, 30), dt.time(10, 0), chunk_minutes=15)
        self.assertEqual(chunks, [(dt.time(9, 30), dt.time(9, 45)), (dt.time(9, 45), dt.time(10, 0))])

    def test_window_smaller_than_one_chunk_is_a_single_chunk(self):
        chunks = coll._time_chunks(dt.time(9, 30), dt.time(9, 33), chunk_minutes=15)
        self.assertEqual(chunks, [(dt.time(9, 30), dt.time(9, 33))])

    def test_uneven_final_chunk_is_clamped_to_end(self):
        chunks = coll._time_chunks(dt.time(9, 30), dt.time(9, 52), chunk_minutes=15)
        self.assertEqual(chunks, [(dt.time(9, 30), dt.time(9, 45)), (dt.time(9, 45), dt.time(9, 52))])

    def test_empty_window_produces_no_chunks(self):
        self.assertEqual(coll._time_chunks(dt.time(9, 30), dt.time(9, 30)), [])


class CollectTradeQuoteChunkingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_cursor_dir = coll.CURSOR_DIR
        coll.CURSOR_DIR = Path(self.tmp.name)
        self.universe = {
            "expirations": ["2026-07-24"],
            "contracts": [{"strike": 745.0}],
            "spot": 745.0,
        }

    def tearDown(self):
        coll.CURSOR_DIR = self.old_cursor_dir
        self.tmp.cleanup()

    def test_small_window_makes_exactly_one_call(self):
        # The normal live-cron case (~5min window) must still behave like a
        # single unchunked pull -- one bounded_call, one cursor save.
        now = dt.datetime(2026, 7, 24, 9, 35, tzinfo=coll.ET)
        with patch.object(coll, "get_client", return_value=MagicMock()), \
             patch.object(coll, "bounded_call", return_value=_fixture_df()) as mock_call, \
             patch.object(coll, "_free_ram_mb", return_value=2000.0):
            out = coll.collect_trade_quote("SPY", self.universe, dt.date(2026, 7, 24), now)
        self.assertEqual(mock_call.call_count, 1)
        self.assertEqual(len(out), 1)
        self.assertEqual(coll.load_cursor("SPY", dt.date(2026, 7, 24)), dt.time(9, 35))

    def test_wide_window_splits_into_multiple_chunks_and_cursor_advances_each_time(self):
        # A badly-stale cursor (e.g. after an outage) must not become one
        # giant call -- this is the exact scenario that drove real RAM from
        # 1.2GB to 376MB before this fix.
        now = dt.datetime(2026, 7, 24, 10, 30, tzinfo=coll.ET)  # 60min since session open
        cursor_calls = []
        original_save = coll.save_cursor

        def spy_save_cursor(symbol, today, end_time):
            cursor_calls.append(end_time)
            original_save(symbol, today, end_time)

        with patch.object(coll, "get_client", return_value=MagicMock()), \
             patch.object(coll, "bounded_call", return_value=_fixture_df()) as mock_call, \
             patch.object(coll, "_free_ram_mb", return_value=2000.0), \
             patch.object(coll, "save_cursor", side_effect=spy_save_cursor):
            out = coll.collect_trade_quote("SPY", self.universe, dt.date(2026, 7, 24), now)
        self.assertEqual(mock_call.call_count, 4)  # 60min / 15min chunks
        self.assertEqual(cursor_calls, [dt.time(9, 45), dt.time(10, 0), dt.time(10, 15), dt.time(10, 30)])
        self.assertEqual(len(out), 4)  # one fixture row appended per chunk

    def test_low_ram_aborts_mid_window_but_keeps_data_collected_so_far(self):
        now = dt.datetime(2026, 7, 24, 10, 30, tzinfo=coll.ET)
        ram_readings = [2000.0, 2000.0, 100.0]  # goes low on the 3rd chunk check

        def fake_free_ram():
            return ram_readings.pop(0) if ram_readings else 100.0

        with patch.object(coll, "get_client", return_value=MagicMock()), \
             patch.object(coll, "bounded_call", return_value=_fixture_df()) as mock_call, \
             patch.object(coll, "_free_ram_mb", side_effect=fake_free_ram):
            out = coll.collect_trade_quote("SPY", self.universe, dt.date(2026, 7, 24), now)
        self.assertEqual(mock_call.call_count, 2)  # stopped before the 3rd chunk's pull
        self.assertEqual(len(out), 2)
        # Cursor reflects only the chunks that actually completed.
        self.assertEqual(coll.load_cursor("SPY", dt.date(2026, 7, 24)), dt.time(10, 0))

    def test_a_failed_expiration_pull_does_not_block_the_chunk_or_later_chunks(self):
        now = dt.datetime(2026, 7, 24, 9, 45, tzinfo=coll.ET)
        with patch.object(coll, "get_client", return_value=MagicMock()), \
             patch.object(coll, "bounded_call", side_effect=coll.ThetaDataUnavailable("boom")), \
             patch.object(coll, "_free_ram_mb", return_value=2000.0):
            out = coll.collect_trade_quote("SPY", self.universe, dt.date(2026, 7, 24), now)
        self.assertTrue(out.empty)
        # Cursor still advances -- a single expiration's failure isn't fatal
        # to session continuity, matching the pre-existing tolerance for
        # per-expiration failures.
        self.assertEqual(coll.load_cursor("SPY", dt.date(2026, 7, 24)), dt.time(9, 45))


if __name__ == "__main__":
    unittest.main()
