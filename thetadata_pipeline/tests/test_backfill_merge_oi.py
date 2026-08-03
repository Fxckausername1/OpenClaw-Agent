import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path

from thetadata_pipeline import backfill as bf


class MergeWriteOiTests(unittest.TestCase):
    def test_merges_into_existing_file_without_dropping_prior_entries(self):
        day = dt.date(2026, 7, 24)
        with tempfile.TemporaryDirectory() as tmp:
            old_data = bf.DATA
            bf.DATA = Path(tmp)
            try:
                path = bf.DATA / f"oi_SPY_{day.isoformat()}.json"
                path.write_text(json.dumps({"oi": {"0DTE_CONTRACT": 500.0}, "oi_as_of_session": day.isoformat()}))
                bf._merge_write_oi("SPY", day, {"WEEKLY_CONTRACT": 1200.0})
                result = json.loads(path.read_text())
            finally:
                bf.DATA = old_data
        self.assertEqual(result["oi"]["0DTE_CONTRACT"], 500.0)
        self.assertEqual(result["oi"]["WEEKLY_CONTRACT"], 1200.0)

    def test_creates_file_when_none_exists_yet(self):
        day = dt.date(2026, 7, 24)
        with tempfile.TemporaryDirectory() as tmp:
            old_data = bf.DATA
            bf.DATA = Path(tmp)
            try:
                bf._merge_write_oi("QQQ", day, {"C1": 42.0})
                path = bf.DATA / f"oi_QQQ_{day.isoformat()}.json"
                result = json.loads(path.read_text())
            finally:
                bf.DATA = old_data
        self.assertEqual(result["oi"], {"C1": 42.0})

    def test_overwrites_same_contract_id_on_rerun_idempotently(self):
        day = dt.date(2026, 7, 24)
        with tempfile.TemporaryDirectory() as tmp:
            old_data = bf.DATA
            bf.DATA = Path(tmp)
            try:
                bf._merge_write_oi("SPY", day, {"C1": 100.0})
                bf._merge_write_oi("SPY", day, {"C1": 200.0})  # re-run with an updated value
                path = bf.DATA / f"oi_SPY_{day.isoformat()}.json"
                result = json.loads(path.read_text())
            finally:
                bf.DATA = old_data
        self.assertEqual(result["oi"]["C1"], 200.0)

    def test_survives_a_corrupt_existing_file(self):
        day = dt.date(2026, 7, 24)
        with tempfile.TemporaryDirectory() as tmp:
            old_data = bf.DATA
            bf.DATA = Path(tmp)
            try:
                path = bf.DATA / f"oi_SPY_{day.isoformat()}.json"
                path.write_text("{not valid json")
                bf._merge_write_oi("SPY", day, {"C1": 1.0})
                result = json.loads(path.read_text())
            finally:
                bf.DATA = old_data
        self.assertEqual(result["oi"], {"C1": 1.0})


if __name__ == "__main__":
    unittest.main()
