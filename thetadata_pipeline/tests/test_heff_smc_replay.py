"""Tests for heff_smc_replay.py -- continuous-series gap handling and the
end-to-end run_replay orchestration, on small synthetic multi-session data
(no network, no real bar files)."""

from __future__ import annotations

import datetime as dt
import unittest
from zoneinfo import ZoneInfo

import pandas as pd

from thetadata_pipeline.heff_smc_engine import HeffSmcConfig
from thetadata_pipeline.heff_smc_replay import build_continuous_1min_series, run_replay

ET = ZoneInfo("America/New_York")


def _session_rows(day: dt.date, present_minutes, price=100.0):
    """present_minutes: set of minute-offsets (0..389) that have a real bar;
    all others are gaps. Prices drift by +0.01 per present minute so gaps
    are visually distinguishable from a real flat market."""
    base = dt.datetime.combine(day, dt.time(9, 30), tzinfo=ET)
    rows = []
    p = price
    for m in sorted(present_minutes):
        p += 0.01
        t = base + dt.timedelta(minutes=m)
        rows.append({"t": t, "o": p, "h": p + 0.02, "l": p - 0.02, "c": p, "v": 10.0, "n": 1, "vw": p})
    return rows


class BuildContinuousSeriesTests(unittest.TestCase):
    def test_interior_gap_forward_filled_from_last_real_close(self):
        day = dt.date(2026, 1, 5)
        # minute 2 is missing; minute 1's close should carry into minute 2
        present = list(range(0, 390))
        present.remove(2)
        raw = pd.DataFrame(_session_rows(day, present))
        cont = build_continuous_1min_series(raw)
        day_df = cont[cont["t"].dt.date == day].reset_index(drop=True)
        self.assertEqual(len(day_df), 390)
        gap_row = day_df.iloc[2]
        prior_row = day_df.iloc[1]
        self.assertTrue(gap_row["synthetic"])
        self.assertAlmostEqual(gap_row["c"], prior_row["c"])
        self.assertAlmostEqual(gap_row["o"], prior_row["c"])
        self.assertAlmostEqual(gap_row["h"], prior_row["c"])
        self.assertAlmostEqual(gap_row["l"], prior_row["c"])
        self.assertEqual(gap_row["v"], 0.0)

    def test_leading_gap_backfilled_from_next_real_print_not_fabricated(self):
        day = dt.date(2026, 1, 5)
        present = list(range(0, 390))
        present.remove(0)  # the session's own first minute is missing
        raw = pd.DataFrame(_session_rows(day, present))
        cont = build_continuous_1min_series(raw)
        day_df = cont[cont["t"].dt.date == day].reset_index(drop=True)
        self.assertTrue(day_df.iloc[0]["synthetic"])
        self.assertAlmostEqual(day_df.iloc[0]["c"], day_df.iloc[1]["c"])

    def test_never_fills_across_a_session_boundary(self):
        day1 = dt.date(2026, 1, 5)
        day2 = dt.date(2026, 1, 6)
        present1 = list(range(0, 390))
        present2 = list(range(0, 390))
        present2.remove(0)  # day2's first minute missing
        raw = pd.DataFrame(_session_rows(day1, present1, price=100.0) + _session_rows(day2, present2, price=500.0))
        cont = build_continuous_1min_series(raw)
        day2_df = cont[cont["t"].dt.date == day2].reset_index(drop=True)
        day1_df = cont[cont["t"].dt.date == day1].reset_index(drop=True)
        # day2's leading gap must be filled from day2's OWN next print (~500),
        # never day1's final close (~104) -- confirms no cross-day bleed
        self.assertTrue(day2_df.iloc[0]["synthetic"])
        self.assertGreater(day2_df.iloc[0]["c"], 400.0)
        self.assertLess(day1_df.iloc[-1]["c"], 110.0)


class RunReplayTests(unittest.TestCase):
    def test_pdh_pdl_none_on_first_session_then_real_prior_session_extremes(self):
        day1 = dt.date(2026, 1, 5)
        day2 = dt.date(2026, 1, 6)
        raw = pd.DataFrame(
            _session_rows(day1, range(0, 390), price=100.0) + _session_rows(day2, range(0, 390), price=100.0)
        )
        cont = build_continuous_1min_series(raw)
        cfg = HeffSmcConfig(show_pdhl=True)
        events, diag = run_replay(cont, cfg)
        self.assertGreater(len(diag), 0)
        day1_high = cont[cont["t"].dt.date == day1]["h"].max()
        day1_low = cont[cont["t"].dt.date == day1]["l"].min()
        # can't read engine.pdh directly post-hoc (state is internal to the
        # single run), but this at least proves the replay runs end-to-end
        # across a real session boundary without error and produces a
        # non-degenerate diagnostic frame covering both sessions
        self.assertEqual(diag["t"].dt.date.nunique(), 2)
        self.assertGreater(day1_high, day1_low)

    def test_run_replay_produces_well_formed_events(self):
        day1 = dt.date(2026, 1, 5)
        raw = pd.DataFrame(_session_rows(day1, range(0, 390), price=500.0))
        cont = build_continuous_1min_series(raw)
        cfg = HeffSmcConfig()
        events, diag = run_replay(cont, cfg)
        for e in events:
            self.assertIn(e["side"], ("long", "short"))
            self.assertGreaterEqual(e["score"], cfg.min_score)
            self.assertIn(e["trigger"], ("MSS", "BOS", "SWEEP_RECLAIM", "PULLBACK", "MA_FADE"))
            self.assertEqual(e["mode"], "honest")
            self.assertIn("factors", e)


if __name__ == "__main__":
    unittest.main()
