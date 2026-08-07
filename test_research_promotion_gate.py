#!/usr/bin/env python3
import unittest

from research_promotion_gate import evaluate


def analysis(verdict="positive_confirmed", days=300, avg=0.1, low=0.02):
    return {
        "strategies": {
            "MR": {
                "all": {
                    "verdict": verdict,
                    "unique_days": days,
                    "net_6bp_avg_r": avg,
                    "fill_rate": 0.8,
                    "daily_block_bootstrap_95pct_mean_r": {"low": low, "high": 0.2},
                }
            }
        }
    }


class GateTests(unittest.TestCase):
    def test_clean_positive_evidence_is_eligible(self):
        result = evaluate("MR", analysis(), {"live_backtest_contracts": {"failed": 0}})
        self.assertEqual(result["decision"], "ELIGIBLE_FOR_FORWARD_PAPER_REVIEW")

    def test_contract_and_negative_evidence_block(self):
        result = evaluate(
            "MR",
            analysis(verdict="negative_confirmed", avg=-0.1, low=-0.2),
            {"live_backtest_contracts": {"failed": 4}},
        )
        self.assertEqual(result["decision"], "BLOCKED")
        self.assertGreaterEqual(len(result["reasons"]), 4)


if __name__ == "__main__":
    unittest.main()
