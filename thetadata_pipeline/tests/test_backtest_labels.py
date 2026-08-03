import datetime as dt
import unittest

import pandas as pd

from thetadata_pipeline.backtest_labels import BREAK, HOLD, UNAVAILABLE, label_wall_break_hold

ET_NAIVE = lambda h, m: dt.datetime(2026, 7, 24, h, m)


def _bars(rows):
    return pd.DataFrame([
        {"timestamp": ts, "high": h, "low": l} for ts, h, l in rows
    ])


class LabelWallBreakHoldTests(unittest.TestCase):
    def test_resistance_wall_holds_when_high_never_reaches_strike(self):
        observed = ET_NAIVE(10, 0)
        bars = _bars([
            (ET_NAIVE(10, 1), 744.0, 743.0),
            (ET_NAIVE(10, 5), 744.5, 743.5),
            (ET_NAIVE(10, 15), 744.8, 743.8),
            (ET_NAIVE(10, 30), 744.9, 743.9),
        ])
        out = label_wall_break_hold(strike=745.0, spot_at_observation=743.0, observed_at=observed, minute_bars=bars)
        self.assertEqual(out, {5: HOLD, 15: HOLD, 30: HOLD})

    def test_resistance_wall_breaks_once_high_crosses_strike(self):
        observed = ET_NAIVE(10, 0)
        bars = _bars([
            (ET_NAIVE(10, 3), 744.0, 743.0),   # within 5min: still under
            (ET_NAIVE(10, 12), 745.5, 744.0),  # within 15min: crosses 745 strike
            (ET_NAIVE(10, 25), 746.0, 745.0),
        ])
        out = label_wall_break_hold(strike=745.0, spot_at_observation=743.0, observed_at=observed, minute_bars=bars)
        self.assertEqual(out[5], HOLD)
        self.assertEqual(out[15], BREAK)
        self.assertEqual(out[30], BREAK)  # a break by 15min stays a break by 30min (window is cumulative)

    def test_support_wall_breaks_on_low_crossing_below_strike(self):
        # strike below spot -> support role -> break means price falls THROUGH it
        observed = ET_NAIVE(10, 0)
        bars = _bars([
            (ET_NAIVE(10, 2), 743.0, 742.0),
            (ET_NAIVE(10, 8), 742.5, 739.5),  # low crosses below the 740 strike, at minute 8
        ])
        out = label_wall_break_hold(strike=740.0, spot_at_observation=743.0, observed_at=observed, minute_bars=bars)
        self.assertEqual(out[5], HOLD)  # the crossing bar is at minute 8, outside the 5min window
        self.assertEqual(out[15], BREAK)

    def test_strike_equal_to_spot_is_unavailable_not_guessed(self):
        observed = ET_NAIVE(10, 0)
        bars = _bars([(ET_NAIVE(10, 5), 744.0, 742.0)])
        out = label_wall_break_hold(strike=743.0, spot_at_observation=743.0, observed_at=observed, minute_bars=bars)
        self.assertEqual(out, {5: UNAVAILABLE, 15: UNAVAILABLE, 30: UNAVAILABLE})

    def test_missing_future_bars_is_unavailable_per_horizon_independently(self):
        # Wall observed at 15:50 -- only a few bars exist before the 16:00 close,
        # so 5min might resolve but 15/30min genuinely have no data yet.
        observed = ET_NAIVE(15, 50)
        bars = _bars([
            (ET_NAIVE(15, 52), 744.0, 743.0),
            (ET_NAIVE(15, 55), 744.2, 743.2),
            (ET_NAIVE(15, 59), 744.3, 743.3),
        ])
        out = label_wall_break_hold(strike=745.0, spot_at_observation=743.0, observed_at=observed, minute_bars=bars)
        self.assertEqual(out[5], HOLD)  # bars exist through +5min (15:55) and never crossed
        self.assertEqual(out[15], HOLD)  # last bar at 15:59 is within the +15min window, still holds
        self.assertEqual(out[30], HOLD)  # window still just contains the same bars, still holds

    def test_no_bars_at_all_is_unavailable(self):
        observed = ET_NAIVE(10, 0)
        out = label_wall_break_hold(strike=745.0, spot_at_observation=743.0, observed_at=observed, minute_bars=pd.DataFrame())
        self.assertEqual(out, {5: UNAVAILABLE, 15: UNAVAILABLE, 30: UNAVAILABLE})

    def test_bars_before_observation_are_ignored(self):
        # A bar timestamped BEFORE observed_at that would have "broken" the
        # strike must not count -- only bars strictly after observation matter.
        observed = ET_NAIVE(10, 0)
        bars = _bars([
            (ET_NAIVE(9, 55), 900.0, 800.0),  # before observation, wildly crosses -- must be ignored
            (ET_NAIVE(10, 3), 744.0, 743.0),
        ])
        out = label_wall_break_hold(strike=745.0, spot_at_observation=743.0, observed_at=observed, minute_bars=bars)
        self.assertEqual(out[5], HOLD)


if __name__ == "__main__":
    unittest.main()
