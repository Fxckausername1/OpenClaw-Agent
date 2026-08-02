import unittest

import pandas as pd

from thetadata_pipeline.aggregate import (
    accumulate_contract_stats, classify_wall_state, label_next_day_confirmation, minute_bars,
    wall_aggregates, wall_aggregates_from_stats,
)
from thetadata_pipeline.schemas import (
    CLASS_ASK, CLASS_BID, CLASS_MID, WALL_CONFIRMED_DISMANTLING, WALL_GHOST_CANDIDATE,
    WALL_INSUFFICIENT_DATA, WALL_REJECTED_GHOST, WALL_STABLE,
)


def _trade(contract_id, classification, size=1, price=1.0, ts="2026-07-24 10:00:00",
           underlying="SPY", expiration="2026-07-24", strike=750.0, right="C"):
    return {
        "contract_id": contract_id, "underlying": underlying, "expiration": expiration,
        "strike": strike, "right": right, "classification": classification,
        "size": size, "price": price, "trade_timestamp": pd.Timestamp(ts),
    }


class MinuteBarsTests(unittest.TestCase):
    def test_buckets_by_minute_and_side(self):
        rows = [
            _trade("C1", CLASS_ASK, size=5, price=1.0, ts="2026-07-24 10:00:10"),
            _trade("C1", CLASS_BID, size=2, price=1.0, ts="2026-07-24 10:00:40"),
            _trade("C1", CLASS_ASK, size=3, price=1.0, ts="2026-07-24 10:01:05"),
        ]
        out = minute_bars(pd.DataFrame(rows))
        self.assertEqual(len(out), 2)  # two distinct minute buckets
        first_minute = out[out["minute"] == pd.Timestamp("2026-07-24 10:00:00")].iloc[0]
        self.assertEqual(first_minute["ask_contracts"], 5)
        self.assertEqual(first_minute["bid_contracts"], 2)

    def test_empty_input(self):
        out = minute_bars(pd.DataFrame())
        self.assertTrue(out.empty)
        self.assertIn("contract_id", out.columns)


class WallAggregatesTests(unittest.TestCase):
    def test_v_oi_and_fractions(self):
        rows = [_trade("C1", CLASS_ASK, size=10) for _ in range(3)] + [_trade("C1", CLASS_BID, size=10)]
        df = pd.DataFrame(rows)
        out = wall_aggregates(df, prior_session_oi={"C1": 200.0})
        row = out.iloc[0]
        self.assertEqual(row["intraday_volume"], 40)
        self.assertAlmostEqual(row["v_oi"], 40 / 200.0)
        self.assertAlmostEqual(row["ask_fraction"], 30 / 40)
        self.assertAlmostEqual(row["bid_fraction"], 10 / 40)
        self.assertTrue(row["established_oi"])

    def test_fractions_are_volume_weighted_not_row_counted(self):
        # Regression test: a real bug (found via this exact non-uniform-size
        # scenario) computed ask/bid/mid as ROW counts via boolean .sum(),
        # not summed contract volume -- silently wrong whenever trade sizes
        # differ, which real market data always does. One ASK row of size
        # 100 must outweigh three BID rows of size 1 each, not the reverse.
        rows = [_trade("C1", CLASS_ASK, size=100), _trade("C1", CLASS_BID, size=1),
                _trade("C1", CLASS_BID, size=1), _trade("C1", CLASS_BID, size=1)]
        out = wall_aggregates(pd.DataFrame(rows), prior_session_oi={"C1": 200.0})
        row = out.iloc[0]
        self.assertEqual(row["intraday_volume"], 103)
        self.assertAlmostEqual(row["ask_fraction"], 100 / 103)
        self.assertAlmostEqual(row["bid_fraction"], 3 / 103)

    def test_missing_oi_is_not_established(self):
        df = pd.DataFrame([_trade("C1", CLASS_ASK, size=10)])
        out = wall_aggregates(df, prior_session_oi={})
        self.assertFalse(out.iloc[0]["established_oi"])
        self.assertIsNone(out.iloc[0]["v_oi"])

    def test_oi_floor(self):
        df = pd.DataFrame([_trade("C1", CLASS_ASK, size=10)])
        out = wall_aggregates(df, prior_session_oi={"C1": 50.0})  # below MIN_ESTABLISHED_OI=100
        self.assertFalse(out.iloc[0]["established_oi"])

    def test_empty(self):
        out = wall_aggregates(pd.DataFrame(), {})
        self.assertTrue(out.empty)


class AccumulateContractStatsTests(unittest.TestCase):
    def test_accumulates_across_multiple_batches(self):
        batch1 = pd.DataFrame([_trade("C1", CLASS_ASK, size=10), _trade("C1", CLASS_BID, size=3)])
        batch2 = pd.DataFrame([_trade("C1", CLASS_ASK, size=5), _trade("C1", CLASS_MID, size=2)])
        stats = accumulate_contract_stats({}, batch1)
        stats = accumulate_contract_stats(stats, batch2)
        self.assertEqual(stats["C1"]["ask"], 15)
        self.assertEqual(stats["C1"]["bid"], 3)
        self.assertEqual(stats["C1"]["mid"], 2)
        self.assertEqual(stats["C1"]["total"], 20)

    def test_empty_batch_is_a_noop(self):
        stats = accumulate_contract_stats({"C1": {"ask": 1, "bid": 0, "mid": 0, "total": 1}}, pd.DataFrame())
        self.assertEqual(stats["C1"]["ask"], 1)

    def test_matches_full_batch_wall_aggregates(self):
        # Same total data, fed in two increments vs. all at once -- v_oi math
        # must be identical either way (this is the whole point of the fix).
        all_rows = [_trade("C1", CLASS_ASK, size=10), _trade("C1", CLASS_BID, size=3),
                    _trade("C1", CLASS_ASK, size=5), _trade("C1", CLASS_MID, size=2)]
        full_df = pd.DataFrame(all_rows)
        oi = {"C1": 200.0}
        one_shot = wall_aggregates(full_df, oi)

        stats = accumulate_contract_stats({}, pd.DataFrame(all_rows[:2]))
        stats = accumulate_contract_stats(stats, pd.DataFrame(all_rows[2:]))
        incremental = wall_aggregates_from_stats(stats, oi)

        self.assertAlmostEqual(one_shot.iloc[0]["v_oi"], incremental.iloc[0]["v_oi"])
        self.assertAlmostEqual(one_shot.iloc[0]["bid_fraction"], incremental.iloc[0]["bid_fraction"])
        self.assertEqual(one_shot.iloc[0]["intraday_volume"], incremental.iloc[0]["intraday_volume"])


class WallStateTests(unittest.TestCase):
    def test_ghost_candidate(self):
        row = pd.Series({"established_oi": True, "v_oi": 1.2, "bid_fraction": 0.8, "ask_fraction": 0.2})
        self.assertEqual(classify_wall_state(row), WALL_GHOST_CANDIDATE)

    def test_insufficient_data_without_established_oi(self):
        row = pd.Series({"established_oi": False, "v_oi": 1.2, "bid_fraction": 0.8, "ask_fraction": 0.2})
        self.assertEqual(classify_wall_state(row), WALL_INSUFFICIENT_DATA)

    def test_stable(self):
        row = pd.Series({"established_oi": True, "v_oi": 0.2, "bid_fraction": 0.5, "ask_fraction": 0.5})
        self.assertEqual(classify_wall_state(row), WALL_STABLE)


class LabelNextDayConfirmationTests(unittest.TestCase):
    def _ghost_row(self, contract_id="C1", oi=200.0):
        return {"contract_id": contract_id, "wall_state": WALL_GHOST_CANDIDATE, "oi": oi}

    def test_confirmed_when_oi_drops_materially(self):
        df = pd.DataFrame([self._ghost_row(oi=200.0)])
        out = label_next_day_confirmation(df, next_day_oi={"C1": 100.0})  # 50% drop
        self.assertEqual(out.iloc[0]["next_day_confirmation_status"], WALL_CONFIRMED_DISMANTLING)

    def test_rejected_when_oi_stable_or_higher(self):
        df = pd.DataFrame([self._ghost_row(oi=200.0)])
        out = label_next_day_confirmation(df, next_day_oi={"C1": 210.0})
        self.assertEqual(out.iloc[0]["next_day_confirmation_status"], WALL_REJECTED_GHOST)

    def test_small_drop_below_noise_floor_is_rejected_not_confirmed(self):
        # A 5% drop is ordinary day-to-day noise, not real closing activity --
        # must not be confirmed just because it went down at all.
        df = pd.DataFrame([self._ghost_row(oi=200.0)])
        out = label_next_day_confirmation(df, next_day_oi={"C1": 190.0})
        self.assertEqual(out.iloc[0]["next_day_confirmation_status"], WALL_REJECTED_GHOST)

    def test_missing_next_day_oi_is_unresolved_not_guessed(self):
        df = pd.DataFrame([self._ghost_row(oi=200.0)])
        out = label_next_day_confirmation(df, next_day_oi={})
        self.assertIsNone(out.iloc[0]["next_day_confirmation_status"])

    def test_non_ghost_rows_are_not_applicable(self):
        df = pd.DataFrame([{"contract_id": "C1", "wall_state": WALL_STABLE, "oi": 200.0}])
        out = label_next_day_confirmation(df, next_day_oi={"C1": 1.0})  # even a huge drop shouldn't matter
        self.assertIsNone(out.iloc[0]["next_day_confirmation_status"])

    def test_empty_input(self):
        out = label_next_day_confirmation(pd.DataFrame(), {})
        self.assertTrue(out.empty)

    def test_multiple_rows_independent(self):
        df = pd.DataFrame([
            self._ghost_row(contract_id="C1", oi=200.0),
            self._ghost_row(contract_id="C2", oi=100.0),
            {"contract_id": "C3", "wall_state": WALL_STABLE, "oi": 300.0},
        ])
        out = label_next_day_confirmation(df, next_day_oi={"C1": 50.0, "C2": 100.0})
        statuses = dict(zip(out["contract_id"], out["next_day_confirmation_status"]))
        self.assertEqual(statuses["C1"], WALL_CONFIRMED_DISMANTLING)
        self.assertEqual(statuses["C2"], WALL_REJECTED_GHOST)
        self.assertIsNone(statuses["C3"])


if __name__ == "__main__":
    unittest.main()
