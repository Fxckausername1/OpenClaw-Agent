import tempfile
import unittest
from pathlib import Path

import pandas as pd

from candidate_analysis import walk_forward
from candidate_replay import build_regimes, simulate_next_open, static_filter


def bar(timestamp, open_price, high, low, close):
    return {"time": timestamp, "open": open_price, "high": high, "low": low, "close": close}


class NextOpenTests(unittest.TestCase):
    def setUp(self):
        self.row = {
            "signal_index": 0,
            "side": "LONG",
            "stop": 99.0,
            "entry": 100.0,
            "risk_frac": 0.01,
            "signal_time": "2025-01-02T09:45:00",
        }

    def test_next_bar_open_is_entry_and_same_bar_stop_wins(self):
        bars = [
            bar("2025-01-02T09:45:00", 99.8, 100.2, 99.5, 100.1),
            bar("2025-01-02T09:50:00", 100.0, 101.2, 98.8, 100.5),
        ]
        outcome = simulate_next_open(bars, self.row, target_rr=1.0)
        self.assertEqual(outcome["fill_price"], 100.0)
        self.assertEqual(outcome["exit_reason"], "stop")
        self.assertEqual(outcome["outcome_r"], -1.0)

    def test_fixed_r_target(self):
        bars = [
            bar("2025-01-02T09:45:00", 99.8, 100.2, 99.5, 100.1),
            bar("2025-01-02T09:50:00", 100.0, 101.1, 99.4, 100.8),
        ]
        outcome = simulate_next_open(bars, self.row, target_rr=1.0)
        self.assertEqual(outcome["exit_reason"], "target")
        self.assertEqual(outcome["outcome_r"], 1.0)

    def test_aligned_regime_filter_is_side_aware(self):
        candidate = {"regime_filter": "aligned"}
        row = {**self.row, "side": "LONG"}
        self.assertTrue(static_filter(row, candidate, "bull"))
        self.assertFalse(static_filter(row, candidate, "bear"))


class RegimeTests(unittest.TestCase):
    def test_same_day_close_cannot_change_same_day_regime(self):
        dates = pd.date_range("2025-01-01", periods=25, freq="B")
        values = [100.0 + index for index in range(25)]
        with tempfile.TemporaryDirectory() as directory:
            first = Path(directory) / "first.parquet"
            second = Path(directory) / "second.parquet"
            pd.DataFrame({"Close": values}, index=dates).to_parquet(first)
            changed = values[:]
            changed[-1] = 1.0
            pd.DataFrame({"Close": changed}, index=dates).to_parquet(second)
            day = str(dates[-1].date())
            self.assertEqual(build_regimes(first)[day], build_regimes(second)[day])


class WalkForwardTests(unittest.TestCase):
    @staticmethod
    def rows(candidate, year, value):
        rows = []
        for index in range(240):
            day = index % 60 + 1
            rows.append({
                "candidate": candidate,
                "strategy": "MR",
                "date": f"{year}-01-{(day - 1) % 28 + 1:02d}",
                "fill_state": "filled_closed",
                "outcome_r": value,
                "net_r_6bp": value,
                "net_r_12bp": value,
                "net_r_20bp": value,
                "net_r_30bp": value,
            })
        # Ensure the synthetic sample meets the 60 unique-day coverage rule.
        for index, row in enumerate(rows):
            row["date"] = f"{year}-{index // 28 + 1:02d}-{index % 28 + 1:02d}"
        return rows

    def test_first_fold_selection_uses_training_only(self):
        rows = []
        rows += self.rows("candidate_a", "2024", 0.20)
        rows += self.rows("candidate_b", "2024", 0.10)
        rows += self.rows("candidate_a", "2025", -1.00)
        rows += self.rows("candidate_b", "2025", 1.00)
        rows += self.rows("candidate_a", "2026", -1.00)
        rows += self.rows("candidate_b", "2026", 1.00)
        folds, _ = walk_forward(rows, "MR", ["candidate_a", "candidate_b"])
        self.assertEqual(folds[0]["selected"], "candidate_a")
        self.assertEqual(folds[1]["selected"], "candidate_b")


if __name__ == "__main__":
    unittest.main()
