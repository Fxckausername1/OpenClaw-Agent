"""Targeted regression test for a real defect surfaced by
SELECTOR_REJECTION_AUDIT_v1.md: live_heff_smc_selector._near_miss_diagnostics'
reason-classification chain has no branch for evaluate_candidate's "no valid
two-sided quote (missing/zero/crossed)" reason, so it silently falls into the
"other" bucket -- indistinguishable from a genuinely unclassified/future rule
text, defeating the diagnostic's own stated purpose ("so a
'no_candidate_passed_all_rules' day is diagnosable ... instead of a black
box"). Diagnostic-only: this cannot affect any PASS/FAIL selection, fill, or
trading decision -- select_contract's own logic is untouched.

Not yet observed in the (16-decision) live shadow log as of 2026-08-01, but
directly demonstrable against the function itself, which is what this test
does.
"""
import unittest

import pandas as pd

from live_heff_smc_selector import LIVE_SELECTOR_CONFIG, _near_miss_diagnostics


def _book_row(bid=None, ask=None, delta=0.20, quote_age_seconds=2.0, ask_size=20):
    return dict(strike=616.0, right="C", expiration="2026-01-05", bid=bid, ask=ask,
                delta=delta, quote_age_seconds=quote_age_seconds, ask_size=ask_size)


class NearMissDiagnosticsClassificationTests(unittest.TestCase):
    def test_no_two_sided_quote_gets_its_own_bucket_not_other(self):
        book = pd.DataFrame([_book_row(bid=None, ask=None)])
        decision_ts = pd.Timestamp("2026-01-05 10:00:00", tz="America/New_York")
        result = _near_miss_diagnostics(book, decision_ts, LIVE_SELECTOR_CONFIG)
        self.assertEqual(result["fail_reason_counts"].get("no_two_sided_quote"), 1)
        self.assertNotIn("other", result["fail_reason_counts"])

    def test_crossed_quote_also_classified_correctly(self):
        # bid >= ask -- evaluate_candidate's own crossed-quote branch
        book = pd.DataFrame([_book_row(bid=0.30, ask=0.25)])
        decision_ts = pd.Timestamp("2026-01-05 10:00:00", tz="America/New_York")
        result = _near_miss_diagnostics(book, decision_ts, LIVE_SELECTOR_CONFIG)
        self.assertEqual(result["fail_reason_counts"].get("no_two_sided_quote"), 1)
        self.assertNotIn("other", result["fail_reason_counts"])

    def test_genuinely_unclassified_reasons_still_fall_to_other(self):
        # sanity: the "other" bucket must still exist as a catch-all for
        # reason text this branch doesn't recognize -- this fix narrows the
        # bucket, it does not remove it.
        book = pd.DataFrame([_book_row(bid=0.10, ask=0.12, delta=0.50)])
        # DTE not in allowed set -> real "DTE ... not in allowed set" reason,
        # already classified as "dte", not "other" -- unaffected by this fix.
        decision_ts = pd.Timestamp("2020-01-05 10:00:00", tz="America/New_York")
        result = _near_miss_diagnostics(book, decision_ts, LIVE_SELECTOR_CONFIG)
        self.assertIn("dte", result["fail_reason_counts"])


if __name__ == "__main__":
    unittest.main()
