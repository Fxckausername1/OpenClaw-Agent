import datetime as dt
import unittest

import pandas as pd

from thetadata_pipeline.bt2_selector import (
    NO_CONTRACT_REASON_NO_CANDIDATES, NO_CONTRACT_REASON_NONE_PASS, SelectorConfig,
    build_point_in_time_book, evaluate_candidate, select_cheapest_passing_contract, select_contract,
)

DECISION_TS = pd.Timestamp("2026-07-24 14:00:00", tz="UTC")


def _trade_row(contract_id, expiration, strike, right, ts, bid, ask, bid_size=20, ask_size=20):
    return dict(
        contract_id=contract_id, expiration=expiration, strike=strike, right=right,
        trade_timestamp=ts, bid=bid, ask=ask, bid_size=bid_size, ask_size=ask_size,
    )


class BuildPointInTimeBookTests(unittest.TestCase):
    def test_only_uses_rows_at_or_before_decision_ts(self):
        trades = pd.DataFrame([
            _trade_row("SPY_20260724_745000_C", "2026-07-24", 745.0, "C",
                       DECISION_TS - pd.Timedelta(minutes=5), 0.25, 0.27),
            _trade_row("SPY_20260724_745000_C", "2026-07-24", 745.0, "C",
                       DECISION_TS + pd.Timedelta(minutes=5), 0.50, 0.52),  # future -- must never be used
        ])
        book = build_point_in_time_book(trades, DECISION_TS)
        self.assertEqual(len(book), 1)
        self.assertEqual(float(book.iloc[0]["ask"]), 0.27)

    def test_takes_most_recent_row_per_contract(self):
        trades = pd.DataFrame([
            _trade_row("SPY_20260724_745000_C", "2026-07-24", 745.0, "C",
                       DECISION_TS - pd.Timedelta(minutes=5), 0.25, 0.27),
            _trade_row("SPY_20260724_745000_C", "2026-07-24", 745.0, "C",
                       DECISION_TS - pd.Timedelta(minutes=1), 0.28, 0.30),
        ])
        book = build_point_in_time_book(trades, DECISION_TS)
        self.assertEqual(len(book), 1)
        self.assertEqual(float(book.iloc[0]["ask"]), 0.30)

    def test_quote_age_computed_against_decision_ts(self):
        trades = pd.DataFrame([
            _trade_row("SPY_20260724_745000_C", "2026-07-24", 745.0, "C",
                       DECISION_TS - pd.Timedelta(seconds=7), 0.25, 0.27),
        ])
        book = build_point_in_time_book(trades, DECISION_TS)
        self.assertAlmostEqual(float(book.iloc[0]["quote_age_seconds"]), 7.0, places=1)

    def test_empty_trades_returns_empty_book(self):
        self.assertTrue(build_point_in_time_book(pd.DataFrame(), DECISION_TS).empty)

    def test_greeks_merged_by_contract_id(self):
        trades = pd.DataFrame([
            _trade_row("SPY_20260724_745000_C", "2026-07-24", 745.0, "C",
                       DECISION_TS - pd.Timedelta(minutes=1), 0.25, 0.27),
        ])
        book = build_point_in_time_book(trades, DECISION_TS, greeks={"SPY_20260724_745000_C": {"delta": 0.33}})
        self.assertEqual(float(book.iloc[0]["delta"]), 0.33)

    def test_intraday_spot_and_midpoint_override_conflicting_eod_greek(self):
        cid = "SPY_20260724_745000_C"
        trades = pd.DataFrame([
            _trade_row(
                cid, "2026-07-24", 745.0, "C",
                DECISION_TS - pd.Timedelta(minutes=1), 0.25, 0.27,
            ),
        ])
        book = build_point_in_time_book(
            trades, DECISION_TS,
            greeks={cid: {"delta": 0.99}},
            underlying_price=745.0,
        )
        delta = float(book.iloc[0]["delta"])
        self.assertNotAlmostEqual(delta, 0.99)
        self.assertGreater(delta, 0.0)
        self.assertLess(delta, 1.0)

    def test_invalid_intraday_spot_does_not_fall_back_to_eod_greek(self):
        trades = pd.DataFrame([
            _trade_row("SPY_20260724_745000_C", "2026-07-24", 745.0, "C",
                       DECISION_TS - pd.Timedelta(minutes=1), 0.25, 0.27),
        ])
        book = build_point_in_time_book(trades, DECISION_TS, greeks={"SPY_20260724_745000_C": {"delta": 0.99}}, underlying_price=0)
        self.assertIsNone(book.iloc[0]["delta"])


class EvaluateCandidateTests(unittest.TestCase):
    def _row(self, **overrides):
        row = dict(
            strike=745.0, right="C", expiration=dt.date(2026, 7, 24),
            bid=0.24, ask=0.26, bid_size=20, ask_size=20,
            quote_age_seconds=2.0, delta=0.35,
        )
        row.update(overrides)
        return row

    def test_clean_candidate_passes(self):
        out = evaluate_candidate(self._row(), dt.date(2026, 7, 24), SelectorConfig())
        self.assertTrue(out["passed"])
        self.assertEqual(out["reasons"], [])

    def test_dte_outside_allowed_set_fails(self):
        out = evaluate_candidate(self._row(expiration=dt.date(2026, 7, 30)), dt.date(2026, 7, 24), SelectorConfig())
        self.assertFalse(out["passed"])
        self.assertTrue(any("DTE" in r for r in out["reasons"]))

    def test_premium_outside_band_fails_hard(self):
        # Unlike contract_quote.py's live screen (premium is preference
        # only), BT-2's selector treats the premium band as a real gate --
        # Section 6 lists it among the pass/fail rules and Section 16
        # requires NO TRADE (not walking further OTM) when nothing is
        # in-band.
        out = evaluate_candidate(self._row(bid=0.03, ask=0.04), dt.date(2026, 7, 24), SelectorConfig())
        self.assertFalse(out["passed"])
        self.assertTrue(any("premium band" in r for r in out["reasons"]))

    def test_wide_spread_fails(self):
        out = evaluate_candidate(self._row(bid=0.20, ask=0.28), dt.date(2026, 7, 24), SelectorConfig())
        self.assertFalse(out["passed"])
        self.assertTrue(any("spread" in r for r in out["reasons"]))

    def test_low_delta_fails(self):
        out = evaluate_candidate(self._row(delta=0.05), dt.date(2026, 7, 24), SelectorConfig())
        self.assertFalse(out["passed"])
        self.assertTrue(any("delta" in r for r in out["reasons"]))

    def test_missing_delta_fails(self):
        out = evaluate_candidate(self._row(delta=None), dt.date(2026, 7, 24), SelectorConfig())
        self.assertFalse(out["passed"])
        self.assertTrue(any("delta unavailable" in r for r in out["reasons"]))

    def test_stale_quote_fails(self):
        out = evaluate_candidate(self._row(quote_age_seconds=99.0), dt.date(2026, 7, 24), SelectorConfig())
        self.assertFalse(out["passed"])
        self.assertTrue(any("quote age" in r for r in out["reasons"]))

    def test_thin_displayed_size_fails(self):
        out = evaluate_candidate(self._row(ask_size=1), dt.date(2026, 7, 24), SelectorConfig())
        self.assertFalse(out["passed"])
        self.assertTrue(any("liquidity floor" in r for r in out["reasons"]))

    def test_crossed_quote_fails(self):
        out = evaluate_candidate(self._row(bid=0.30, ask=0.28), dt.date(2026, 7, 24), SelectorConfig())
        self.assertFalse(out["passed"])
        self.assertEqual(out["reasons"], ["no valid two-sided quote (missing/zero/crossed)"])

    def test_zero_bid_fails(self):
        out = evaluate_candidate(self._row(bid=0.0, ask=0.26), dt.date(2026, 7, 24), SelectorConfig())
        self.assertFalse(out["passed"])


class SelectContractTests(unittest.TestCase):
    def _book(self):
        return pd.DataFrame([
            # Identical spread_pct_mid (8%) on both -- isolates the
            # documented tie-break (closest to target_delta=0.35) from the
            # primary quality ranking (lowest spread_pct_mid), which would
            # otherwise decide it first.
            {"contract_id": "SPY_20260724_744000_C", "strike": 744.0, "right": "C", "expiration": "2026-07-24",
             "bid": 0.24, "ask": 0.26, "bid_size": 20, "ask_size": 20, "quote_age_seconds": 2.0, "delta": 0.45},
            {"contract_id": "SPY_20260724_745000_C", "strike": 745.0, "right": "C", "expiration": "2026-07-24",
             "bid": 0.24, "ask": 0.26, "bid_size": 20, "ask_size": 20, "quote_age_seconds": 2.0, "delta": 0.35},
            {"contract_id": "SPY_20260724_746000_C", "strike": 746.0, "right": "C", "expiration": "2026-07-24",
             "bid": 0.60, "ask": 0.90, "bid_size": 20, "ask_size": 20, "quote_age_seconds": 2.0, "delta": 0.55},  # outside premium band -> fails
        ])

    def test_selects_closest_to_target_delta_among_passing(self):
        # config target_delta=0.35 -- the 745 strike (delta 0.35 exactly)
        # should beat the 744 strike (delta 0.45) purely on the documented
        # tie-break, since both share an identical spread_pct_mid.
        result = select_contract(self._book(), "C", DECISION_TS, SelectorConfig())
        self.assertTrue(result.found)
        self.assertEqual(result.contract["strike"], 745.0)

    def test_never_falls_back_to_a_failing_candidate(self):
        book = pd.DataFrame([
            {"contract_id": "SPY_20260724_746000_C", "strike": 746.0, "right": "C", "expiration": "2026-07-24",
             "bid": 0.60, "ask": 0.90, "bid_size": 20, "ask_size": 20, "quote_age_seconds": 2.0, "delta": 0.55},
        ])
        result = select_contract(book, "C", DECISION_TS, SelectorConfig())
        self.assertFalse(result.found)
        self.assertEqual(result.reason, NO_CONTRACT_REASON_NONE_PASS)

    def test_no_candidates_for_right_returns_no_contract(self):
        result = select_contract(self._book(), "P", DECISION_TS, SelectorConfig())
        self.assertFalse(result.found)
        self.assertEqual(result.reason, NO_CONTRACT_REASON_NO_CANDIDATES)

    def test_empty_book_returns_no_contract(self):
        result = select_contract(pd.DataFrame(), "C", DECISION_TS, SelectorConfig())
        self.assertFalse(result.found)
        self.assertEqual(result.reason, NO_CONTRACT_REASON_NO_CANDIDATES)

    def test_no_preferred_premium_contract_does_not_walk_further_otm(self):
        # Section 16: "No preferred-premium contract exists -> Return NO
        # TRADE; do not walk further OTM to force a fill." Every candidate
        # here is priced outside the premium band.
        book = pd.DataFrame([
            {"contract_id": "SPY_20260724_760000_C", "strike": 760.0, "right": "C", "expiration": "2026-07-24",
             "bid": 0.02, "ask": 0.03, "bid_size": 20, "ask_size": 20, "quote_age_seconds": 2.0, "delta": 0.05},
            {"contract_id": "SPY_20260724_700000_C", "strike": 700.0, "right": "C", "expiration": "2026-07-24",
             "bid": 5.00, "ask": 5.10, "bid_size": 20, "ask_size": 20, "quote_age_seconds": 2.0, "delta": 0.95},
        ])
        result = select_contract(book, "C", DECISION_TS, SelectorConfig())
        self.assertFalse(result.found)
        self.assertEqual(result.reason, NO_CONTRACT_REASON_NONE_PASS)


class SelectCheapestPassingContractTests(unittest.TestCase):
    def test_cheapest_can_diverge_from_quality_pick(self):
        # roadmap Section 16 adversarial test: force the selector to pick
        # the cheapest available contract and compare against the real
        # (quality-ranked) choice. 744 has the lowest ask but a much wider
        # spread_pct_mid and a worse delta fit; 745 has a tighter spread
        # and delta==target_delta exactly.
        book = pd.DataFrame([
            {"contract_id": "SPY_20260724_744000_C", "strike": 744.0, "right": "C", "expiration": "2026-07-24",
             "bid": 0.18, "ask": 0.20, "bid_size": 20, "ask_size": 20, "quote_age_seconds": 2.0, "delta": 0.20},
            {"contract_id": "SPY_20260724_745000_C", "strike": 745.0, "right": "C", "expiration": "2026-07-24",
             "bid": 0.27, "ask": 0.28, "bid_size": 20, "ask_size": 20, "quote_age_seconds": 2.0, "delta": 0.35},
        ])
        quality_pick = select_contract(book, "C", DECISION_TS, SelectorConfig())
        cheapest_pick = select_cheapest_passing_contract(book, "C", DECISION_TS, SelectorConfig())
        self.assertEqual(quality_pick.contract["strike"], 745.0)   # tighter spread_pct_mid AND exact delta match
        self.assertEqual(cheapest_pick.contract["strike"], 744.0)  # lowest ask, worse spread and delta fit
        # The adversarial comparison itself: the cheapest pick's spread_pct
        # is materially worse than the quality pick's -- exactly the
        # "simulated damage" Section 16 wants quantified.
        self.assertGreater(cheapest_pick.contract["spread_pct_mid"], quality_pick.contract["spread_pct_mid"])


if __name__ == "__main__":
    unittest.main()
