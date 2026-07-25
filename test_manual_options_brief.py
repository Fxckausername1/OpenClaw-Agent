import unittest
from datetime import date, datetime, timedelta, timezone

import manual_options_brief as mob


class ManualOptionsBriefTests(unittest.TestCase):
    def test_sweep_requires_reclaim_and_next_bar_hold(self):
        day = date(2026, 7, 27)
        start = datetime(2026, 7, 27, 13, 45, tzinfo=timezone.utc)
        rows = [
            (start, {"l": 99.97, "h": 100.10, "c": 100.05}),
            (start + timedelta(minutes=1), {"l": 100.01, "h": 100.12, "c": 100.08}),
        ]
        levels = [{
            "name": "opening-range low",
            "level": 100.0,
            "active_after": start.astimezone(mob.ET),
        }]
        events = mob.detect_liquidity_sweeps(
            [(stamp.astimezone(mob.ET), row) for stamp, row in rows],
            levels,
        )
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["direction"], "bullish")
        self.assertEqual(events[0]["level_name"], "opening-range low")

    def test_low_rejection_is_not_mislabeled_bearish_sweep(self):
        day = date(2026, 7, 27)
        start = datetime(2026, 7, 27, 13, 45, tzinfo=timezone.utc)
        rows = [
            (start, {"l": 99.80, "h": 100.05, "c": 99.95}),
            (start + timedelta(minutes=1), {"l": 99.75, "h": 99.98, "c": 99.90}),
        ]
        levels = [{
            "name": "premarket low",
            "level": 100.0,
            "active_after": start.astimezone(mob.ET),
        }]
        events = mob.detect_liquidity_sweeps(
            [(stamp.astimezone(mob.ET), row) for stamp, row in rows],
            levels,
        )
        self.assertEqual(events, [])

    def test_option_candidate_budget_target_and_friction(self):
        observed = datetime(2026, 7, 27, 13, 50, tzinfo=timezone.utc)
        candidate = mob.option_candidate(
            "SPY260727C00602000",
            {
                "latestQuote": {
                    "bp": 0.23, "ap": 0.24, "bs": 200, "as": 180,
                    "t": observed.isoformat(),
                },
                "impliedVolatility": 0.22,
                "greeks": {"delta": 0.22, "gamma": 0.04, "theta": -0.03},
            },
            {"open_interest": 2500},
            600.0,
            observed,
        )
        self.assertIsNotNone(candidate)
        self.assertEqual(candidate["contracts"], 16)
        self.assertEqual(candidate["estimated_debit"], 384.0)
        self.assertEqual(candidate["target_premium"], 0.30)
        self.assertEqual(candidate["gross_target_profit"], 96.0)
        self.assertEqual(candidate["estimated_full_spread_cost"], 16.0)
        self.assertEqual(candidate["screen"], "SCREEN PASS - VERIFY LIVE")

    def test_wide_contract_is_rejected(self):
        observed = datetime(2026, 7, 27, 13, 50, tzinfo=timezone.utc)
        candidate = mob.option_candidate(
            "QQQ260727P00533000",
            {
                "latestQuote": {"bp": 0.20, "ap": 0.27, "t": observed.isoformat()},
                "impliedVolatility": 0.25,
                "greeks": {"delta": -0.20, "gamma": 0.05, "theta": -0.04},
            },
            {"open_interest": 800},
            535.0,
            observed,
        )
        self.assertEqual(candidate["screen"], "REJECT")
        self.assertTrue(any("spread" in reason for reason in candidate["reject_reasons"]))


if __name__ == "__main__":
    unittest.main()
