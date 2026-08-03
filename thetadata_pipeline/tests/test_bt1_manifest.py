import unittest

from thetadata_pipeline.bt1_manifest import (
    build_session_manifest, core_hour_chunks, grade_session, overall_summary,
    GRADE_FAIL, GRADE_PARTIAL, GRADE_PASS,
)


def clean_inputs(**overrides):
    base = dict(
        received={
            "option_trade_quote_rows": 5000, "underlying_bars_count": 390,
            "underlying_bars_expected": 390, "open_interest_contracts": 40,
            "option_greeks_rows": 800,
        },
        missing={"empty_trade_quote_core_chunks": [], "unrecoverable_errors": []},
        rejected={"duplicate_trade_rows_dropped": 3, "crossed_or_locked_quote_rows": 1},
        integrity={"trade_classification_coverage": 0.8, "ambiguous_trade_fraction": 0.1,
                   "crossed_market_fraction": 0.0002},
    )
    base.update(overrides)
    return base


class CoreHourChunksTests(unittest.TestCase):
    def test_filters_thin_open_and_close_windows(self):
        labels = ["09:30-09:45", "10:00-10:15", "12:00-12:15", "15:45-16:00"]
        self.assertEqual(core_hour_chunks(labels), ["10:00-10:15", "12:00-12:15"])


class GradeSessionTests(unittest.TestCase):
    def test_clean_pull_passes(self):
        grade, reasons = grade_session(**clean_inputs())
        self.assertEqual(grade, GRADE_PASS)
        self.assertEqual(reasons, [])

    def test_zero_trade_rows_fails(self):
        grade, reasons = grade_session(**clean_inputs(received={
            "option_trade_quote_rows": 0, "underlying_bars_count": 390,
            "underlying_bars_expected": 390, "open_interest_contracts": 40,
            "option_greeks_rows": 800,
        }))
        self.assertEqual(grade, GRADE_FAIL)
        self.assertTrue(any("zero option trade" in r for r in reasons))

    def test_zero_bars_fails(self):
        grade, reasons = grade_session(**clean_inputs(received={
            "option_trade_quote_rows": 5000, "underlying_bars_count": 0,
            "underlying_bars_expected": 390, "open_interest_contracts": 40,
            "option_greeks_rows": 800,
        }))
        self.assertEqual(grade, GRADE_FAIL)

    def test_unrecoverable_errors_fail(self):
        grade, reasons = grade_session(**clean_inputs(
            missing={"empty_trade_quote_core_chunks": [], "unrecoverable_errors": ["chunk 11:00 timed out"]}
        ))
        self.assertEqual(grade, GRADE_FAIL)

    def test_grossly_crossed_market_fails(self):
        grade, reasons = grade_session(**clean_inputs(
            integrity={"trade_classification_coverage": 0.8, "ambiguous_trade_fraction": 0.1,
                       "crossed_market_fraction": 0.5}
        ))
        self.assertEqual(grade, GRADE_FAIL)

    def test_thin_trade_volume_only_is_partial_not_fail(self):
        grade, reasons = grade_session(**clean_inputs(received={
            "option_trade_quote_rows": 10, "underlying_bars_count": 390,
            "underlying_bars_expected": 390, "open_interest_contracts": 40,
            "option_greeks_rows": 800,
        }))
        self.assertEqual(grade, GRADE_PARTIAL)

    def test_single_empty_core_chunk_still_passes(self):
        # One missed 15-min window out of ~23 core-hour chunks is within
        # tolerance -- MAX_EMPTY_CORE_CHUNKS_FOR_PASS's whole point.
        grade, reasons = grade_session(**clean_inputs(
            missing={"empty_trade_quote_core_chunks": ["11:00-11:15"], "unrecoverable_errors": []}
        ))
        self.assertEqual(grade, GRADE_PASS)

    def test_few_empty_core_chunks_is_partial(self):
        grade, reasons = grade_session(**clean_inputs(
            missing={"empty_trade_quote_core_chunks": ["11:00-11:15", "13:00-13:15"], "unrecoverable_errors": []}
        ))
        self.assertEqual(grade, GRADE_PARTIAL)

    def test_many_empty_core_chunks_is_fail(self):
        grade, reasons = grade_session(**clean_inputs(
            missing={"empty_trade_quote_core_chunks": ["11:00-11:15", "11:15-11:30", "11:30-11:45",
                                                         "11:45-12:00", "12:00-12:15"],
                     "unrecoverable_errors": []}
        ))
        self.assertEqual(grade, GRADE_FAIL)

    def test_low_coverage_is_partial_then_fail(self):
        partial, _ = grade_session(**clean_inputs(
            integrity={"trade_classification_coverage": 0.30, "ambiguous_trade_fraction": 0.5,
                       "crossed_market_fraction": 0.0002}
        ))
        self.assertEqual(partial, GRADE_PARTIAL)
        fail, _ = grade_session(**clean_inputs(
            integrity={"trade_classification_coverage": 0.10, "ambiguous_trade_fraction": 0.8,
                       "crossed_market_fraction": 0.0002}
        ))
        self.assertEqual(fail, GRADE_FAIL)

    def test_missing_oi_or_greeks_is_partial_not_pass(self):
        grade, reasons = grade_session(**clean_inputs(received={
            "option_trade_quote_rows": 5000, "underlying_bars_count": 390,
            "underlying_bars_expected": 390, "open_interest_contracts": 0,
            "option_greeks_rows": 0,
        }))
        self.assertEqual(grade, GRADE_PARTIAL)
        self.assertEqual(len(reasons), 2)


class ManifestAssemblyTests(unittest.TestCase):
    def test_build_session_manifest_includes_grade(self):
        row = build_session_manifest(
            symbol="SPY", date="2026-07-24",
            calendar={"open": "09:30", "close": "16:00", "is_early_close": False},
            requested={"option_trade_quote_chunks": 26},
            retrieval={"started_at": "t0", "completed_at": "t1"},
            response_metadata={"thetadata_calls": 53, "thetadata_errors": 0},
            **clean_inputs(),
        )
        self.assertEqual(row["quality_grade"], GRADE_PASS)
        self.assertEqual(row["symbol"], "SPY")

    def test_overall_summary_counts_and_usability(self):
        rows = [
            build_session_manifest(symbol="SPY", date=f"2026-07-2{i}",
                                    calendar={}, requested={}, retrieval={}, response_metadata={},
                                    **clean_inputs())
            for i in range(2)
        ]
        rows.append(build_session_manifest(
            symbol="SPY", date="2026-07-26", calendar={}, requested={}, retrieval={}, response_metadata={},
            **clean_inputs(received={
                "option_trade_quote_rows": 0, "underlying_bars_count": 390,
                "underlying_bars_expected": 390, "open_interest_contracts": 40,
                "option_greeks_rows": 800,
            })
        ))
        summary = overall_summary(rows)
        self.assertEqual(summary["sessions_pass"], 2)
        self.assertEqual(summary["sessions_fail"], 1)
        self.assertFalse(summary["usable_for_bt2"])  # any FAIL session blocks BT-2 readiness


if __name__ == "__main__":
    unittest.main()
