import unittest

import pandas as pd

from thetadata_pipeline.bt2_fills import (
    FILL_MODEL_BASE_REALISTIC, FILL_MODEL_MIDPOINT, FillConfig,
    STATUS_FILLED, STATUS_FILLED_WORTHLESS, STATUS_MISSED_HALTED,
    STATUS_MISSED_ENTRY_TTL, STATUS_MISSED_LOCKED_CROSSED, STATUS_MISSED_NO_QUOTE, STATUS_MISSED_ONE_SIDED,
    STATUS_MISSED_ZERO_SIZE, effective_spread_paid, eligible_quotes,
    friction_metrics, simulate_entry_fill, simulate_exit_fill,
)

DECISION_TS = pd.Timestamp("2026-07-24 14:00:00", tz="UTC")


def _quote(offset_seconds, bid, ask, bid_size=20, ask_size=20):
    return {"quote_ts": DECISION_TS + pd.Timedelta(seconds=offset_seconds),
            "bid": bid, "ask": ask, "bid_size": bid_size, "ask_size": ask_size}


class EligibleQuotesTests(unittest.TestCase):
    def test_drops_quotes_before_reaction_latency_floor(self):
        quotes = pd.DataFrame([_quote(0, 0.24, 0.26), _quote(5, 0.24, 0.26)])
        usable = eligible_quotes(quotes, DECISION_TS, FillConfig(reaction_latency_seconds=3.0))
        self.assertEqual(len(usable), 1)
        self.assertEqual(usable.iloc[0]["quote_ts"], DECISION_TS + pd.Timedelta(seconds=5))

    def test_drops_quotes_inside_halt_window(self):
        halt_start = DECISION_TS + pd.Timedelta(seconds=10)
        halt_end = DECISION_TS + pd.Timedelta(seconds=60)
        quotes = pd.DataFrame([_quote(20, 0.24, 0.26), _quote(90, 0.25, 0.27)])
        config = FillConfig(reaction_latency_seconds=3.0, halted_windows=((halt_start, halt_end),))
        usable = eligible_quotes(quotes, DECISION_TS, config)
        self.assertEqual(len(usable), 1)
        self.assertEqual(usable.iloc[0]["quote_ts"], DECISION_TS + pd.Timedelta(seconds=90))


class SimulateEntryFillTests(unittest.TestCase):
    def test_base_realistic_fills_at_ask(self):
        quotes = pd.DataFrame([_quote(5, 0.24, 0.26)])
        result = simulate_entry_fill(quotes, DECISION_TS, 1, FillConfig(fill_model=FILL_MODEL_BASE_REALISTIC))
        self.assertEqual(result.status, STATUS_FILLED)
        self.assertEqual(result.fill_price, 0.26)
        self.assertEqual(result.contemporaneous_mid, 0.25)

    def test_midpoint_fills_at_mid(self):
        quotes = pd.DataFrame([_quote(5, 0.24, 0.26)])
        result = simulate_entry_fill(quotes, DECISION_TS, 1, FillConfig(fill_model=FILL_MODEL_MIDPOINT))
        self.assertEqual(result.status, STATUS_FILLED)
        self.assertEqual(result.fill_price, 0.25)

    def test_caps_quantity_by_displayed_ask_size(self):
        quotes = pd.DataFrame([_quote(5, 0.24, 0.26, ask_size=2)])
        result = simulate_entry_fill(quotes, DECISION_TS, 10, FillConfig())
        self.assertEqual(result.quantity, 2)

    def test_zero_ask_size_is_missed(self):
        quotes = pd.DataFrame([_quote(5, 0.24, 0.26, ask_size=0)])
        result = simulate_entry_fill(quotes, DECISION_TS, 1, FillConfig())
        self.assertEqual(result.status, STATUS_MISSED_ZERO_SIZE)
        self.assertFalse(result.filled)

    def test_walks_past_a_locked_or_crossed_quote(self):
        quotes = pd.DataFrame([_quote(5, 0.30, 0.28), _quote(10, 0.24, 0.26)])  # first is crossed
        result = simulate_entry_fill(quotes, DECISION_TS, 1, FillConfig())
        self.assertEqual(result.status, STATUS_FILLED)
        self.assertEqual(result.fill_price, 0.26)

    def test_walks_past_a_one_sided_quote(self):
        quotes = pd.DataFrame([_quote(5, None, None), _quote(10, 0.24, 0.26)])
        result = simulate_entry_fill(quotes, DECISION_TS, 1, FillConfig())
        self.assertEqual(result.status, STATUS_FILLED)

    def test_quote_after_default_entry_ttl_is_not_filled(self):
        quotes = pd.DataFrame([_quote(24, 0.24, 0.26)])
        result = simulate_entry_fill(quotes, DECISION_TS, 1, FillConfig())
        self.assertEqual(result.status, STATUS_MISSED_ENTRY_TTL)
        self.assertFalse(result.filled)

    def test_no_quotes_at_all_is_missed(self):
        result = simulate_entry_fill(pd.DataFrame(), DECISION_TS, 1, FillConfig())
        self.assertEqual(result.status, STATUS_MISSED_NO_QUOTE)

    def test_only_crossed_quotes_is_missed_locked_crossed(self):
        quotes = pd.DataFrame([_quote(5, 0.30, 0.28)])
        result = simulate_entry_fill(quotes, DECISION_TS, 1, FillConfig())
        self.assertEqual(result.status, STATUS_MISSED_LOCKED_CROSSED)

    def test_only_one_sided_quotes_is_missed_one_sided(self):
        quotes = pd.DataFrame([_quote(5, None, None)])
        result = simulate_entry_fill(quotes, DECISION_TS, 1, FillConfig())
        self.assertEqual(result.status, STATUS_MISSED_ONE_SIDED)

    def test_gap_flagged_when_fill_is_far_past_latency_floor(self):
        # Real BT-1 data density means the next tick for a given contract
        # can be minutes away -- that alone must not block the fill
        # (Section 16: "use next valid executable quote"), but it should be
        # flagged so the gap is never silently absorbed.
        quotes = pd.DataFrame([_quote(200, 0.24, 0.26)])  # 200s after decision, well past the 3s latency floor
        result = simulate_entry_fill(quotes, DECISION_TS, 1, FillConfig(entry_ttl_seconds=300.0, gap_flag_seconds=60.0))
        self.assertEqual(result.status, STATUS_FILLED)
        self.assertTrue(result.gap_flagged)
        self.assertGreater(result.gap_seconds, 60.0)

    def test_no_gap_flag_for_a_prompt_fill(self):
        quotes = pd.DataFrame([_quote(4, 0.24, 0.26)])
        result = simulate_entry_fill(quotes, DECISION_TS, 1, FillConfig(gap_flag_seconds=60.0))
        self.assertFalse(result.gap_flagged)


class SimulateExitFillTests(unittest.TestCase):
    def test_base_realistic_fills_at_bid(self):
        quotes = pd.DataFrame([_quote(5, 0.40, 0.42)])
        result = simulate_exit_fill(quotes, DECISION_TS, 1, FillConfig())
        self.assertEqual(result.status, STATUS_FILLED)
        self.assertEqual(result.fill_price, 0.40)

    def test_zero_bid_is_a_real_worthless_fill_not_midpoint(self):
        # Section 16: "Bid becomes zero -- do NOT value at midpoint; treat
        # as realistic liquidation (can't sell into a zero bid)."
        quotes = pd.DataFrame([_quote(5, 0.0, 0.05)])
        result = simulate_exit_fill(quotes, DECISION_TS, 3, FillConfig())
        self.assertEqual(result.status, STATUS_FILLED_WORTHLESS)
        self.assertTrue(result.filled)  # a real outcome, not a missed trade
        self.assertEqual(result.fill_price, 0.0)
        self.assertEqual(result.quantity, 3)

    def test_zero_bid_uses_full_requested_quantity_not_bid_size(self):
        quotes = pd.DataFrame([_quote(5, 0.0, 0.05, bid_size=0)])
        result = simulate_exit_fill(quotes, DECISION_TS, 2, FillConfig())
        self.assertEqual(result.status, STATUS_FILLED_WORTHLESS)
        self.assertEqual(result.quantity, 2)

    def test_zero_displayed_bid_size_with_nonzero_bid_is_missed(self):
        quotes = pd.DataFrame([_quote(5, 0.40, 0.42, bid_size=0)])
        result = simulate_exit_fill(quotes, DECISION_TS, 1, FillConfig())
        self.assertEqual(result.status, STATUS_MISSED_ZERO_SIZE)
        self.assertFalse(result.filled)


class TradingHaltTests(unittest.TestCase):
    def test_fills_freeze_during_halt_and_resume_after(self):
        halt_start = DECISION_TS + pd.Timedelta(seconds=10)
        halt_end = DECISION_TS + pd.Timedelta(seconds=120)
        config = FillConfig(entry_ttl_seconds=200.0, halted_windows=((halt_start, halt_end),))
        quotes = pd.DataFrame([
            _quote(20, 0.24, 0.26),   # inside the halt -- must be skipped
            _quote(150, 0.25, 0.27),  # after the halt lifts -- usable
        ])
        result = simulate_entry_fill(quotes, DECISION_TS, 1, config)
        self.assertEqual(result.status, STATUS_FILLED)
        self.assertEqual(result.fill_price, 0.27)

    def test_no_quote_after_halt_lifts_is_missed_halted(self):
        halt_start = DECISION_TS + pd.Timedelta(seconds=10)
        halt_end = DECISION_TS + pd.Timedelta(seconds=120)
        config = FillConfig(halted_windows=((halt_start, halt_end),))
        quotes = pd.DataFrame([_quote(20, 0.24, 0.26)])  # only quote, and it's inside the halt
        result = simulate_entry_fill(quotes, DECISION_TS, 1, config)
        self.assertEqual(result.status, STATUS_MISSED_HALTED)
        self.assertFalse(result.filled)


class FrictionMetricsTests(unittest.TestCase):
    def test_effective_spread_paid_formula(self):
        self.assertEqual(effective_spread_paid(0.26, 0.25), 0.02)

    def test_friction_metrics_hand_checked(self):
        quotes_entry = pd.DataFrame([_quote(5, 0.24, 0.26)])
        quotes_exit = pd.DataFrame([_quote(5, 0.40, 0.42)])
        entry = simulate_entry_fill(quotes_entry, DECISION_TS, 1, FillConfig(fee_per_contract=0.05))
        exit_ = simulate_exit_fill(quotes_exit, DECISION_TS, 1, FillConfig(fee_per_contract=0.05))
        # UNITS MATTER -- this test previously asserted the buggy mixed-unit sum
        # (round_trip_friction = 0.02 + 0.02 + 0.10 = 0.14), adding PREMIUM-unit
        # slippage to DOLLAR fees. Corrected 2026-07-31:
        #
        #   Section 7 premium-unit metrics (reporting only):
        #     entry: fill 0.26, mid 0.25 -> effective_spread_paid = 2*0.01 = 0.02
        #     exit:  fill 0.40, mid 0.41 -> effective_spread_paid = 2*0.01 = 0.02
        #   Dollar metrics (the only ones P&L may use), qty=1:
        #     entry_slippage_dollars = (0.26-0.25)*1*100 = 1.00
        #     exit_slippage_dollars  = (0.41-0.40)*1*100 = 1.00
        #     fees                   = 0.05 + 0.05       = 0.10
        #     round_trip_friction_dollars = 2.00 + 0.10  = 2.10
        #     friction_share_of_target = 2.10 / 100.0    = 0.021
        planned_gross_profit = 100.0
        friction = friction_metrics(entry, exit_, planned_gross_profit)
        self.assertAlmostEqual(friction["entry_slippage_premium"], 0.02)
        self.assertAlmostEqual(friction["exit_slippage_premium"], 0.02)
        self.assertAlmostEqual(friction["entry_slippage"], 0.02)  # legacy alias, premium units
        self.assertAlmostEqual(friction["exit_slippage"], 0.02)
        self.assertAlmostEqual(friction["fees"], 0.10)
        self.assertAlmostEqual(friction["entry_slippage_dollars"], 1.00)
        self.assertAlmostEqual(friction["exit_slippage_dollars"], 1.00)
        self.assertAlmostEqual(friction["total_slippage_dollars"], 2.00)
        self.assertAlmostEqual(friction["round_trip_friction_dollars"], 2.10)
        self.assertAlmostEqual(friction["friction_share_of_target"], 0.021)
        self.assertTrue(friction["reconciles_to_direct_fill_pnl"])

    def test_friction_decomposition_reconciles_to_direct_fill_pnl(self):
        """The identity that makes the decomposition trustworthy:
        mid_pnl - total_slippage_dollars == (exit_fill - entry_fill) * qty * 100.
        friction_metrics raises if this ever stops holding; this asserts it
        positively, across several quantities."""
        for qty in (1, 3, 10):
            entry = simulate_entry_fill(pd.DataFrame([_quote(5, 0.24, 0.26)]), DECISION_TS, qty,
                                        FillConfig(fee_per_contract=0.05))
            exit_ = simulate_exit_fill(pd.DataFrame([_quote(5, 0.40, 0.42)]), DECISION_TS, qty,
                                       FillConfig(fee_per_contract=0.05))
            friction = friction_metrics(entry, exit_, 100.0, quantity=qty)
            direct = (exit_.fill_price - entry.fill_price) * qty * 100
            mid = (exit_.contemporaneous_mid - entry.contemporaneous_mid) * qty * 100
            self.assertAlmostEqual(mid - friction["total_slippage_dollars"], direct, places=6,
                                   msg=f"decomposition must reconcile exactly at qty={qty}")

    def test_friction_share_none_when_no_planned_profit(self):
        quotes = pd.DataFrame([_quote(5, 0.24, 0.26)])
        entry = simulate_entry_fill(quotes, DECISION_TS, 1, FillConfig())
        exit_quotes = pd.DataFrame([_quote(5, 0.10, 0.12)])
        exit_ = simulate_exit_fill(exit_quotes, DECISION_TS, 1, FillConfig())
        friction = friction_metrics(entry, exit_, planned_gross_profit=0.0)
        self.assertIsNone(friction["friction_share_of_target"])


if __name__ == "__main__":
    unittest.main()
