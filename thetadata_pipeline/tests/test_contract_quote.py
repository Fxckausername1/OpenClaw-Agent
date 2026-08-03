import datetime as dt
import unittest
from unittest.mock import patch

import pandas as pd

from thetadata_pipeline.contract_quote import (
    ContractScreenUnavailable, _candidate_rank_key, _evaluate_candidate,
    _runway_check, screen_candidates,
)


def _book_row(strike=743.0, right="C", bid=0.76, ask=0.78, bid_size=279, ask_size=120,
              quote_age_seconds=1.5, delta=0.35, implied_vol=0.15):
    df = pd.DataFrame([{
        "strike": strike, "right": right, "bid": bid, "ask": ask,
        "bid_size": bid_size, "ask_size": ask_size,
        "quote_age_seconds": quote_age_seconds, "delta": delta, "implied_vol": implied_vol,
    }])
    return next(df.itertuples())


class EvaluateCandidateTests(unittest.TestCase):
    def test_pass_case(self):
        row = _book_row()
        out = _evaluate_candidate(row, spot=743.5, right="C", wall_records=[])
        self.assertEqual(out["gate"], "PASS")
        self.assertEqual(out["reasons"], [])
        self.assertEqual(out["bid_size"], 279.0)

    def test_stale_quote_only_is_wait_not_fail(self):
        # Freshness is the only violated gate -- should degrade to WAIT
        # (might resolve on the next refresh), not FAIL outright.
        row = _book_row(quote_age_seconds=45.0)
        out = _evaluate_candidate(row, spot=743.5, right="C", wall_records=[])
        self.assertEqual(out["gate"], "WAIT")
        self.assertEqual(len(out["reasons"]), 1)
        self.assertIn("quote age", out["reasons"][0])

    def test_wide_spread_is_fail(self):
        row = _book_row(bid=0.60, ask=0.90)  # $0.30 spread, way over the $0.02 ceiling
        out = _evaluate_candidate(row, spot=743.5, right="C", wall_records=[])
        self.assertEqual(out["gate"], "FAIL")
        self.assertTrue(any("spread" in r for r in out["reasons"]))

    def test_low_delta_is_fail(self):
        row = _book_row(delta=0.04)
        out = _evaluate_candidate(row, spot=743.5, right="C", wall_records=[])
        self.assertEqual(out["gate"], "FAIL")
        self.assertTrue(any("delta" in r for r in out["reasons"]))

    def test_missing_delta_is_fail(self):
        row = _book_row(delta=None)
        out = _evaluate_candidate(row, spot=743.5, right="C", wall_records=[])
        self.assertEqual(out["gate"], "FAIL")
        self.assertTrue(any("delta unavailable" in r for r in out["reasons"]))

    def test_no_size_on_either_side_is_fail(self):
        # Non-uniform sizes on the two sides of the book -- one deep side
        # must not mask a genuinely empty other side (the wall_aggregates
        # row-count-vs-volume mistake was exactly this kind of asymmetry).
        row = _book_row(bid_size=500, ask_size=0)
        out = _evaluate_candidate(row, spot=743.5, right="C", wall_records=[])
        self.assertEqual(out["gate"], "FAIL")
        self.assertTrue(any("displayed size" in r for r in out["reasons"]))

    def test_invalid_quote_short_circuits(self):
        row = _book_row(bid=0.0, ask=0.78)
        out = _evaluate_candidate(row, spot=743.5, right="C", wall_records=[])
        self.assertEqual(out["gate"], "FAIL")
        self.assertEqual(out["reasons"], ["no valid two-sided quote"])

    def test_crossed_quote_short_circuits(self):
        row = _book_row(bid=0.80, ask=0.78)
        out = _evaluate_candidate(row, spot=743.5, right="C", wall_records=[])
        self.assertEqual(out["gate"], "FAIL")
        self.assertEqual(out["reasons"], ["no valid two-sided quote"])

    def test_high_friction_is_fail_even_when_spread_gates_pass(self):
        # spread=$0.02 (right at the $ ceiling, passes) and spread_pct=10%
        # (right at the midpoint ceiling, passes) but the 25%-gross-target
        # friction ratio still fails on its own -- these are independent
        # gates, not one gate implying the other.
        row = _book_row(bid=0.19, ask=0.21)
        out = _evaluate_candidate(row, spot=743.5, right="C", wall_records=[])
        self.assertEqual(out["gate"], "FAIL")
        self.assertTrue(any("friction" in r for r in out["reasons"]))
        self.assertEqual(len(out["reasons"]), 1)

    def test_premium_outside_preferred_band_is_only_informational(self):
        # Section 8: premium band is "preference only", never a hard gate.
        row = _book_row(bid=0.02, ask=0.021, delta=0.5)  # tiny premium, everything else clean
        out = _evaluate_candidate(row, spot=743.5, right="C", wall_records=[])
        self.assertFalse(out["premium_in_preferred_band"])
        self.assertEqual(out["gate"], "PASS")  # out-of-band premium must not block the gate


class RunwayCheckTests(unittest.TestCase):
    def test_solid_opposing_wall_blocks(self):
        walls = [{"strike": 745.0, "right": "C", "wall_state": "REINFORCED"}]
        out = _runway_check(strike=750.0, spot=743.0, right="C", wall_records=walls)
        self.assertTrue(out["blocked"])
        self.assertIn("REINFORCED", out["reason"])

    def test_ghost_candidate_wall_does_not_block(self):
        # A wall mid-unwind isn't a real obstruction -- that's the entire
        # point of the ghost-wall state machine.
        walls = [{"strike": 745.0, "right": "C", "wall_state": "GHOST_CANDIDATE"}]
        out = _runway_check(strike=750.0, spot=743.0, right="C", wall_records=walls)
        self.assertFalse(out["blocked"])

    def test_wall_outside_path_does_not_block(self):
        walls = [{"strike": 760.0, "right": "C", "wall_state": "REINFORCED"}]
        out = _runway_check(strike=750.0, spot=743.0, right="C", wall_records=walls)
        self.assertFalse(out["blocked"])

    def test_no_walls_does_not_block(self):
        out = _runway_check(strike=750.0, spot=743.0, right="C", wall_records=[])
        self.assertFalse(out["blocked"])


class ScreenCandidatesTests(unittest.TestCase):
    def _universe(self):
        return {"spot": 743.5, "expirations": ["2026-07-27"]}

    def test_prefers_pass_over_wait_and_fail(self):
        book = pd.DataFrame([
            {"strike": 743.0, "right": "C", "bid": 0.60, "ask": 0.90,
             "bid_size": 100, "ask_size": 100, "quote_age_seconds": 1.0,
             "delta": 0.30, "implied_vol": 0.15},  # wide spread -> FAIL
            {"strike": 744.0, "right": "C", "bid": 0.30, "ask": 0.31,
             "bid_size": 100, "ask_size": 100, "quote_age_seconds": 45.0,
             "delta": 0.30, "implied_vol": 0.15},  # stale only -> WAIT
            {"strike": 745.0, "right": "C", "bid": 0.35, "ask": 0.36,
             "bid_size": 100, "ask_size": 100, "quote_age_seconds": 1.0,
             "delta": 0.30, "implied_vol": 0.15},  # clean -> PASS
        ])
        with patch("thetadata_pipeline.contract_quote.fetch_near_money_book", return_value=book):
            out = screen_candidates("SPY", "C", self._universe(), [], dt.datetime(2026, 7, 24, 12, 0))
        self.assertEqual(out["gate"], "PASS")
        self.assertEqual(out["candidate"]["strike"], 745.0)
        self.assertEqual(out["checked"], 3)

    def test_no_matching_right_raises(self):
        book = pd.DataFrame([
            {"strike": 743.0, "right": "P", "bid": 0.60, "ask": 0.61,
             "bid_size": 100, "ask_size": 100, "quote_age_seconds": 1.0,
             "delta": -0.30, "implied_vol": 0.15},
        ])
        with patch("thetadata_pipeline.contract_quote.fetch_near_money_book", return_value=book):
            with self.assertRaises(ContractScreenUnavailable):
                screen_candidates("SPY", "C", self._universe(), [], dt.datetime(2026, 7, 24, 12, 0))

    def test_empty_book_raises(self):
        with patch("thetadata_pipeline.contract_quote.fetch_near_money_book", return_value=pd.DataFrame()):
            with self.assertRaises(ContractScreenUnavailable):
                screen_candidates("SPY", "C", self._universe(), [], dt.datetime(2026, 7, 24, 12, 0))

    def test_no_spot_raises_without_calling_thetadata(self):
        with patch("thetadata_pipeline.contract_quote.fetch_near_money_book") as mock_fetch:
            with self.assertRaises(ContractScreenUnavailable):
                screen_candidates("SPY", "C", {"spot": None, "expirations": ["2026-07-27"]}, [], dt.datetime(2026, 7, 24, 12, 0))
            mock_fetch.assert_not_called()


class CandidateRankKeyTests(unittest.TestCase):
    def test_gate_priority_orders_pass_before_wait_before_fail(self):
        pass_c = {"gate": "PASS", "friction_ratio_pct": 20.0, "delta": 0.3, "spread": 0.01}
        wait_c = {"gate": "WAIT", "friction_ratio_pct": 5.0, "delta": 0.5, "spread": 0.005}
        fail_c = {"gate": "FAIL", "friction_ratio_pct": 1.0, "delta": 0.9, "spread": 0.001}
        ranked = sorted([fail_c, wait_c, pass_c], key=_candidate_rank_key)
        self.assertEqual([c["gate"] for c in ranked], ["PASS", "WAIT", "FAIL"])


if __name__ == "__main__":
    unittest.main()
