import unittest

from thetadata_pipeline.selector_rejection_audit import (
    CATEGORY_GROUP, NO_CONTRACT_REASON_NONE_PASS, RELAXABLE_RULES, RULE_ORDER,
    _candidate_passes_without_rule, _first_and_all_reasons, candidates_index,
    classify_reason, counterfactual_gate_sensitivity, outcome_of, time_of_day_bucket,
    trigger_comparison_with_ci, wilson_ci,
)


class ClassifyReasonTests(unittest.TestCase):
    def test_two_sided_quote_reason(self):
        self.assertEqual(
            classify_reason("no valid two-sided quote (missing/zero/crossed)"),
            "no_two_sided_quote",
        )

    def test_premium_band_reason(self):
        self.assertEqual(
            classify_reason("ask $0.45 outside preferred premium band $0.20-$0.30"),
            "premium_band",
        )

    def test_delta_floor_reason(self):
        self.assertEqual(classify_reason("|delta| 0.08 below 0.15 floor"), "delta_floor")

    def test_delta_unavailable_reason(self):
        self.assertEqual(classify_reason("delta unavailable"), "delta_floor")

    def test_spread_pct_reason(self):
        self.assertEqual(classify_reason("spread 22.0% exceeds 15% of mid"), "spread_pct")

    def test_spread_dollars_reason(self):
        self.assertEqual(classify_reason("spread $0.08 exceeds $0.05 limit"), "spread_dollars")

    def test_quote_age_reason(self):
        self.assertEqual(classify_reason("quote age 42.0 exceeds 10.0s ceiling"), "quote_age")

    def test_ask_size_reason(self):
        self.assertEqual(
            classify_reason("displayed ask size 2.0 below 5.0 liquidity floor"),
            "ask_size",
        )

    def test_dte_reason(self):
        self.assertEqual(classify_reason("DTE 3 not in allowed set [0, 1, 2]"), "dte")

    def test_unrecognized_reason_falls_to_other(self):
        self.assertEqual(classify_reason("some brand new future rule text"), "other")

    def test_every_rule_order_entry_has_a_category_group(self):
        for rule in RULE_ORDER:
            if rule == "no_candidates_in_book":
                continue
            self.assertIn(rule, CATEGORY_GROUP, f"{rule} missing from CATEGORY_GROUP")


class FirstAndAllReasonsTests(unittest.TestCase):
    def test_empty_candidates_returns_none_primary(self):
        result = _first_and_all_reasons([])
        self.assertIsNone(result["primary_category"])
        self.assertEqual(result["all_categories"], [])
        self.assertEqual(result["n_candidates_checked"], 0)

    def test_picks_least_bad_candidate_by_fewest_reasons(self):
        candidates = [
            {"reasons": ["ask $0.45 outside preferred premium band $0.20-$0.30",
                         "|delta| 0.08 below 0.15 floor"], "spread_pct_mid": 0.05},
            {"reasons": ["|delta| 0.08 below 0.15 floor"], "spread_pct_mid": 0.10},
        ]
        result = _first_and_all_reasons(candidates)
        self.assertEqual(result["n_candidates_checked"], 2)
        self.assertEqual(result["primary_category"], "delta_floor")
        self.assertEqual(result["closest_candidate_reasons"], ["|delta| 0.08 below 0.15 floor"])

    def test_primary_category_follows_fixed_rule_order_not_list_order(self):
        # reasons listed with ask_size first in the list, but RULE_ORDER puts
        # premium_band ahead of ask_size -- primary_category must reflect
        # evaluate_candidate's own check order, not list insertion order.
        candidates = [{
            "reasons": [
                "displayed ask size 2.0 below 5.0 liquidity floor",
                "ask $0.45 outside preferred premium band $0.20-$0.30",
            ],
            "spread_pct_mid": 0.05,
        }]
        result = _first_and_all_reasons(candidates)
        self.assertEqual(result["primary_category"], "premium_band")
        self.assertEqual(set(result["all_categories"]), {"premium_band", "ask_size"})

    def test_all_categories_is_union_across_every_candidate_not_just_closest(self):
        candidates = [
            {"reasons": ["quote age 42.0 exceeds 10.0s ceiling"], "spread_pct_mid": 0.05},
            {"reasons": ["|delta| 0.08 below 0.15 floor",
                         "spread $0.08 exceeds $0.05 limit"], "spread_pct_mid": 0.20},
        ]
        result = _first_and_all_reasons(candidates)
        self.assertEqual(result["primary_category"], "quote_age")
        self.assertEqual(set(result["all_categories"]), {"quote_age", "delta_floor", "spread_dollars"})

    def test_ties_broken_by_spread_pct_mid(self):
        candidates = [
            {"reasons": ["|delta| 0.08 below 0.15 floor"], "spread_pct_mid": 0.30},
            {"reasons": ["quote age 42.0 exceeds 10.0s ceiling"], "spread_pct_mid": 0.05},
        ]
        result = _first_and_all_reasons(candidates)
        # both have exactly 1 failing reason -> tie broken by lower spread_pct_mid
        self.assertEqual(result["primary_category"], "quote_age")


class OutcomeOfTests(unittest.TestCase):
    def test_filled_when_net_pnl_present(self):
        self.assertEqual(outcome_of({"net_pnl": 12.5, "exit_reason": "TARGET"}), "FILLED")

    def test_admission_reject(self):
        self.assertEqual(
            outcome_of({"net_pnl": None, "exit_reason": "ADMISSION_REJECT"}),
            "ADMISSION_REJECT",
        )

    def test_no_contract(self):
        self.assertEqual(
            outcome_of({"net_pnl": None, "exit_reason": "NO_CONTRACT"}),
            "NO_CONTRACT",
        )

    def test_no_fill(self):
        self.assertEqual(outcome_of({"net_pnl": None, "exit_reason": "NO_FILL"}), "NO_FILL")


class TimeOfDayBucketTests(unittest.TestCase):
    def test_charter_window_open(self):
        self.assertEqual(time_of_day_bucket("2026-01-05T10:15:00-05:00"), "10:00-11:00")

    def test_pre_open(self):
        self.assertEqual(time_of_day_bucket("2026-01-05T09:15:00-05:00"), "pre_open")

    def test_kill_zone_start(self):
        self.assertEqual(time_of_day_bucket("2026-01-05T09:45:00-05:00"), "09:30-10:00")

    def test_after_charter_window(self):
        self.assertEqual(time_of_day_bucket("2026-01-05T15:45:00-05:00"), "15:30-close")


class CandidatePassesWithoutRuleTests(unittest.TestCase):
    def test_passes_when_only_failing_reason_matches_rule(self):
        candidate = {"reasons": ["|delta| 0.08 below 0.15 floor"]}
        self.assertTrue(_candidate_passes_without_rule(candidate, "delta_floor"))

    def test_still_fails_when_other_reasons_remain(self):
        candidate = {"reasons": ["|delta| 0.08 below 0.15 floor",
                                  "quote age 42.0 exceeds 10.0s ceiling"]}
        self.assertFalse(_candidate_passes_without_rule(candidate, "delta_floor"))

    def test_already_passing_candidate_has_no_reasons_to_remove(self):
        self.assertTrue(_candidate_passes_without_rule({"reasons": []}, "delta_floor"))


def _sig(trigger="MSS", session="2026-01-05", decision_ts="2026-01-05T10:15:00-05:00"):
    return {"session": session, "decision_ts": __import__("datetime").datetime.fromisoformat(decision_ts),
            "trigger": trigger, "direction": "CALL WATCH", "score": 6.0,
            "bar_index": 1, "in_charter_window": True, "underlying_price": 500.0,
            "_factors": {"rvol": 0.0}}


def _no_contract_row(context_id="ctx1", data_quality=NO_CONTRACT_REASON_NONE_PASS):
    return {"context_snapshot_id": context_id, "net_pnl": None, "exit_reason": "NO_CONTRACT",
            "data_quality": data_quality}


class CounterfactualGateSensitivityTests(unittest.TestCase):
    def test_signal_counted_for_every_rule_that_would_unblock_it(self):
        records = [{
            "signal": _sig(), "row": _no_contract_row(),
            "candidates": [{"reasons": ["|delta| 0.08 below 0.15 floor"], "spread_pct_mid": 0.05}],
        }]
        result = counterfactual_gate_sensitivity(records)
        self.assertEqual(result["n_eligible_no_contract_signals"], 1)
        self.assertEqual(result["by_rule"]["delta_floor"]["signals_would_pass_gate"], 1)
        for rule in RELAXABLE_RULES:
            if rule != "delta_floor":
                self.assertEqual(result["by_rule"][rule]["signals_would_pass_gate"], 0)

    def test_signal_with_multiple_failing_rules_on_only_candidate_unblocks_neither_alone(self):
        records = [{
            "signal": _sig(), "row": _no_contract_row(),
            "candidates": [{"reasons": ["|delta| 0.08 below 0.15 floor",
                                         "quote age 42.0 exceeds 10.0s ceiling"], "spread_pct_mid": 0.05}],
        }]
        result = counterfactual_gate_sensitivity(records)
        self.assertEqual(result["by_rule"]["delta_floor"]["signals_would_pass_gate"], 0)
        self.assertEqual(result["by_rule"]["quote_age"]["signals_would_pass_gate"], 0)

    def test_filled_signals_excluded_from_eligible_count(self):
        records = [{
            "signal": _sig(), "row": {"context_snapshot_id": "ctx2", "net_pnl": 12.0, "exit_reason": "TARGET"},
            "candidates": [],
        }]
        result = counterfactual_gate_sensitivity(records)
        self.assertEqual(result["n_eligible_no_contract_signals"], 0)

    def test_no_candidates_in_book_signals_excluded(self):
        records = [{
            "signal": _sig(),
            "row": _no_contract_row(data_quality="no_candidates_in_book"),
            "candidates": [],
        }]
        result = counterfactual_gate_sensitivity(records)
        self.assertEqual(result["n_eligible_no_contract_signals"], 0)

    def test_by_trigger_breakdown_attributes_to_signal_trigger(self):
        records = [{
            "signal": _sig(trigger="SWEEP_RECLAIM"), "row": _no_contract_row(),
            "candidates": [{"reasons": ["|delta| 0.08 below 0.15 floor"], "spread_pct_mid": 0.05}],
        }]
        result = counterfactual_gate_sensitivity(records)
        self.assertEqual(result["by_rule"]["delta_floor"]["by_trigger"], {"SWEEP_RECLAIM": 1})


class CandidatesIndexTests(unittest.TestCase):
    def test_indexes_none_pass_signals_by_context_id(self):
        records = [{
            "signal": _sig(), "row": _no_contract_row(context_id="ctx1"),
            "candidates": [{"reasons": ["|delta| 0.08 below 0.15 floor"], "spread_pct_mid": 0.05}],
        }]
        index = candidates_index(records)
        self.assertIn("ctx1", index)
        self.assertEqual(len(index["ctx1"]), 1)

    def test_excludes_no_candidates_in_book(self):
        records = [{
            "signal": _sig(), "row": _no_contract_row(context_id="ctx1", data_quality="no_candidates_in_book"),
            "candidates": [],
        }]
        self.assertEqual(candidates_index(records), {})

    def test_excludes_filled_signals(self):
        records = [{
            "signal": _sig(), "row": {"context_snapshot_id": "ctx1", "net_pnl": 5.0, "exit_reason": "TARGET"},
            "candidates": [],
        }]
        self.assertEqual(candidates_index(records), {})


class WilsonCiTests(unittest.TestCase):
    def test_zero_n_returns_none(self):
        self.assertIsNone(wilson_ci(0, 0))

    def test_point_estimate_is_k_over_n(self):
        result = wilson_ci(27, 40)
        self.assertAlmostEqual(result["point_estimate"], 0.675, places=3)

    def test_all_successes_upper_bound_is_exactly_one(self):
        result = wilson_ci(10, 10)
        self.assertEqual(result["upper"], 1.0)
        self.assertLess(result["lower"], 1.0)

    def test_all_failures_lower_bound_is_exactly_zero(self):
        result = wilson_ci(0, 10)
        self.assertEqual(result["lower"], 0.0)
        self.assertGreater(result["upper"], 0.0)

    def test_interval_widens_with_smaller_n_at_same_proportion(self):
        small = wilson_ci(2, 4)   # 50%, n=4
        large = wilson_ci(20, 40)  # 50%, n=40
        self.assertGreater(small["upper"] - small["lower"], large["upper"] - large["lower"])

    def test_bounds_always_contain_point_estimate(self):
        result = wilson_ci(27, 312)
        self.assertLessEqual(result["lower"], result["point_estimate"])
        self.assertLessEqual(result["point_estimate"], result["upper"])


def _ledger_row(trigger, session, net_pnl):
    return {"heff_smc_trigger": trigger, "session": session, "net_pnl": net_pnl}


class TriggerComparisonWithCiTests(unittest.TestCase):
    def test_separates_by_trigger(self):
        rows = [
            _ledger_row("MSS", "2026-01-05", 10.0),
            _ledger_row("MSS", "2026-01-06", None),
            _ledger_row("SWEEP_RECLAIM", "2026-01-05", -5.0),
        ]
        result = trigger_comparison_with_ci(rows)
        self.assertEqual(set(result.keys()), {"MSS", "SWEEP_RECLAIM"})
        self.assertEqual(result["MSS"]["n_signals"], 2)
        self.assertEqual(result["MSS"]["n_filled"], 1)
        self.assertEqual(result["SWEEP_RECLAIM"]["n_filled"], 1)

    def test_win_rate_and_ci_present_when_filled(self):
        rows = [
            _ledger_row("MSS", "2026-01-05", 10.0),
            _ledger_row("MSS", "2026-01-06", -5.0),
            _ledger_row("MSS", "2026-01-07", 8.0),
        ]
        result = trigger_comparison_with_ci(rows)
        self.assertAlmostEqual(result["MSS"]["win_rate"], 2 / 3, places=4)
        self.assertIsNotNone(result["MSS"]["win_rate_wilson_95ci"])
        self.assertIsNotNone(result["MSS"]["net_expectancy_session_bootstrap_95ci"])

    def test_no_fills_gives_none_stats_not_crash(self):
        rows = [_ledger_row("BOS", "2026-01-05", None)]
        result = trigger_comparison_with_ci(rows)
        self.assertEqual(result["BOS"]["n_filled"], 0)
        self.assertIsNone(result["BOS"]["win_rate"])
        self.assertIsNone(result["BOS"]["net_expectancy_session_bootstrap_95ci"])


if __name__ == "__main__":
    unittest.main()
