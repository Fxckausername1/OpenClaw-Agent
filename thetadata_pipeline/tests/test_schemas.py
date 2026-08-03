import datetime as dt
import unittest

from thetadata_pipeline.schemas import (
    contract_id, mills_to_strike, normalize_right, source_health,
    strike_to_mills,
)


class StrikeMillsTests(unittest.TestCase):
    def test_roundtrip(self):
        for strike in (330.0, 330.5, 0.5, 750.0, 12.345):
            self.assertAlmostEqual(mills_to_strike(strike_to_mills(strike)), strike, places=6)

    def test_no_float_drift_on_common_strikes(self):
        # 0.1 + 0.2 style float issues would silently produce two different
        # contract_ids for what should be the same strike.
        self.assertEqual(strike_to_mills(100.10), strike_to_mills(100.10))
        self.assertEqual(strike_to_mills(330.0), 330000)


class NormalizeRightTests(unittest.TestCase):
    def test_variants(self):
        self.assertEqual(normalize_right("CALL"), "C")
        self.assertEqual(normalize_right("call"), "C")
        self.assertEqual(normalize_right("C"), "C")
        self.assertEqual(normalize_right("PUT"), "P")
        self.assertEqual(normalize_right("p"), "P")

    def test_invalid_raises(self):
        with self.assertRaises(ValueError):
            normalize_right("X")


class ContractIdTests(unittest.TestCase):
    def test_deterministic(self):
        exp = dt.date(2026, 7, 24)
        a = contract_id("SPY", exp, 750.0, "CALL")
        b = contract_id("SPY", exp, 750.0, "C")
        self.assertEqual(a, b)
        self.assertEqual(a, "SPY_20260724_000750000_C")

    def test_distinguishes_strike_and_right(self):
        exp = dt.date(2026, 7, 24)
        ids = {
            contract_id("SPY", exp, 750.0, "C"),
            contract_id("SPY", exp, 750.5, "C"),
            contract_id("SPY", exp, 750.0, "P"),
        }
        self.assertEqual(len(ids), 3)


class SourceHealthTests(unittest.TestCase):
    def test_shape(self):
        h = source_health(
            provider="thetadata", observed_at="2026-07-25T10:00:00-04:00", age_seconds=1.2345,
            contracts_expected=10, contracts_received=9,
            trade_classification_coverage=0.9123456, ambiguous_trade_fraction=0.05,
            oi_as_of_session="2026-07-24", quality="FRESH",
        )
        self.assertEqual(h["quality"], "FRESH")
        self.assertEqual(h["age_seconds"], round(1.2345, 3))  # banker's-rounding float repr, not a fixed literal
        self.assertEqual(h["trade_classification_coverage"], round(0.9123456, 4))

    def test_handles_none_coverage(self):
        h = source_health(
            provider="thetadata", observed_at="x", age_seconds=0,
            contracts_expected=0, contracts_received=0,
            trade_classification_coverage=None, ambiguous_trade_fraction=None,
            oi_as_of_session=None, quality="UNAVAILABLE",
        )
        self.assertIsNone(h["trade_classification_coverage"])


if __name__ == "__main__":
    unittest.main()
