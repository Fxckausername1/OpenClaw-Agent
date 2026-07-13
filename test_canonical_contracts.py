#!/usr/bin/env python3
import unittest

from canonical_mr_contract import detect_mr
from canonical_strategy_contracts import detect_orb, simulate_boundary_limit


def bar(ts, o, h, l, c, volume=100, **extra):
    return {"time": ts, "open": o, "high": h, "low": l, "close": c, "volume": volume, **extra}


class OrbContractTests(unittest.TestCase):
    def test_wick_touch_does_not_trigger_but_close_break_does(self):
        bars = [
            bar("2026-01-05T09:30:00", 100, 101, 99.5, 100),
            bar("2026-01-05T09:35:00", 100, 100.5, 99, 100),
            bar("2026-01-05T09:40:00", 100, 100.4, 99.4, 100),
            bar("2026-01-05T09:45:00", 100, 102, 99.8, 100.5),  # wick only
            bar("2026-01-05T09:50:00", 100.5, 101.5, 100.4, 101.2),  # close-confirmed
            bar("2026-01-05T09:55:00", 101.3, 101.8, 101.1, 101.6),
        ]
        intents = detect_orb(
            bars,
            {"max_range_frac": 0.03, "use_vwap": False, "use_vol": False, "use_sector_gate": False},
        )
        self.assertEqual(len(intents), 1)
        self.assertEqual(intents[0]["signal_index"], 4)
        self.assertEqual(intents[0]["side"], "LONG")

    def test_boundary_limit_can_miss_continuation(self):
        bars = [
            bar("2026-01-05T09:50:00", 101, 102, 101, 101.8),
            bar("2026-01-05T09:55:00", 101.8, 103, 101.5, 102.8),
        ]
        result = simulate_boundary_limit(bars, 0, "LONG", 101.0, 99.0, None)
        self.assertEqual(result["fill_state"], "never_filled")
        self.assertEqual(result["outcome_r"], 0.0)


class MrContractTests(unittest.TestCase):
    def test_five_minute_watch_to_trigger(self):
        bars = [
            bar("2026-01-05T09:30:00", 97.1, 97.2, 96.8, 97.0, rsi=20, vwap=100, vwap_dev=-0.03, sma20=100, std20=1, z=-3),
            bar("2026-01-05T09:35:00", 97.5, 98.1, 97.0, 98.0, rsi=25, vwap=100, vwap_dev=-0.02, sma20=100, std20=1, z=-2),
            bar("2026-01-05T09:40:00", 98.0, 98.2, 97.2, 99.0, rsi=35, vwap=100, vwap_dev=-0.01, sma20=100, std20=1, z=-1),
        ]
        intents = detect_mr(bars, {"min_rr": 1.0})
        self.assertEqual(len(intents), 1)
        self.assertEqual(intents[0]["signal_index"], 2)
        self.assertEqual(intents[0]["side"], "LONG")

    def test_same_bar_ambiguity_is_stop_first(self):
        bars = [
            bar("2026-01-05T09:40:00", 100, 101, 99, 100),
            bar("2026-01-05T09:45:00", 100, 103, 97, 101),
        ]
        result = simulate_boundary_limit(bars, 0, "LONG", 100, 98, 102)
        self.assertEqual(result["fill_state"], "filled_closed")
        self.assertEqual(result["exit_reason"], "stop")
        self.assertEqual(result["outcome_r"], -1.0)


if __name__ == "__main__":
    unittest.main()
