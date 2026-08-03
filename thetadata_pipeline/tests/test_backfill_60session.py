"""Tests for backfill_60session.py. Every test passes an EXPLICIT tempdir
path for manifest_path/progress_path/raw_dir -- never the module's real
production defaults (BACKFILL_DIR/MANIFEST_PATH/PROGRESS_PATH/RAW_DIR).
Real incident this convention guards against, found live in this same
session: test_bt1_pilot.py once wrote to the real production BT-1 manifest
path by omission, silently clobbering heff's actual graded 5-session pilot
data. Never repeated here."""

import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

import thetadata_pipeline.backfill_60session as bf60
from thetadata_pipeline.bt1_manifest import GRADE_FAIL, GRADE_PASS


def _fake_trade_quote_df(n=10):
    now = pd.Timestamp("2026-07-20 10:00:00", tz="UTC")
    rows = []
    for i in range(n):
        rows.append(dict(
            symbol="SPY", expiration="2026-07-20", strike=745.0, right="C",
            trade_timestamp=now + pd.Timedelta(seconds=i), quote_timestamp=now + pd.Timedelta(seconds=i),
            sequence=i, condition=0, size=1, exchange="X", price=3.55,
            bid_size=5, bid_exchange="X", bid=3.45, bid_condition=0,
            ask_size=5, ask_exchange="X", ask=3.55, ask_condition=0,
        ))
    return pd.DataFrame(rows)


def _fake_iv_df(n=3):
    return pd.DataFrame({"strike": [745.0] * n, "right": ["C"] * n,
                          "implied_vol": [0.2] * n, "delta": [0.35] * n})


def _fake_oi_df(n=3):
    return pd.DataFrame({"strike": [745.0] * n, "right": ["C"] * n, "open_interest": [100] * n,
                          "expiration": ["2026-07-20"] * n})


def _fake_bars_df(n=390):
    idx = pd.date_range("2026-07-20 13:30", periods=n, freq="1min", tz="UTC")
    return pd.DataFrame({"t": idx, "o": 700.0, "h": 700.5, "l": 699.5, "c": 700.1, "v": 1000})


SESSION = {"date": "2026-07-20", "open": "09:30", "close": "16:00", "is_early_close": False}


class PullAndPersistSessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.raw_dir = Path(self.tmp.name) / "raw"
        # REAL BUG this fixes, found live 2026-07-26: an earlier version of
        # this setUp only overrode raw_dir, leaving pull_and_persist_session's
        # backfill_dir parameter at its default (the real production
        # data/thetadata/backfill_60session/ directory). Every run of this
        # test class was writing synthetic OI/IV fixture files into that
        # real directory -- confirmed live (oi_SPY_2026-07-20.json /
        # iv_SPY_2026-07-20.json appeared there with fake oi=100/delta=0.35
        # values). backfill_dir must ALWAYS be passed explicitly here too,
        # not just raw_dir.
        self.backfill_dir = Path(self.tmp.name) / "backfill"
        self.client = mock.Mock()
        self.client.option_history_trade_quote.return_value = _fake_trade_quote_df()
        self.client.option_history_open_interest.return_value = _fake_oi_df()
        self.client.option_history_greeks_eod.return_value = _fake_iv_df()

        patches = [
            mock.patch.object(bf60, "get_client", return_value=self.client),
            mock.patch.object(bf60, "active_expirations", return_value=[dt.date(2026, 7, 20)]),
            mock.patch.object(bf60, "get_spot_price", return_value=745.0),
            mock.patch.object(bf60, "strike_window", return_value=[740.0, 745.0, 750.0]),
            mock.patch.object(bf60, "fetch_underlying_bars", return_value=_fake_bars_df()),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)

    def test_grades_pass_on_clean_pull(self):
        row = bf60.pull_and_persist_session("SPY", SESSION, raw_dir=self.raw_dir, backfill_dir=self.backfill_dir)
        self.assertEqual(row["quality_grade"], GRADE_PASS)
        self.assertGreater(row["received"]["option_trade_quote_rows"], 0)
        self.assertGreater(row["received"]["option_greeks_rows"], 0)

    def test_actually_persists_raw_parquet_rows(self):
        # The real gap this module exists to close vs bt1_pilot.py: the
        # classified trade rows must land on disk, not just get counted.
        row = bf60.pull_and_persist_session("SPY", SESSION, raw_dir=self.raw_dir, backfill_dir=self.backfill_dir)
        self.assertGreater(row["persisted_raw_rows"], 0)
        part_dir = self.raw_dir / "SPY" / "2026-07-20"
        parts = list(part_dir.glob("part-*.parquet"))
        self.assertGreater(len(parts), 0)
        combined = pd.concat([pd.read_parquet(p) for p in parts], ignore_index=True)
        self.assertEqual(len(combined), row["received"]["option_trade_quote_rows"])
        self.assertIn("contract_id", combined.columns)  # already classified, not raw ThetaData rows

    def test_no_expiration_fails_and_persists_nothing(self):
        with mock.patch.object(bf60, "active_expirations", return_value=[]):
            row = bf60.pull_and_persist_session("SPY", SESSION, raw_dir=self.raw_dir, backfill_dir=self.backfill_dir)
        self.assertEqual(row["quality_grade"], GRADE_FAIL)
        self.assertEqual(row["persisted_raw_rows"], 0)
        self.assertFalse((self.raw_dir / "SPY").exists())


class RunSixtySessionBackfillTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.manifest_path = base / "manifest.json"
        self.progress_path = base / "progress.json"
        self.raw_dir = base / "raw"
        self.addCleanup(self.tmp.cleanup)

        self.client = mock.Mock()
        self.client.option_history_trade_quote.return_value = _fake_trade_quote_df()
        self.client.option_history_open_interest.return_value = _fake_oi_df()
        self.client.option_history_greeks_eod.return_value = _fake_iv_df()
        patches = [
            mock.patch.object(bf60, "get_client", return_value=self.client),
            mock.patch.object(bf60, "active_expirations", return_value=[dt.date(2026, 7, 20)]),
            mock.patch.object(bf60, "get_spot_price", return_value=745.0),
            mock.patch.object(bf60, "strike_window", return_value=[740.0, 745.0, 750.0]),
            mock.patch.object(bf60, "fetch_underlying_bars", return_value=_fake_bars_df()),
            mock.patch.object(bf60, "most_recent_complete_sessions", return_value=[SESSION]),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_end_to_end_writes_manifest(self):
        result = bf60.run_60session_backfill(
            symbols=("SPY", "QQQ"), sessions=1,
            manifest_path=self.manifest_path, progress_path=self.progress_path, raw_dir=self.raw_dir,
        )
        self.assertEqual(len(result["sessions"]), 2)  # SPY + QQQ, 1 session each
        self.assertEqual(result["overall"]["sessions_total"], 2)
        self.assertTrue(self.manifest_path.exists())
        self.assertTrue(self.progress_path.exists())

    def test_resumability_skips_already_completed_units(self):
        bf60._save_progress({"completed": ["SPY:2026-07-20"]}, self.progress_path)
        bf60.run_60session_backfill(
            symbols=("SPY", "QQQ"), sessions=1,
            manifest_path=self.manifest_path, progress_path=self.progress_path, raw_dir=self.raw_dir,
        )
        # SPY should have been skipped (already in progress) -- only QQQ's
        # session actually pulled trade_quote data.
        self.assertEqual(self.client.option_history_trade_quote.call_count, len(bf60._session_chunks()))

    def test_second_run_is_a_true_no_op_when_everything_is_already_done(self):
        bf60.run_60session_backfill(
            symbols=("SPY", "QQQ"), sessions=1,
            manifest_path=self.manifest_path, progress_path=self.progress_path, raw_dir=self.raw_dir,
        )
        call_count_after_first_run = self.client.option_history_trade_quote.call_count
        bf60.run_60session_backfill(
            symbols=("SPY", "QQQ"), sessions=1,
            manifest_path=self.manifest_path, progress_path=self.progress_path, raw_dir=self.raw_dir,
        )
        self.assertEqual(self.client.option_history_trade_quote.call_count, call_count_after_first_run)


class RunSixtySessionBackfillBeforeParamTests(unittest.TestCase):
    """Tests for the 2026-07-29 `before` addition: explicit-earlier-date-range
    support, added so a second backfill run can extend the real dataset
    BACKWARD from a fixed date instead of always sliding forward from
    'today'. Every existing test above passes no `before` at all and must
    keep passing unchanged -- that's the real regression this addition must
    never cause."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.manifest_path = base / "manifest.json"
        self.progress_path = base / "progress.json"
        self.raw_dir = base / "raw"
        self.addCleanup(self.tmp.cleanup)

        self.client = mock.Mock()
        self.client.option_history_trade_quote.return_value = _fake_trade_quote_df()
        self.client.option_history_open_interest.return_value = _fake_oi_df()
        self.client.option_history_greeks_eod.return_value = _fake_iv_df()
        self.most_recent_mock = mock.Mock(return_value=[SESSION])
        patches = [
            mock.patch.object(bf60, "get_client", return_value=self.client),
            mock.patch.object(bf60, "active_expirations", return_value=[dt.date(2026, 7, 20)]),
            mock.patch.object(bf60, "get_spot_price", return_value=745.0),
            mock.patch.object(bf60, "strike_window", return_value=[740.0, 745.0, 750.0]),
            mock.patch.object(bf60, "fetch_underlying_bars", return_value=_fake_bars_df()),
            mock.patch.object(bf60, "most_recent_complete_sessions", self.most_recent_mock),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_default_call_omits_before_unchanged(self):
        # No `before` passed at all -- must call most_recent_complete_sessions
        # exactly as every pre-existing caller already does.
        bf60.run_60session_backfill(
            symbols=("QQQ",), sessions=1,
            manifest_path=self.manifest_path, progress_path=self.progress_path, raw_dir=self.raw_dir,
        )
        self.most_recent_mock.assert_called_once_with(1, before=None)

    def test_explicit_before_is_passed_through(self):
        before = dt.date(2026, 4, 29)
        bf60.run_60session_backfill(
            symbols=("QQQ",), sessions=20, before=before,
            manifest_path=self.manifest_path, progress_path=self.progress_path, raw_dir=self.raw_dir,
        )
        self.most_recent_mock.assert_called_once_with(20, before=before)

    def test_before_is_recorded_in_manifest_scope(self):
        before = dt.date(2026, 4, 29)
        result = bf60.run_60session_backfill(
            symbols=("QQQ",), sessions=1, before=before,
            manifest_path=self.manifest_path, progress_path=self.progress_path, raw_dir=self.raw_dir,
        )
        self.assertEqual(result["backfill_scope"]["before"], "2026-04-29")

    def test_omitted_before_records_null_in_manifest_scope(self):
        result = bf60.run_60session_backfill(
            symbols=("QQQ",), sessions=1,
            manifest_path=self.manifest_path, progress_path=self.progress_path, raw_dir=self.raw_dir,
        )
        self.assertIsNone(result["backfill_scope"]["before"])

    def test_earlier_extension_merges_with_existing_later_sessions_no_duplication(self):
        # Simulates the real overnight sequence: an original run already
        # covers a later session, then a second run with an earlier `before`
        # adds an earlier one. Both must coexist in one manifest, and
        # re-running the exact same earlier extension again must not
        # duplicate its own row (same "retried unit replaces, not
        # accumulates" rule the 2026-07-29 dedupfix already enforces for
        # same-date retries).
        later_session = {"date": "2026-04-29", "open": "09:30", "close": "16:00", "is_early_close": False}
        earlier_session = {"date": "2026-04-28", "open": "09:30", "close": "16:00", "is_early_close": False}

        self.most_recent_mock.return_value = [later_session]
        bf60.run_60session_backfill(
            symbols=("QQQ",), sessions=1,
            manifest_path=self.manifest_path, progress_path=self.progress_path, raw_dir=self.raw_dir,
        )

        self.most_recent_mock.return_value = [earlier_session]
        bf60.run_60session_backfill(
            symbols=("QQQ",), sessions=1, before=dt.date(2026, 4, 29),
            manifest_path=self.manifest_path, progress_path=self.progress_path, raw_dir=self.raw_dir,
        )

        result = json.loads(self.manifest_path.read_text())
        dates = sorted(s["date"] for s in result["sessions"])
        self.assertEqual(dates, ["2026-04-28", "2026-04-29"])

        # Re-running the earlier extension again must be a true no-op (already
        # in progress.json) and must not duplicate the 2026-04-28 row.
        bf60.run_60session_backfill(
            symbols=("QQQ",), sessions=1, before=dt.date(2026, 4, 29),
            manifest_path=self.manifest_path, progress_path=self.progress_path, raw_dir=self.raw_dir,
        )
        result = json.loads(self.manifest_path.read_text())
        dates = sorted(s["date"] for s in result["sessions"])
        self.assertEqual(dates, ["2026-04-28", "2026-04-29"])


if __name__ == "__main__":
    unittest.main()
