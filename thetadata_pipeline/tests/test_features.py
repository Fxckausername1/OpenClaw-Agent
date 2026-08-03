import unittest
from unittest.mock import patch

import pandas as pd

from thetadata_pipeline import features as feat
from thetadata_pipeline.schemas import WALL_GHOST_CANDIDATE, WALL_REINFORCED, WALL_STABLE


class FullPcTests(unittest.TestCase):
    def test_unavailable_without_live_gex_row(self):
        with patch.object(feat, "load_live_gex_row", return_value=None):
            out = feat.full_pc("SPY", pd.DataFrame())
        self.assertEqual(out["quality"], "UNAVAILABLE")
        self.assertIsNone(out["p_c_raw_full"])

    def test_matches_production_weights_when_f_state_zero(self):
        # With f=0 (no established-OI flow evidence), p_c_raw_partial (the
        # existing 3-factor redistribution) and p_c_raw_full should differ
        # ONLY because full includes w_flow*0 as a real zero rather than
        # excluding+redistributing -- i.e. full <= partial always when f=0,
        # since redistribution can only raise weight on the remaining
        # positive components.
        row = {"spot": 700.0, "flip": 750.0, "net_vex": -1e8}  # negative gamma-ish setup
        with patch.object(feat, "load_live_gex_row", return_value=row), \
             patch.object(feat, "load_vex_history_copy", return_value=[1e8, 2e8]), \
             patch.object(feat, "load_vix_vxv_ratio", return_value=1.05):
            out = feat.full_pc("SPY", pd.DataFrame())  # empty wall_df -> v_oi=0, bid_dominant=False -> f=0
        self.assertEqual(out["quality"], "FRESH")
        self.assertEqual(out["f_state"], 0.0)
        self.assertLessEqual(out["p_c_raw_full"], out["p_c_raw_partial"] + 1e-9)

    def test_real_flow_raises_f_state_above_zero(self):
        wall_df = pd.DataFrame([{
            "established_oi": True, "intraday_volume": 300, "oi": 200.0,
            "bid_fraction": 0.9, "classified_volume": 300,
        }])
        row = {"spot": 700.0, "flip": 750.0, "net_vex": -1e8}
        with patch.object(feat, "load_live_gex_row", return_value=row), \
             patch.object(feat, "load_vex_history_copy", return_value=[1e8]), \
             patch.object(feat, "load_vix_vxv_ratio", return_value=1.05):
            out = feat.full_pc("SPY", wall_df)
        self.assertGreater(out["f_state"], 0.0)
        # full formula weighs f_state at 0.3 -- a real positive f must raise
        # p_c_raw_full relative to the zero-flow case.
        self.assertGreater(out["p_c_raw_full"], 0.0)


class GhostWallSummaryTests(unittest.TestCase):
    def test_flags_true_when_any_ghost_candidate(self):
        wall_df = pd.DataFrame([
            {"wall_state": WALL_STABLE, "contract_id": "A"},
            {"wall_state": WALL_GHOST_CANDIDATE, "contract_id": "B"},
        ])
        out = feat.ghost_wall_summary(wall_df)
        self.assertTrue(out["ghost_wall"])
        self.assertEqual(len(out["walls"]), 2)

    def test_false_when_empty(self):
        out = feat.ghost_wall_summary(pd.DataFrame())
        self.assertFalse(out["ghost_wall"])


class OptionsCvdTests(unittest.TestCase):
    def _minute_df(self):
        return pd.DataFrame([{
            "contract_id": "SPY_20260724_000750000_C", "minute": pd.Timestamp("2026-07-24 10:00:00"),
            "ask_contracts": 10, "bid_contracts": 2, "mid_contracts": 1, "excluded_contracts": 0,
            "ask_premium": 1000.0, "bid_premium": 100.0,
        }])

    def test_calls_bought_are_bullish(self):
        contract_meta = {"SPY_20260724_000750000_C": {"right": "C"}}
        greeks = {"SPY_20260724_000750000_C": 0.5}
        out = feat.options_cvd(self._minute_df(), contract_meta, greeks, underlying_price=750.0)
        self.assertGreater(out["delta_notional_flow"], 0)
        self.assertTrue(out["estimated_delta"])

    def test_puts_bought_are_bearish(self):
        contract_meta = {"SPY_20260724_000750000_C": {"right": "P"}}
        greeks = {"SPY_20260724_000750000_C": 0.5}
        out = feat.options_cvd(self._minute_df(), contract_meta, greeks, underlying_price=750.0)
        self.assertLess(out["delta_notional_flow"], 0)

    def test_missing_underlying_price_is_unavailable(self):
        out = feat.options_cvd(self._minute_df(), {}, {}, underlying_price=None)
        self.assertIsNone(out["delta_notional_flow"])

    def test_empty_minute_df(self):
        out = feat.options_cvd(pd.DataFrame(), {}, {}, underlying_price=750.0)
        self.assertIsNone(out["options_cvd"])


class DivergenceFlagsTests(unittest.TestCase):
    def test_bearish_divergence(self):
        self.assertIn("bearish_divergence", feat.divergence_flags(price_change=1.0, cvd_change=-1.0))

    def test_bullish_divergence(self):
        self.assertIn("bullish_divergence", feat.divergence_flags(price_change=-1.0, cvd_change=1.0))

    def test_none_inputs_produce_no_flags(self):
        self.assertEqual(feat.divergence_flags(None, None), [])


def _greek(strike, right, iv, expiration="2026-07-24", delta=0.5):
    return {"strike": strike, "right": right, "expiration": expiration, "implied_vol": iv, "delta": delta}


class IvSkewFeaturesTests(unittest.TestCase):
    def test_unavailable_without_greeks_or_spot(self):
        self.assertEqual(feat.iv_skew_features({}, 700.0)["quality"], "UNAVAILABLE")
        self.assertEqual(feat.iv_skew_features({"a": _greek(700, "C", 0.2)}, None)["quality"], "UNAVAILABLE")

    def test_picks_nearest_strike_as_atm(self):
        greeks = {
            "c1": _greek(695, "C", 0.20), "c2": _greek(700, "C", 0.22), "c3": _greek(705, "C", 0.24),
            "p1": _greek(700, "P", 0.25),
        }
        out = feat.iv_skew_features(greeks, spot=701.0)
        self.assertAlmostEqual(out["atm_iv_by_expiration"]["2026-07-24"]["call"], 0.22)  # 700 nearer to 701 than 705
        self.assertAlmostEqual(out["put_call_skew"]["2026-07-24"], 0.25 - 0.22)

    def test_atm_iv_change_vs_prior(self):
        greeks = {"c1": _greek(700, "C", 0.25), "p1": _greek(700, "P", 0.28)}
        prior = {"2026-07-24": {"call": 0.20, "put": 0.30}}
        out = feat.iv_skew_features(greeks, spot=700.0, prior_atm_iv=prior)
        self.assertAlmostEqual(out["atm_iv_change"]["2026-07-24"]["call"], 0.05)
        self.assertAlmostEqual(out["atm_iv_change"]["2026-07-24"]["put"], -0.02)

    def test_zero_dte_vs_next_expiry(self):
        greeks = {
            "c1": _greek(700, "C", 0.30, expiration="2026-07-24"),
            "p1": _greek(700, "P", 0.30, expiration="2026-07-24"),
            "c2": _greek(700, "C", 0.20, expiration="2026-07-25"),
            "p2": _greek(700, "P", 0.20, expiration="2026-07-25"),
        }
        out = feat.iv_skew_features(greeks, spot=700.0)
        self.assertAlmostEqual(out["zero_dte_vs_next_iv"], 0.10)

    def test_smile_stability_flags_a_real_jump(self):
        greeks = {
            "c1": _greek(695, "C", 0.20), "c2": _greek(700, "C", 0.21), "c3": _greek(705, "C", 0.50),
        }
        out = feat.iv_skew_features(greeks, spot=700.0)
        self.assertTrue(out["smile_stability"]["flag"])
        self.assertAlmostEqual(out["smile_stability"]["max_adjacent_jump"], 0.29)

    def test_smile_stable_does_not_flag(self):
        greeks = {"c1": _greek(695, "C", 0.20), "c2": _greek(700, "C", 0.21), "c3": _greek(705, "C", 0.22)}
        out = feat.iv_skew_features(greeks, spot=700.0)
        self.assertFalse(out["smile_stability"]["flag"])


class DealerPositioningLeanTests(unittest.TestCase):
    def test_unavailable_without_live_gex_row(self):
        self.assertEqual(feat.dealer_positioning_lean(None, [])["lean"], "unavailable")

    def test_bullish_when_call_wall_cracking(self):
        row = {"call_wall": 710.0, "put_wall": 690.0}
        walls = [
            {"strike": 710.0, "right": "C", "wall_state": WALL_GHOST_CANDIDATE},
            {"strike": 690.0, "right": "P", "wall_state": WALL_STABLE},
        ]
        self.assertEqual(feat.dealer_positioning_lean(row, walls)["lean"], "bullish")

    def test_bearish_when_put_wall_cracking(self):
        row = {"call_wall": 710.0, "put_wall": 690.0}
        walls = [
            {"strike": 710.0, "right": "C", "wall_state": WALL_STABLE},
            {"strike": 690.0, "right": "P", "wall_state": WALL_GHOST_CANDIDATE},
        ]
        self.assertEqual(feat.dealer_positioning_lean(row, walls)["lean"], "bearish")

    def test_neutral_when_both_stable(self):
        row = {"call_wall": 710.0, "put_wall": 690.0}
        walls = [
            {"strike": 710.0, "right": "C", "wall_state": WALL_STABLE},
            {"strike": 690.0, "right": "P", "wall_state": WALL_STABLE},
        ]
        self.assertEqual(feat.dealer_positioning_lean(row, walls)["lean"], "neutral")

    def test_bearish_when_call_wall_reinforced_asymmetrically(self):
        row = {"call_wall": 710.0, "put_wall": 690.0}
        walls = [
            {"strike": 710.0, "right": "C", "wall_state": WALL_REINFORCED},
            {"strike": 690.0, "right": "P", "wall_state": WALL_STABLE},
        ]
        self.assertEqual(feat.dealer_positioning_lean(row, walls)["lean"], "bearish")


class OptionsFlowLeanTests(unittest.TestCase):
    def test_unavailable_without_cvd(self):
        self.assertEqual(feat.options_flow_lean({})["lean"], "unavailable")

    def test_bullish_positive_cvd(self):
        self.assertEqual(feat.options_flow_lean({"options_cvd": 1000.0, "coverage": 0.8})["lean"], "bullish")

    def test_bearish_negative_cvd(self):
        self.assertEqual(feat.options_flow_lean({"options_cvd": -1000.0, "coverage": 0.8})["lean"], "bearish")

    def test_unavailable_when_coverage_too_low(self):
        out = feat.options_flow_lean({"options_cvd": 1000.0, "coverage": 0.1})
        self.assertEqual(out["lean"], "unavailable")


class VolatilityLeanTests(unittest.TestCase):
    def test_unavailable_without_skew(self):
        self.assertEqual(feat.volatility_lean({})["lean"], "unavailable")

    def test_bearish_when_puts_richer(self):
        out = feat.volatility_lean({"put_call_skew": {"2026-07-24": 0.05}})
        self.assertEqual(out["lean"], "bearish")

    def test_bullish_when_calls_richer(self):
        out = feat.volatility_lean({"put_call_skew": {"2026-07-24": -0.05}})
        self.assertEqual(out["lean"], "bullish")

    def test_neutral_small_skew(self):
        out = feat.volatility_lean({"put_call_skew": {"2026-07-24": 0.001}})
        self.assertEqual(out["lean"], "neutral")


class ControlMapVerdictTests(unittest.TestCase):
    def _bullish_gex_row(self):
        return {"call_wall": 710.0, "put_wall": 690.0}

    def _bullish_walls(self):
        return [
            {"strike": 710.0, "right": "C", "wall_state": WALL_GHOST_CANDIDATE},
            {"strike": 690.0, "right": "P", "wall_state": WALL_STABLE},
        ]

    def test_wait_data_when_fewer_than_two_families_available(self):
        out = feat.control_map_verdict(None, [], {}, {})
        self.assertEqual(out["verdict"], "WAIT - DATA")

    def test_call_watch_when_two_families_agree_bullish(self):
        cvd = {"options_cvd": 1000.0, "coverage": 0.8}
        out = feat.control_map_verdict(self._bullish_gex_row(), self._bullish_walls(), cvd, {})
        self.assertEqual(out["verdict"], "CALL WATCH")

    def test_two_sided_on_real_conflict(self):
        cvd = {"options_cvd": -1000.0, "coverage": 0.8}  # bearish flow vs bullish dealer positioning
        out = feat.control_map_verdict(self._bullish_gex_row(), self._bullish_walls(), cvd, {})
        self.assertEqual(out["verdict"], "TWO-SIDED")

    def test_no_trade_structure_when_all_neutral(self):
        row = {"call_wall": 710.0, "put_wall": 690.0}
        walls = [
            {"strike": 710.0, "right": "C", "wall_state": WALL_STABLE},
            {"strike": 690.0, "right": "P", "wall_state": WALL_STABLE},
        ]
        cvd = {"options_cvd": 0.0, "coverage": 0.8}
        out = feat.control_map_verdict(row, walls, cvd, {})
        self.assertEqual(out["verdict"], "NO TRADE - STRUCTURE")

    def test_narrative_test_identifies_the_hinge_family(self):
        # dealer_positioning + options_flow both bullish, volatility unavailable
        # -> verdict is CALL WATCH driven by those two; zeroing EITHER one should
        # flip it (drops below the "2 agreeing" bar), proving they're the real hinge.
        cvd = {"options_cvd": 1000.0, "coverage": 0.8}
        out = feat.control_map_verdict(self._bullish_gex_row(), self._bullish_walls(), cvd, {})
        self.assertEqual(out["verdict"], "CALL WATCH")
        self.assertIn("dealer_positioning", out["narrative_test"]["hinge_families"])
        self.assertIn("options_flow", out["narrative_test"]["hinge_families"])

    def test_narrative_test_shows_supporting_context_not_hinge(self):
        # All three bullish -- volatility agrees but isn't NEEDED (dealer+flow
        # alone already clear the 2-agreeing bar), so zeroing it should NOT flip
        # the verdict -- exactly CB-V4's "supporting context, not hinge" case.
        cvd = {"options_cvd": 1000.0, "coverage": 0.8}
        iv_skew = {"put_call_skew": {"2026-07-24": -0.05}}  # bullish
        out = feat.control_map_verdict(self._bullish_gex_row(), self._bullish_walls(), cvd, iv_skew)
        self.assertEqual(out["verdict"], "CALL WATCH")
        self.assertIn("volatility", out["narrative_test"]["supporting_context_families"])
        self.assertNotIn("volatility", out["narrative_test"]["hinge_families"])


if __name__ == "__main__":
    unittest.main()
