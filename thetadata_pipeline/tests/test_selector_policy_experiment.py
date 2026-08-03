import datetime as dt
import unittest
from zoneinfo import ZoneInfo

import pandas as pd

from thetadata_pipeline.bt2_schemas import GATE_PASS
from thetadata_pipeline.bt_dedup import AdmissionPolicy
from thetadata_pipeline.selector_policy_experiment import (
    DEBIT_CAP_DOLLARS, DEFAULT_FEE_PER_CONTRACT, HOLDOUT_END, HOLDOUT_START,
    TRAIN_END, TRAIN_START, VARIANT_CONFIGS, _simulate_variant_session,
    segment_of, total_debit_dollars,
)
from thetadata_pipeline.selector_policy_stats import (
    by_time_of_day, delta_distribution, filter_segment, max_drawdown_dollars,
    premium_stats, sweep_reclaim_premium_relief_check, variant_summary,
)

ET = ZoneInfo("America/New_York")


class TotalDebitDollarsTests(unittest.TestCase):
    def test_matches_hand_computation(self):
        self.assertEqual(total_debit_dollars(0.25, quantity=1, fee_per_contract=0.05), 25.05)

    def test_scales_with_quantity(self):
        self.assertEqual(total_debit_dollars(0.25, quantity=2, fee_per_contract=0.05), 50.10)

    def test_fee_inclusion_is_the_deciding_factor_at_the_boundary(self):
        # ask=$1.00 -> raw premium debit is EXACTLY $100.00 (would pass a
        # naive "ask*100 <= 100" check with no fee). This is the concrete
        # case heff asked to be verified: the cap must use ask*100*qty+fees,
        # not the quoted price alone.
        without_fee = total_debit_dollars(1.00, quantity=1, fee_per_contract=0.0)
        with_fee = total_debit_dollars(1.00, quantity=1, fee_per_contract=DEFAULT_FEE_PER_CONTRACT)
        self.assertEqual(without_fee, 100.00)
        self.assertGreater(with_fee, DEBIT_CAP_DOLLARS)
        self.assertEqual(with_fee, 100.05)


class SegmentOfTests(unittest.TestCase):
    def test_train_boundaries_inclusive(self):
        self.assertEqual(segment_of(TRAIN_START), "train")
        self.assertEqual(segment_of(TRAIN_END), "train")

    def test_holdout_boundaries_inclusive(self):
        self.assertEqual(segment_of(HOLDOUT_START), "holdout")
        self.assertEqual(segment_of(HOLDOUT_END), "holdout")

    def test_no_gap_or_overlap_at_the_seam(self):
        self.assertEqual(segment_of("2026-06-10"), "train")
        self.assertEqual(segment_of("2026-06-11"), "holdout")

    def test_outside_dataset_range_is_unassigned(self):
        self.assertEqual(segment_of("2020-01-01"), "unassigned")
        self.assertEqual(segment_of("2030-01-01"), "unassigned")


def _trade_row(contract_id, expiration, strike, right, ts, bid, ask, bid_size=50, ask_size=50):
    return dict(contract_id=contract_id, expiration=expiration, strike=strike, right=right,
                trade_timestamp=ts, bid=bid, ask=ask, bid_size=bid_size, ask_size=ask_size)


def _signal(session="2026-01-05", trigger="MSS", underlying_price=620.0, side="CALL WATCH"):
    decision_ts = dt.datetime(2026, 1, 5, 10, 0, 0, tzinfo=ET)
    return {
        "symbol": "QQQ", "session": session, "decision_ts": decision_ts,
        "direction": side, "trigger": trigger, "score": 6.0,
        "underlying_price": underlying_price, "bar_index": 100,
        "in_charter_window": True,
    }


class VariantSelectionIntegrationTests(unittest.TestCase):
    """Exercises the real pipeline (both monkeypatch mechanisms together),
    not just the helper functions in isolation."""

    def test_variant_a_selects_in_band_candidate_and_fills(self):
        sig = _signal(underlying_price=620.0)
        ts = pd.Timestamp(sig["decision_ts"]) - pd.Timedelta(seconds=1)
        trades_df = pd.DataFrame([
            _trade_row("QQQ260107C00622000", "2026-01-07", 622.0, "C", ts, 0.24, 0.26),
        ])
        rows = _simulate_variant_session(
            "A_baseline", VARIANT_CONFIGS["A_baseline"], [sig], trades_df,
            policy=AdmissionPolicy(),
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["contract_gate"], GATE_PASS)
        self.assertIsNotNone(rows[0]["_selected_delta"])

    def test_variant_a_rejects_expensive_candidate_variant_b_accepts_it(self):
        sig = _signal(underlying_price=620.0)
        ts = pd.Timestamp(sig["decision_ts"]) - pd.Timedelta(seconds=1)
        # ask=$0.60 -- outside A's $0.20-$0.30 band, but well under B's
        # $100 total-debit cap ($60.05).
        trades_df = pd.DataFrame([
            _trade_row("QQQ260107C00622000", "2026-01-07", 622.0, "C", ts, 0.58, 0.60),
        ])
        rows_a = _simulate_variant_session(
            "A_baseline", VARIANT_CONFIGS["A_baseline"], [sig], trades_df,
            policy=AdmissionPolicy(),
        )
        rows_b = _simulate_variant_session(
            "B_delta_first_debit_cap", VARIANT_CONFIGS["B_delta_first_debit_cap"], [sig], trades_df,
            policy=AdmissionPolicy(),
        )
        self.assertNotEqual(rows_a[0]["contract_gate"], GATE_PASS)
        self.assertEqual(rows_b[0]["contract_gate"], GATE_PASS)

    def test_variant_b_still_rejects_candidate_over_the_debit_cap(self):
        sig = _signal(underlying_price=620.0)
        ts = pd.Timestamp(sig["decision_ts"]) - pd.Timedelta(seconds=1)
        # ask=$1.84 -- real example from SELECTOR_REJECTION_AUDIT_v1.md,
        # total debit $184.05, well over B's $100 cap.
        trades_df = pd.DataFrame([
            _trade_row("QQQ260107C00622000", "2026-01-07", 622.0, "C", ts, 1.81, 1.84),
        ])
        rows_b = _simulate_variant_session(
            "B_delta_first_debit_cap", VARIANT_CONFIGS["B_delta_first_debit_cap"], [sig], trades_df,
            policy=AdmissionPolicy(),
        )
        self.assertNotEqual(rows_b[0]["contract_gate"], GATE_PASS)

    def test_variant_c3_widest_band_accepts_the_1_84_candidate(self):
        sig = _signal(underlying_price=620.0)
        ts = pd.Timestamp(sig["decision_ts"]) - pd.Timedelta(seconds=1)
        trades_df = pd.DataFrame([
            _trade_row("QQQ260107C00622000", "2026-01-07", 622.0, "C", ts, 1.81, 1.84),
        ])
        rows_c3 = _simulate_variant_session(
            "C3_premium_0.20_2.50", VARIANT_CONFIGS["C3_premium_0.20_2.50"], [sig], trades_df,
            policy=AdmissionPolicy(),
        )
        self.assertEqual(rows_c3[0]["contract_gate"], GATE_PASS)

    def test_evaluate_candidate_patch_is_restored_after_variant_b(self):
        from thetadata_pipeline import bt2_selector
        from thetadata_pipeline.bt2_selector import evaluate_candidate as real_fn
        sig = _signal()
        ts = pd.Timestamp(sig["decision_ts"]) - pd.Timedelta(seconds=1)
        trades_df = pd.DataFrame([_trade_row("QQQ260107C00622000", "2026-01-07", 622.0, "C", ts, 0.58, 0.60)])
        _simulate_variant_session(
            "B_delta_first_debit_cap", VARIANT_CONFIGS["B_delta_first_debit_cap"], [sig], trades_df,
            policy=AdmissionPolicy(),
        )
        self.assertIs(bt2_selector.evaluate_candidate, real_fn)

    def test_select_contract_patch_is_restored_after_a_single_session_call(self):
        # _simulate_variant_session installs/restores its own select_contract
        # patch -- self-contained, no caller setup required.
        from thetadata_pipeline import bt2_simulator as bt2_simulator_mod
        from thetadata_pipeline.bt2_selector import select_contract as real_fn
        sig = _signal()
        ts = pd.Timestamp(sig["decision_ts"]) - pd.Timedelta(seconds=1)
        trades_df = pd.DataFrame([_trade_row("QQQ260107C00622000", "2026-01-07", 622.0, "C", ts, 0.24, 0.26)])
        _simulate_variant_session(
            "A_baseline", VARIANT_CONFIGS["A_baseline"], [sig], trades_df,
            policy=AdmissionPolicy(),
        )
        self.assertIs(bt2_simulator_mod.select_contract, real_fn)


def _row(session="2026-01-05", net_pnl=None, exit_ts=None, contract_gate=None,
         delta=None, trigger="MSS", tod="10:00-11:00", entry_ask=None,
         gross_pnl=None, fees=0.05, exit_reason="NO_CONTRACT"):
    return {
        "session": session, "decision_ts": f"{session}T10:00:00-05:00",
        "net_pnl": net_pnl, "exit_ts": exit_ts, "contract_gate": contract_gate,
        "_selected_delta": delta, "heff_smc_trigger": trigger,
        "time_of_day_bucket": tod, "entry_ask": [entry_ask] if entry_ask is not None else [],
        "_selected_ask_at_selection": entry_ask, "gross_pnl": gross_pnl, "fees": fees,
        "exit_reason": exit_reason, "segment": "train",
    }


class MaxDrawdownTests(unittest.TestCase):
    def test_simple_known_drawdown(self):
        rows = [
            _row(net_pnl=10.0, exit_ts="2026-01-05T10:05:00-05:00"),
            _row(net_pnl=-30.0, exit_ts="2026-01-05T11:05:00-05:00"),
            _row(net_pnl=5.0, exit_ts="2026-01-05T12:05:00-05:00"),
        ]
        # cumulative: 10 -> -20 -> -15. Peak=10, trough=-20 -> drawdown=30.
        result = max_drawdown_dollars(rows)
        self.assertEqual(result["max_drawdown_dollars"], 30.0)
        self.assertEqual(result["final_cumulative_pnl"], -15.0)

    def test_no_filled_trades_returns_none(self):
        rows = [_row(net_pnl=None)]
        self.assertIsNone(max_drawdown_dollars(rows))

    def test_monotonically_increasing_curve_has_zero_drawdown(self):
        rows = [
            _row(net_pnl=5.0, exit_ts="2026-01-05T10:05:00-05:00"),
            _row(net_pnl=5.0, exit_ts="2026-01-05T11:05:00-05:00"),
        ]
        result = max_drawdown_dollars(rows)
        self.assertEqual(result["max_drawdown_dollars"], 0.0)


class DeltaDistributionTests(unittest.TestCase):
    def test_uses_absolute_value_and_buckets_correctly(self):
        rows = [
            _row(contract_gate=GATE_PASS, delta=-0.35),
            _row(contract_gate=GATE_PASS, delta=0.12),
            _row(contract_gate=GATE_PASS, delta=0.55),
        ]
        result = delta_distribution(rows)
        self.assertEqual(result["n"], 3)
        self.assertEqual(result["histogram"]["0.30-0.40"], 1)
        self.assertEqual(result["histogram"]["0.10-0.15"], 1)
        self.assertEqual(result["histogram"]["0.50+"], 1)

    def test_excludes_signals_that_never_selected_a_contract(self):
        rows = [_row(contract_gate=None, delta=None), _row(contract_gate=GATE_PASS, delta=0.35)]
        result = delta_distribution(rows)
        self.assertEqual(result["n"], 1)

    def test_no_selections_returns_none(self):
        self.assertIsNone(delta_distribution([_row(contract_gate=None, delta=None)]))


class PremiumStatsTests(unittest.TestCase):
    def test_separates_selection_time_and_fill_time_premiums(self):
        rows = [_row(contract_gate=GATE_PASS, entry_ask=0.55, net_pnl=10.0)]
        result = premium_stats(rows)
        self.assertEqual(result["selected_at_selection"]["mean"], 0.55)
        self.assertEqual(result["filled_entry"]["mean"], 0.55)


class ByTimeOfDayTests(unittest.TestCase):
    def test_splits_by_bucket(self):
        rows = [
            _row(tod="09:30-10:00", net_pnl=10.0),
            _row(tod="09:30-10:00", net_pnl=None),
            _row(tod="10:00-11:00", net_pnl=-5.0),
        ]
        result = by_time_of_day(rows)
        self.assertEqual(result["09:30-10:00"]["n_signals"], 2)
        self.assertEqual(result["09:30-10:00"]["n_filled"], 1)
        self.assertEqual(result["10:00-11:00"]["n_filled"], 1)


class SweepReclaimReliefCheckTests(unittest.TestCase):
    def test_compares_fill_rate_gap_across_variants(self):
        all_rows = {
            "A_baseline": [
                _row(trigger="SWEEP_RECLAIM", net_pnl=None),
                _row(trigger="SWEEP_RECLAIM", net_pnl=5.0),
                _row(trigger="MSS", net_pnl=5.0),
                _row(trigger="MSS", net_pnl=5.0),
            ],
        }
        result = sweep_reclaim_premium_relief_check(all_rows)
        self.assertEqual(result["A_baseline"]["sweep_reclaim_n_signals"], 2)
        self.assertEqual(result["A_baseline"]["sweep_reclaim_fill_rate"], 0.5)
        self.assertEqual(result["A_baseline"]["other_triggers_fill_rate"], 1.0)


class VariantSummarySmokeTest(unittest.TestCase):
    def test_runs_without_error_on_a_small_synthetic_set(self):
        rows = [
            _row(contract_gate=GATE_PASS, net_pnl=10.0, delta=0.35, entry_ask=0.5,
                 exit_ts="2026-01-05T10:05:00-05:00", gross_pnl=11.0),
            _row(contract_gate=None, net_pnl=None),
        ]
        summary = variant_summary(rows)
        self.assertEqual(summary["n_signals"], 2)
        self.assertEqual(summary["n_selectable_contracts"], 1)
        self.assertEqual(summary["n_filled"], 1)
        self.assertIsNotNone(summary["max_drawdown"])
        self.assertIsNotNone(summary["delta_distribution"])


if __name__ == "__main__":
    unittest.main()
