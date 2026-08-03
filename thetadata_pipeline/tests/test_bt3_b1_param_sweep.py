"""Tests for bt3_b1_param_sweep.py's pure comparison/report logic
(compare_to_baseline, render_sweep_summary_md) and the variant-list shape
itself. The expensive end-to-end sweep (real replay + real BT-2 simulation
across 62 sessions x N variants) is exercised for real by the overnight run
itself, not re-run here -- same convention as bt3_b0_random_control.py's own
test file, which never re-runs run_b0() end-to-end in tests either."""

from __future__ import annotations

import unittest

from thetadata_pipeline.bt3_b1_param_sweep import (
    BASELINE_ID, PARAM_VARIANTS, compare_to_baseline, render_sweep_summary_md,
)
from thetadata_pipeline.heff_smc_engine import HeffSmcConfig


class ParamVariantsShapeTests(unittest.TestCase):
    def test_baseline_is_present_with_no_overrides(self):
        ids = [v[0] for v in PARAM_VARIANTS]
        self.assertIn(BASELINE_ID, ids)
        baseline = [v for v in PARAM_VARIANTS if v[0] == BASELINE_ID][0]
        self.assertEqual(baseline[2], {})

    def test_variant_ids_are_unique(self):
        ids = [v[0] for v in PARAM_VARIANTS]
        self.assertEqual(len(ids), len(set(ids)))

    def test_every_override_key_is_a_real_config_field(self):
        valid_fields = {f.name for f in __import__("dataclasses").fields(HeffSmcConfig)}
        for variant_id, desc, overrides in PARAM_VARIANTS:
            for key in overrides:
                self.assertIn(key, valid_fields, f"{variant_id} overrides unknown field {key!r}")

    def test_every_override_actually_constructs_a_valid_config(self):
        for variant_id, desc, overrides in PARAM_VARIANTS:
            cfg = HeffSmcConfig(**overrides)
            self.assertIsInstance(cfg, HeffSmcConfig)

    def test_every_override_differs_from_baseline_default(self):
        """A variant whose override happens to equal the live default isn't
        testing anything -- catches a copy-paste value bug."""
        baseline_cfg = HeffSmcConfig()
        for variant_id, desc, overrides in PARAM_VARIANTS:
            if variant_id == BASELINE_ID:
                continue
            for key, val in overrides.items():
                self.assertNotEqual(
                    getattr(baseline_cfg, key), val,
                    f"{variant_id} overrides {key} to the same value as the live default",
                )


def _summary(net_exp, ci_lower=None, ci_upper=None, n_filled=10):
    ci = {"lower": ci_lower, "upper": ci_upper, "point_estimate": net_exp, "n_sessions": 5} if ci_lower is not None else None
    return {"net_expectancy_per_filled_trade": net_exp, "net_expectancy_session_bootstrap_95ci": ci, "n_filled": n_filled}


class CompareToBaselineTests(unittest.TestCase):
    def test_worse_point_estimate_is_not_better(self):
        results = {
            BASELINE_ID: {"description": "base", "overrides": {}, "summary": _summary(10.0)},
            "v1": {"description": "v1", "overrides": {}, "summary": _summary(5.0, ci_lower=-2.0, ci_upper=12.0)},
        }
        comps = compare_to_baseline(results)
        self.assertTrue(comps["v1"]["verdict"].startswith("not better"))

    def test_better_point_but_overlapping_ci_is_within_noise(self):
        results = {
            BASELINE_ID: {"description": "base", "overrides": {}, "summary": _summary(10.0)},
            "v1": {"description": "v1", "overrides": {}, "summary": _summary(15.0, ci_lower=5.0, ci_upper=25.0)},
        }
        comps = compare_to_baseline(results)
        self.assertTrue(comps["v1"]["verdict"].startswith("within noise"))

    def test_ci_lower_bound_clearing_baseline_is_plausibly_better(self):
        results = {
            BASELINE_ID: {"description": "base", "overrides": {}, "summary": _summary(10.0)},
            "v1": {"description": "v1", "overrides": {}, "summary": _summary(20.0, ci_lower=12.0, ci_upper=28.0)},
        }
        comps = compare_to_baseline(results)
        self.assertTrue(comps["v1"]["verdict"].startswith("plausibly better"))

    def test_missing_data_reported_honestly(self):
        results = {
            BASELINE_ID: {"description": "base", "overrides": {}, "summary": _summary(10.0)},
            "v1": {"description": "v1", "overrides": {}, "summary": {"net_expectancy_per_filled_trade": None, "net_expectancy_session_bootstrap_95ci": None, "n_filled": 0}},
        }
        comps = compare_to_baseline(results)
        self.assertEqual(comps["v1"]["verdict"], "insufficient_data")

    def test_baseline_itself_excluded_from_comparisons(self):
        results = {BASELINE_ID: {"description": "base", "overrides": {}, "summary": _summary(10.0)}}
        comps = compare_to_baseline(results)
        self.assertNotIn(BASELINE_ID, comps)


class RenderSweepSummaryTests(unittest.TestCase):
    def test_renders_without_error_and_reports_honest_read(self):
        results = {
            BASELINE_ID: {"description": "base", "overrides": {}, "summary": _summary(10.0)},
            "v1": {"description": "v1 desc", "overrides": {"piv_len": 3}, "summary": _summary(5.0, ci_lower=-2.0, ci_upper=12.0)},
        }
        comps = compare_to_baseline(results)
        text = render_sweep_summary_md(results, comps)
        self.assertIn("Baseline", text)
        self.assertIn("Honest read", text)
        self.assertIn("No variant's improvement clears", text)

    def test_plausibly_better_variant_is_called_out_not_overstated(self):
        results = {
            BASELINE_ID: {"description": "base", "overrides": {}, "summary": _summary(10.0)},
            "v1": {"description": "v1 desc", "overrides": {"piv_len": 3}, "summary": _summary(20.0, ci_lower=12.0, ci_upper=28.0)},
        }
        comps = compare_to_baseline(results)
        text = render_sweep_summary_md(results, comps)
        self.assertIn("v1", text)
        self.assertIn("treat as a lead worth re-testing", text)


if __name__ == "__main__":
    unittest.main()
