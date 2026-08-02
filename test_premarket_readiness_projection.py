import unittest
from datetime import date

from premarket_forward_evaluator import add_market_days, build_status


class ProjectionTests(unittest.TestCase):
    def test_candidate_observation_date_starts_after_twenty_days(self):
        quality = []
        observations = []
        for day in range(20):
            session_date = f"2026-08-{day + 1:02d}"
            quality.append({
                "session_date": session_date,
                "stock_symbols": 100,
                "valid_quotes": 100,
                "missing_counts": {
                    "prior_close": 0,
                    "last_price": 0,
                    "premarket_volume": 0,
                    "premarket_vwap": 0,
                    "premarket_range_frac": 0,
                    "late_30m_return": 0,
                    "spread_bps": 0,
                    "market_gap": 0,
                    "sector_gap": 0,
                },
            })
            for index in range(2):
                observations.append({
                    "candidate": "orb_pm_gap003",
                    "session_date": session_date,
                    "symbol": f"S{index}",
                    "signal_time": f"{session_date}T09:45:00-04:00",
                    "net_r_6bp": 0.1,
                    "net_r_12bp": 0.05,
                })
        as_of = date(2026, 8, 10)
        status = build_status(quality, observations, as_of)
        candidate = status["candidates"]["orb_pm_gap003"]
        self.assertTrue(status["observation_projection_available"])
        self.assertEqual(candidate["observations_per_sealed_day"], 2.0)
        self.assertEqual(candidate["observations_remaining"], 210)
        self.assertEqual(candidate["estimated_observation_gate_date"], add_market_days(as_of, 105).isoformat())
        self.assertEqual(candidate["estimated_full_gate_date"], candidate["estimated_observation_gate_date"])


if __name__ == "__main__":
    unittest.main()
