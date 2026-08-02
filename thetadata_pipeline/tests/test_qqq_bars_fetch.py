"""Tests for qqq_bars_fetch.py's pure (non-network) logic: session-list
extraction from the real backfill manifest shape, and load_all_bars'
concatenation/sort/filter behavior against local parquet fixtures. The
Alpaca HTTP call itself is exercised by the real 62-session pull (see the
B1 report for real row counts/gaps) -- deliberately not re-mocked here,
same convention as this codebase's other Alpaca-fetch modules, which don't
carry HTTP-mocked unit tests either."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from thetadata_pipeline.qqq_bars_fetch import list_target_sessions, load_all_bars, load_session_bars


class ListTargetSessionsTests(unittest.TestCase):
    def test_filters_to_symbol_and_dedupes_sorted(self):
        with tempfile.TemporaryDirectory() as d:
            manifest_path = Path(d) / "manifest.json"
            manifest_path.write_text(json.dumps({
                "sessions": [
                    {"symbol": "QQQ", "date": "2026-05-02"},
                    {"symbol": "SPY", "date": "2026-05-01"},
                    {"symbol": "QQQ", "date": "2026-05-01"},
                    {"symbol": "QQQ", "date": "2026-05-01"},  # duplicate
                ],
            }))
            dates = list_target_sessions(manifest_path, symbol="QQQ")
            self.assertEqual(dates, ["2026-05-01", "2026-05-02"])


class LoadBarsTests(unittest.TestCase):
    def test_load_all_bars_concatenates_in_date_order(self):
        with tempfile.TemporaryDirectory() as d:
            raw_dir = Path(d)
            for date, price in (("2026-05-02", 200.0), ("2026-05-01", 100.0)):
                out_dir = raw_dir / "QQQ" / date
                out_dir.mkdir(parents=True)
                df = pd.DataFrame({
                    "t": pd.to_datetime([f"{date}T09:30:00-04:00"]),
                    "o": [price], "h": [price], "l": [price], "c": [price], "v": [10.0],
                })
                df.to_parquet(out_dir / "bars.parquet", index=False)
            out = load_all_bars("QQQ", ["2026-05-01", "2026-05-02"], raw_dir)
            self.assertEqual(len(out), 2)
            self.assertTrue(out["t"].is_monotonic_increasing)
            self.assertAlmostEqual(out.iloc[0]["c"], 100.0)
            self.assertAlmostEqual(out.iloc[1]["c"], 200.0)

    def test_load_session_bars_missing_file_returns_empty_frame(self):
        with tempfile.TemporaryDirectory() as d:
            out = load_session_bars("QQQ", "2026-01-01", Path(d))
            self.assertTrue(out.empty)


if __name__ == "__main__":
    unittest.main()
