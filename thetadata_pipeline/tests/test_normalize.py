import unittest

import pandas as pd

from thetadata_pipeline.normalize import classify_trades, coverage_stats, dedup_trades
from thetadata_pipeline.schemas import CLASS_ASK, CLASS_BID, CLASS_COMPLEX, CLASS_MID, CLASS_OUTSIDE, CLASS_STALE


def _row(seq, price, bid, ask, condition=18, trade_ts="2026-07-24 10:00:00", quote_ts="2026-07-24 10:00:00"):
    return {
        "symbol": "SPY", "expiration": "2026-07-24", "strike": 750.0, "right": "CALL",
        "trade_timestamp": pd.Timestamp(trade_ts), "quote_timestamp": pd.Timestamp(quote_ts),
        "sequence": seq, "condition": condition, "size": 1, "exchange": 1,
        "price": price, "bid_size": 10, "bid_exchange": 1, "bid": bid, "bid_condition": 0,
        "ask_size": 10, "ask_exchange": 1, "ask": ask, "ask_condition": 0,
    }


class ClassifyTradesTests(unittest.TestCase):
    def test_at_ask_is_buy(self):
        df = pd.DataFrame([_row(1, price=1.05, bid=1.00, ask=1.05)])
        out = classify_trades(df)
        self.assertEqual(out.iloc[0]["classification"], CLASS_ASK)
        self.assertEqual(out.iloc[0]["classification_confidence"], 1.0)

    def test_at_bid_is_sell(self):
        df = pd.DataFrame([_row(1, price=1.00, bid=1.00, ask=1.05)])
        out = classify_trades(df)
        self.assertEqual(out.iloc[0]["classification"], CLASS_BID)

    def test_at_midpoint_is_ambiguous(self):
        df = pd.DataFrame([_row(1, price=1.025, bid=1.00, ask=1.05)])
        out = classify_trades(df)
        self.assertEqual(out.iloc[0]["classification"], CLASS_MID)
        self.assertEqual(out.iloc[0]["classification_confidence"], 0.0)

    def test_complex_condition_overrides_price_position(self):
        # price sits exactly at the ask, but condition 130 (complex order
        # book) must still win -- guessing a direction here would misattribute
        # multi-leg flow as a simple customer buy.
        df = pd.DataFrame([_row(1, price=1.05, bid=1.00, ask=1.05, condition=130)])
        out = classify_trades(df)
        self.assertEqual(out.iloc[0]["classification"], CLASS_COMPLEX)
        self.assertEqual(out.iloc[0]["excluded_reason"], "non_simple_trade_condition")

    def test_iso_condition_95_is_still_simple(self):
        df = pd.DataFrame([_row(1, price=1.05, bid=1.00, ask=1.05, condition=95)])
        out = classify_trades(df)
        self.assertEqual(out.iloc[0]["classification"], CLASS_ASK)

    def test_stale_quote_excluded(self):
        df = pd.DataFrame([_row(
            1, price=1.05, bid=1.00, ask=1.05,
            trade_ts="2026-07-24 10:00:05", quote_ts="2026-07-24 10:00:00",
        )])
        out = classify_trades(df)
        self.assertEqual(out.iloc[0]["classification"], CLASS_STALE)

    def test_trade_outside_nbbo(self):
        df = pd.DataFrame([_row(1, price=1.20, bid=1.00, ask=1.05)])
        out = classify_trades(df)
        self.assertEqual(out.iloc[0]["classification"], CLASS_OUTSIDE)
        self.assertEqual(out.iloc[0]["excluded_reason"], "trade_outside_nbbo")

    def test_invalid_quote(self):
        df = pd.DataFrame([_row(1, price=1.05, bid=0.0, ask=1.05)])
        out = classify_trades(df)
        self.assertEqual(out.iloc[0]["classification"], CLASS_OUTSIDE)
        self.assertEqual(out.iloc[0]["excluded_reason"], "invalid_quote")

    def test_crossed_market(self):
        df = pd.DataFrame([_row(1, price=1.05, bid=1.10, ask=1.05)])
        out = classify_trades(df)
        self.assertEqual(out.iloc[0]["classification"], CLASS_OUTSIDE)
        self.assertEqual(out.iloc[0]["excluded_reason"], "crossed_or_locked_market")

    def test_empty_input(self):
        out = classify_trades(pd.DataFrame())
        self.assertTrue(out.empty)

    def test_contract_id_present(self):
        df = pd.DataFrame([_row(1, price=1.05, bid=1.00, ask=1.05)])
        out = classify_trades(df)
        self.assertEqual(out.iloc[0]["contract_id"], "SPY_20260724_000750000_C")

    def test_underlying_and_right_are_normalized(self):
        # Regression test: ThetaData's real raw response names the ticker
        # column `symbol` (not `underlying`) and `right` as the full word
        # "CALL"/"PUT" -- a real KeyError against live backfill data
        # ('underlying' not in index) surfaced this before it was fixed at
        # the add_contract_id() normalization point.
        df = pd.DataFrame([_row(1, price=1.05, bid=1.00, ask=1.05)])
        out = classify_trades(df)
        self.assertEqual(out.iloc[0]["underlying"], "SPY")
        self.assertEqual(out.iloc[0]["right"], "C")


class DedupTests(unittest.TestCase):
    def test_dedup_on_contract_and_sequence(self):
        df = pd.DataFrame([_row(1, 1.05, 1.00, 1.05), _row(1, 1.05, 1.00, 1.05), _row(2, 1.05, 1.00, 1.05)])
        classified = classify_trades(df)
        deduped = dedup_trades(classified)
        self.assertEqual(len(deduped), 2)

    def test_dedup_on_raw_unclassified_rows(self):
        # Regression test: a real bug (caught via a live backfill run against
        # real ThetaData, not a mock) had dedup_trades()/append_raw() called
        # on RAW rows straight from the endpoint, before contract_id existed
        # -- KeyError on drop_duplicates(subset=["contract_id", ...]).
        raw = pd.DataFrame([_row(1, 1.05, 1.00, 1.05), _row(1, 1.05, 1.00, 1.05), _row(2, 1.05, 1.00, 1.05)])
        self.assertNotIn("contract_id", raw.columns)
        deduped = dedup_trades(raw)
        self.assertEqual(len(deduped), 2)
        self.assertIn("contract_id", deduped.columns)

    def test_dedup_after_concat_of_classified_and_raw(self):
        # Reproduces append_raw()'s real concat pattern: an "existing"
        # partition that already has contract_id (a prior cycle's classified
        # output) concatenated with fresh raw rows that don't yet. Column
        # union must not silently leave the raw side's contract_id as NaN.
        existing = classify_trades(pd.DataFrame([_row(1, 1.05, 1.00, 1.05)]))
        new_raw = pd.DataFrame([_row(2, 1.05, 1.00, 1.05)])
        combined = pd.concat([existing, new_raw], ignore_index=True)
        self.assertTrue(combined["contract_id"].isna().any())  # confirms the union-NaN setup is real
        deduped = dedup_trades(combined)
        self.assertFalse(deduped["contract_id"].isna().any())
        self.assertEqual(len(deduped), 2)


class CoverageStatsTests(unittest.TestCase):
    def test_coverage_and_ambiguity(self):
        df = pd.DataFrame([
            _row(1, price=1.05, bid=1.00, ask=1.05),  # ASK
            _row(2, price=1.00, bid=1.00, ask=1.05),  # BID
            _row(3, price=1.025, bid=1.00, ask=1.05),  # MID
            _row(4, price=1.025, bid=1.00, ask=1.05),  # MID
        ])
        classified = classify_trades(df)
        stats = coverage_stats(classified)
        self.assertAlmostEqual(stats["trade_classification_coverage"], 0.5)
        self.assertAlmostEqual(stats["ambiguous_trade_fraction"], 0.5)

    def test_empty(self):
        stats = coverage_stats(pd.DataFrame())
        self.assertIsNone(stats["trade_classification_coverage"])


if __name__ == "__main__":
    unittest.main()
