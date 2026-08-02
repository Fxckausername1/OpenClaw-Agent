import datetime as dt
import unittest

import pandas as pd

from smc.selector_variant_b import (
    EXCLUDED_TRIGGERS, VARIANT_B_CONFIG, is_trigger_eligible, select_variant_b_contract,
)
from thetadata_pipeline.bt2_selector import evaluate_candidate as real_evaluate_candidate
from thetadata_pipeline import bt2_selector


def _book_row(strike, right, expiration, bid, ask, delta=0.35, bid_size=50, ask_size=50):
    return dict(strike=strike, right=right, expiration=expiration, bid=bid, ask=ask,
                bid_size=bid_size, ask_size=ask_size, delta=delta, quote_age_seconds=1.0)


class TriggerEligibilityTests(unittest.TestCase):
    def test_sweep_reclaim_excluded(self):
        self.assertFalse(is_trigger_eligible("SWEEP_RECLAIM"))

    def test_other_triggers_eligible(self):
        for trig in ("MSS", "BOS", "MA_FADE", "PULLBACK"):
            self.assertTrue(is_trigger_eligible(trig), trig)

    def test_excluded_set_contains_only_sweep_reclaim(self):
        self.assertEqual(EXCLUDED_TRIGGERS, frozenset({"SWEEP_RECLAIM"}))


class ConfigTests(unittest.TestCase):
    def test_target_delta_is_035(self):
        self.assertEqual(VARIANT_B_CONFIG.target_delta, 0.35)

    def test_premium_band_effectively_disabled(self):
        self.assertEqual(VARIANT_B_CONFIG.premium_low, 0.0)
        self.assertEqual(VARIANT_B_CONFIG.premium_high, float("inf"))

    def test_other_fields_match_bt2_selector_defaults(self):
        from thetadata_pipeline.bt2_selector import SelectorConfig
        baseline = SelectorConfig()
        self.assertEqual(VARIANT_B_CONFIG.allowed_dte, baseline.allowed_dte)
        self.assertEqual(VARIANT_B_CONFIG.max_spread_dollars, baseline.max_spread_dollars)
        self.assertEqual(VARIANT_B_CONFIG.max_spread_pct_mid, baseline.max_spread_pct_mid)
        self.assertEqual(VARIANT_B_CONFIG.min_abs_delta, baseline.min_abs_delta)
        self.assertEqual(VARIANT_B_CONFIG.max_quote_age_seconds, baseline.max_quote_age_seconds)
        self.assertEqual(VARIANT_B_CONFIG.min_ask_size, baseline.min_ask_size)


class SelectVariantBContractTests(unittest.TestCase):
    def setUp(self):
        self.decision_ts = dt.datetime(2026, 1, 5, 10, 0, 0, tzinfo=dt.timezone.utc)
        self.expiration = "2026-01-07"

    def test_selects_candidate_outside_old_premium_band(self):
        # ask=$0.60 -- outside the OLD moderate_combo band ($0.15-$0.40) and
        # the backtest baseline band ($0.20-$0.30), but well under the $100
        # debit cap ($60.05) -- proves the premium override actually works.
        book = pd.DataFrame([_book_row(622.0, "C", self.expiration, 0.58, 0.60, delta=0.35)])
        result = select_variant_b_contract(book, "C", self.decision_ts)
        self.assertTrue(result.found)
        self.assertEqual(result.contract["strike"], 622.0)

    def test_rejects_candidate_over_debit_cap(self):
        # ask=$1.84 -- real example from SELECTOR_REJECTION_AUDIT_v1.md,
        # total debit $184.05, over the $100 cap.
        book = pd.DataFrame([_book_row(622.0, "C", self.expiration, 1.81, 1.84, delta=0.35)])
        result = select_variant_b_contract(book, "C", self.decision_ts)
        self.assertFalse(result.found)

    def test_picks_closest_to_target_delta_among_passing_candidates(self):
        # Identical bid/ask (hence identical spread_pct_mid) across all three
        # -- select_contract's primary quality key is spread_pct_mid, delta-
        # closeness is only the tiebreaker. Holding spread constant isolates
        # the delta tiebreak this test exists to check.
        book = pd.DataFrame([
            _book_row(620.0, "C", self.expiration, 0.57, 0.60, delta=0.50),
            _book_row(625.0, "C", self.expiration, 0.57, 0.60, delta=0.35),
            _book_row(630.0, "C", self.expiration, 0.57, 0.60, delta=0.20),
        ])
        result = select_variant_b_contract(book, "C", self.decision_ts)
        self.assertTrue(result.found)
        self.assertEqual(result.contract["strike"], 625.0)

    def test_empty_book_returns_not_found(self):
        book = pd.DataFrame(columns=["strike", "right", "expiration", "bid", "ask",
                                     "bid_size", "ask_size", "delta", "quote_age_seconds"])
        result = select_variant_b_contract(book, "C", self.decision_ts)
        self.assertFalse(result.found)

    def test_evaluate_candidate_patch_is_always_restored(self):
        book = pd.DataFrame([_book_row(622.0, "C", self.expiration, 0.58, 0.60)])
        select_variant_b_contract(book, "C", self.decision_ts)
        self.assertIs(bt2_selector.evaluate_candidate, real_evaluate_candidate)

    def test_patch_restored_even_when_select_contract_raises(self):
        # A malformed book (missing required column) should make the real
        # select_contract raise -- prove the patch still gets restored.
        bad_book = pd.DataFrame([{"strike": 622.0}])  # missing right/expiration/bid/ask
        with self.assertRaises(Exception):
            select_variant_b_contract(bad_book, "C", self.decision_ts)
        self.assertIs(bt2_selector.evaluate_candidate, real_evaluate_candidate)


if __name__ == "__main__":
    unittest.main()
