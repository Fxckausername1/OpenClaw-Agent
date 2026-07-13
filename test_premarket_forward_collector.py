import json
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from premarket_forward_collector import (
    FORWARD_GATE,
    PROTOCOL_VERSION,
    ET,
    seal_dataset,
    session_bounds,
    universe_hash,
    validate_schedule,
    verify_manifest,
)


class TimeContractTests(unittest.TestCase):
    def test_session_bounds_follow_dst(self):
        summer_start, summer_open = session_bounds(date(2026, 7, 13))
        winter_start, winter_open = session_bounds(date(2026, 1, 13))
        self.assertEqual(summer_start.astimezone(ZoneInfo("UTC")).hour, 8)
        self.assertEqual(summer_open.astimezone(ZoneInfo("UTC")).hour, 13)
        self.assertEqual(winter_start.astimezone(ZoneInfo("UTC")).hour, 9)
        self.assertEqual(winter_open.astimezone(ZoneInfo("UTC")).hour, 14)

    def test_capture_window_is_enforced(self):
        day = date(2026, 7, 13)
        validate_schedule("capture", day, datetime(2026, 7, 13, 9, 15, tzinfo=ET), False)
        with self.assertRaises(RuntimeError):
            validate_schedule("capture", day, datetime(2026, 7, 13, 9, 25, tzinfo=ET), False)

    def test_sip_backfill_window_is_after_close(self):
        day = date(2026, 7, 13)
        validate_schedule("sip-backfill", day, datetime(2026, 7, 13, 16, 30, tzinfo=ET), False)
        with self.assertRaises(RuntimeError):
            validate_schedule("sip-backfill", day, datetime(2026, 7, 13, 9, 50, tzinfo=ET), False)

    def test_forward_gate_is_fixed_before_capture(self):
        self.assertEqual(PROTOCOL_VERSION, "premarket-forward-2026-07-13.2")
        self.assertEqual(FORWARD_GATE["minimum_sealed_market_days"], 120)
        self.assertEqual(FORWARD_GATE["minimum_eligible_observations"], 250)
        self.assertEqual(FORWARD_GATE["minimum_quote_coverage"], 0.90)
        self.assertEqual(FORWARD_GATE["maximum_required_field_missing_rate"], 0.10)
        self.assertTrue(FORWARD_GATE["requires_disjoint_future_sample"])
        self.assertTrue(FORWARD_GATE["daily_block_bootstrap_lower_95pct_positive_at_6bp"])


class SealTests(unittest.TestCase):
    def test_seal_is_hash_verified_and_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            result = seal_dataset(path, "sample", [{"record_type": "bar", "symbol": "AAPL"}], {"test": True})
            self.assertEqual(result["status"], "sealed")
            manifest = verify_manifest(path / "sample.manifest.json")
            self.assertTrue(manifest["sealed"])
            second = seal_dataset(path, "sample", [{"different": True}], {"test": False})
            self.assertEqual(second["status"], "already_sealed")

    def test_tampering_is_detected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            seal_dataset(path, "sample", [{"value": 1}], {"test": True})
            (path / "sample.jsonl").write_text(json.dumps({"value": 2}) + "\n")
            with self.assertRaises(RuntimeError):
                verify_manifest(path / "sample.manifest.json")

    def test_universe_hash_is_order_sensitive_only_after_canonical_sort(self):
        self.assertEqual(universe_hash(sorted(["MSFT", "AAPL"])), universe_hash(["AAPL", "MSFT"]))


if __name__ == "__main__":
    unittest.main()
