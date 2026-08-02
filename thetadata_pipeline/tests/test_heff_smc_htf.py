"""Tests for heff_smc_htf.py -- Module 7 (HTF bias) port."""

from __future__ import annotations

import datetime as dt
import unittest
from zoneinfo import ZoneInfo

import pandas as pd

from thetadata_pipeline.heff_smc_htf import (
    HtfStructDirTracker, align_htf_dir_to_1min, compute_htf_dir_series, resample_htf_bars,
)

ET = ZoneInfo("America/New_York")


def _one_min_frame(day: dt.date, closes, start=dt.time(9, 30)):
    base = dt.datetime.combine(day, start, tzinfo=ET)
    rows = []
    for i, c in enumerate(closes):
        t = base + dt.timedelta(minutes=i)
        rows.append({"t": t, "o": c, "h": c + 0.05, "l": c - 0.05, "c": c, "v": 100.0})
    return pd.DataFrame(rows)


class ResampleHtfBarsTests(unittest.TestCase):
    def test_5min_bins_align_to_clock_and_close_time_is_the_label(self):
        day = dt.date(2026, 1, 5)
        df = _one_min_frame(day, [100 + i * 0.1 for i in range(10)])  # 09:30-09:39
        htf = resample_htf_bars(df, 5)
        # two clean 5-min bins: [09:30,09:35) closes at 09:35, [09:35,09:40) closes at 09:40
        self.assertEqual(len(htf), 2)
        self.assertEqual(htf.iloc[0]["t"].strftime("%H:%M"), "09:35")
        self.assertEqual(htf.iloc[1]["t"].strftime("%H:%M"), "09:40")

    def test_overnight_gap_produces_no_empty_bins(self):
        day1 = dt.date(2026, 1, 5)
        day2 = dt.date(2026, 1, 6)
        df = pd.concat([
            _one_min_frame(day1, [100.0] * 5),
            _one_min_frame(day2, [101.0] * 5),
        ], ignore_index=True)
        htf = resample_htf_bars(df, 5)
        # only real trading bins, no NaN rows spanning the overnight gap
        self.assertTrue(htf[["o", "h", "l", "c"]].notna().all().all())


class HtfStructDirTrackerTests(unittest.TestCase):
    def test_dir_flips_on_close_through_confirmed_pivot(self):
        t = HtfStructDirTracker(piv_len=1)
        # build a pivot high at bar1 (values: bar0=10, bar1=12, bar2=9) then break it
        self.assertEqual(t.update(high=10, low=9, close=9.5), 0)
        self.assertEqual(t.update(high=12, low=9, close=9.6), 0)   # pivot candidate, not confirmed yet
        self.assertEqual(t.update(high=9, low=8, close=9.0), 0)    # confirms ph=12 (piv_len=1), no break yet
        d = t.update(high=13, low=9.5, close=12.5)                 # close(12.5) > active_high(12) -> dir flips to +1
        self.assertEqual(d, 1)


class AlignHtfDirTests(unittest.TestCase):
    def test_bar_inside_forming_bin_gets_prior_completed_bars_dir_not_its_own(self):
        day = dt.date(2026, 1, 5)
        # 10 minutes: enough for two 5-min HTF bars. Force the first HTF bar's
        # dir to become +1 via its own bar-by-bar evolution, and confirm that
        # every 1-min bar INSIDE the second (still-forming) bin reads the
        # first bin's dir, never something derived from its own in-progress data.
        closes = [100, 101, 99, 100, 100, 105, 105, 105, 105, 105]
        df = _one_min_frame(day, closes)
        htf = compute_htf_dir_series(df, minutes=5, piv_len=1)
        aligned = align_htf_dir_to_1min(df, htf)
        first_bin_dir = htf.iloc[0]["dir"]
        # every bar in the second bin (indices 5-9) must read first_bin_dir,
        # not a value that could only be known once the second bin itself closes
        for i in range(5, 10):
            self.assertEqual(aligned.iloc[i], first_bin_dir)

    def test_bars_before_any_completed_htf_bar_default_to_zero(self):
        day = dt.date(2026, 1, 5)
        df = _one_min_frame(day, [100.0] * 3)  # inside the very first, still-forming bin
        htf = compute_htf_dir_series(df, minutes=5, piv_len=1)
        aligned = align_htf_dir_to_1min(df, htf)
        self.assertTrue((aligned == 0).all())


if __name__ == "__main__":
    unittest.main()
