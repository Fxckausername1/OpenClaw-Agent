import unittest
from datetime import date

from premarket_forward_evaluator import (
    CANDIDATES,
    add_market_days,
    aggregate_regular_five_minute,
    build_status,
    candidate_passes,
    is_market_day,
    raw_capture_features,
)


def bar(symbol, timestamp, open_price, high, low, close, volume=100, vwap=None):
    return {
        "record_type": "bar",
        "symbol": symbol,
        "bar": {
            "t": timestamp,
            "o": open_price,
            "h": high,
            "l": low,
            "c": close,
            "v": volume,
            "vw": close if vwap is None else vwap,
        },
    }


class FeatureTests(unittest.TestCase):
    def test_capture_features_use_only_capture_records(self):
        records = [
            bar("AAPL", "2026-07-13T12:44:00Z", 100, 101, 99, 100.5, 200),
            bar("AAPL", "2026-07-13T13:00:00Z", 100.5, 102, 100, 101.5, 300),
            {
                "record_type": "snapshot",
                "symbol": "AAPL",
                "snapshot": {
                    "prevDailyBar": {"c": 99},
                    "latestQuote": {"bp": 101.4, "ap": 101.6},
                },
            },
        ]
        manifest = {"collected_at": "2026-07-13T13:15:00Z", "symbols": ["AAPL"]}
        feature = raw_capture_features(records, manifest)["AAPL"]
        self.assertAlmostEqual(feature["overnight_gap"], 101.5 / 99 - 1)
        self.assertEqual(feature["premarket_volume"], 500)
        self.assertIsNotNone(feature["spread_bps"])

    def test_regular_session_is_aggregated_to_closed_five_minute_bars(self):
        records = [
            bar("AAPL", "2026-07-13T13:30:00Z", 100, 101, 99, 100.5, 10),
            bar("AAPL", "2026-07-13T13:31:00Z", 100.5, 102, 100, 101.5, 20),
            bar("AAPL", "2026-07-13T13:35:00Z", 101.5, 103, 101, 102, 30),
            bar("AAPL", "2026-07-13T12:00:00Z", 90, 200, 1, 150, 999),
        ]
        bars = aggregate_regular_five_minute(records)["AAPL"]
        self.assertEqual(len(bars), 2)
        self.assertEqual(bars[0]["open"], 100)
        self.assertEqual(bars[0]["high"], 102)
        self.assertEqual(bars[0]["close"], 101.5)
        self.assertEqual(bars[0]["volume"], 30)
        self.assertNotIn("vwap", bars[0])


class CandidateTests(unittest.TestCase):
    def test_gap_filter_is_side_symmetric(self):
        candidate = next(item for item in CANDIDATES if item["name"] == "orb_pm_gap003")
        self.assertTrue(candidate_passes(candidate, {"overnight_gap": 0.004}, "LONG"))
        self.assertTrue(candidate_passes(candidate, {"overnight_gap": -0.004}, "SHORT"))
        self.assertFalse(candidate_passes(candidate, {"overnight_gap": -0.004}, "LONG"))

    def test_combined_candidate_requires_every_fixed_feature(self):
        candidate = next(item for item in CANDIDATES if item["name"] == "orb_pm_combined")
        feature = {
            "overnight_gap": 0.004,
            "premarket_relative_volume": 1.6,
            "vwap_position": 0.001,
            "late_30m_return": 0.001,
            "spread_bps": 10,
            "market_gap": 0.002,
            "sector_gap": 0.001,
        }
        self.assertTrue(candidate_passes(candidate, feature, "LONG"))
        self.assertFalse(candidate_passes(candidate, {**feature, "spread_bps": 20}, "LONG"))


class ReadinessTests(unittest.TestCase):
    def test_market_calendar_skips_weekends_and_standard_holidays(self):
        self.assertFalse(is_market_day(date(2026, 7, 4)))
        self.assertFalse(is_market_day(date(2026, 7, 3)))
        self.assertEqual(add_market_days(date(2026, 7, 2), 1), date(2026, 7, 6))

    def test_status_reports_days_and_observations_remaining(self):
        quality = [
            {
                "session_date": f"2026-07-{day:02d}",
                "stock_symbols": 100,
                "valid_quotes": 95,
                "missing_counts": {
                    "prior_close": 1,
                    "last_price": 1,
                    "premarket_volume": 1,
                    "premarket_vwap": 1,
                    "premarket_range_frac": 1,
                    "late_30m_return": 1,
                    "spread_bps": 5,
                    "market_gap": 0,
                    "sector_gap": 2,
                },
            }
            for day in range(1, 11)
        ]
        status = build_status(quality, [], date(2026, 7, 13))
        self.assertEqual(status["sealed_scored_market_days"], 10)
        self.assertEqual(status["market_days_remaining"], 110)
        self.assertEqual(status["stage"], "COLLECTING_FOR_20_DAY_QUALITY_REVIEW")
        self.assertEqual(status["candidates"]["orb_pm_gap003"]["observations_remaining"], 250)


if __name__ == "__main__":
    unittest.main()
