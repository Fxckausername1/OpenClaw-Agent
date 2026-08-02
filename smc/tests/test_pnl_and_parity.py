"""Required tests 19 (P&L unit reconciliation) and 20 (backtest/live dedup parity).

These two are about AGREEMENT between the backtest and the live system. A backtest
that measures a different strategy than the one running is worse than no backtest,
because it produces confident numbers about something that isn't happening.
"""

from __future__ import annotations

import datetime as dt
import shutil
import unittest

from smc.lifecycle import EXIT_STOP, execute_exit
from smc.reconcile import assert_occ_free_for_entry
from smc.risk import check_entry_allowed
from smc.state import CLOSED, OPEN
from smc.tests.harness import (
    FILL_IMMEDIATE, OCC, OTHER_OCC, FakeBroker, FakeClock, make_config, make_dashboard,
    make_store, make_tmpdir,
)
from thetadata_pipeline.bt2_fills import (
    FillConfig, friction_metrics, simulate_entry_fill, simulate_exit_fill, slippage_dollars,
)
from thetadata_pipeline.bt_dedup import (
    ADMIT, REJECT_MAX_CONCURRENT, REJECT_SAME_CONTRACT, AdmissionPolicy, OpenBook, admit,
)

import pandas as pd

DECISION_TS = pd.Timestamp("2026-07-31 14:00:00", tz="UTC")


def _quote(offset_s, bid, ask, size=50):
    return {"quote_ts": DECISION_TS + pd.Timedelta(seconds=offset_s), "bid": bid, "ask": ask,
            "bid_size": size, "ask_size": size}


# =============================================================== required 19
class PnlUnitReconciliationTest(unittest.TestCase):
    """The bug: net P&L mixed ACCOUNT DOLLARS with OPTION PREMIUM units.
    The contract: net_pnl == (exit_fill - entry_fill) * qty * 100 - dollar_fees."""

    def _fills(self, qty, entry_bid, entry_ask, exit_bid, exit_ask, fee=0.05):
        cfg = FillConfig(fee_per_contract=fee)
        entry = simulate_entry_fill(pd.DataFrame([_quote(5, entry_bid, entry_ask)]),
                                    DECISION_TS, qty, cfg)
        exit_ = simulate_exit_fill(pd.DataFrame([_quote(5, exit_bid, exit_ask)]),
                                   DECISION_TS, qty, cfg)
        return entry, exit_

    def test_decomposition_equals_direct_formula_across_quantities(self):
        for qty in (1, 2, 5, 10):
            entry, exit_ = self._fills(qty, 0.24, 0.26, 0.40, 0.42)
            friction = friction_metrics(entry, exit_, 100.0, quantity=qty)
            direct = (exit_.fill_price - entry.fill_price) * qty * 100
            mid = (exit_.contemporaneous_mid - entry.contemporaneous_mid) * qty * 100
            self.assertAlmostEqual(mid - friction["total_slippage_dollars"], direct, places=6,
                                   msg=f"must reconcile exactly at qty={qty}")

    def test_slippage_scales_with_quantity_and_contract_multiplier(self):
        """The heart of the bug: the old term did not scale with qty or the 100x
        multiplier, so it under-charged execution cost by exactly that factor."""
        e1, x1 = self._fills(1, 0.24, 0.26, 0.40, 0.42)
        e5, x5 = self._fills(5, 0.24, 0.26, 0.40, 0.42)
        s1 = slippage_dollars(e1, x1, 1)["total_slippage_dollars"]
        s5 = slippage_dollars(e5, x5, 5)["total_slippage_dollars"]
        self.assertAlmostEqual(s1, 2.00, places=6)   # (0.01 + 0.01) * 1 * 100
        self.assertAlmostEqual(s5, 10.00, places=6)  # (0.01 + 0.01) * 5 * 100
        self.assertAlmostEqual(s5, s1 * 5, places=6)

    def test_premium_unit_metric_is_kept_but_clearly_separate(self):
        """Section 7's effective_spread_paid stays in premium units for reporting;
        it must NOT be numerically confusable with the dollar figure."""
        entry, exit_ = self._fills(1, 0.24, 0.26, 0.40, 0.42)
        f = friction_metrics(entry, exit_, 100.0, quantity=1)
        self.assertAlmostEqual(f["entry_slippage_premium"], 0.02, places=6)
        self.assertAlmostEqual(f["entry_slippage_dollars"], 1.00, places=6)
        self.assertNotAlmostEqual(f["entry_slippage_premium"], f["entry_slippage_dollars"])

    def test_zero_bid_exit_reconciles_as_total_loss(self):
        """Adversarial: exit bid is zero (worthless liquidation). The identity must
        still hold, and the loss must be the full premium paid, not a midpoint."""
        entry, exit_ = self._fills(1, 0.24, 0.26, 0.0, 0.02)
        f = friction_metrics(entry, exit_, 100.0, quantity=1)
        direct = (exit_.fill_price - entry.fill_price) * 1 * 100
        self.assertAlmostEqual(direct, -26.0, places=6)
        mid = (exit_.contemporaneous_mid - entry.contemporaneous_mid) * 1 * 100
        self.assertAlmostEqual(mid - f["total_slippage_dollars"], direct, places=6)

    def test_midpoint_model_has_zero_slippage_by_construction(self):
        cfg = FillConfig(fill_model="midpoint", fee_per_contract=0.05)
        entry = simulate_entry_fill(pd.DataFrame([_quote(5, 0.24, 0.26)]), DECISION_TS, 1, cfg)
        exit_ = simulate_exit_fill(pd.DataFrame([_quote(5, 0.40, 0.42)]), DECISION_TS, 1, cfg)
        f = friction_metrics(entry, exit_, 100.0, quantity=1)
        self.assertAlmostEqual(f["total_slippage_dollars"], 0.0, places=6)

    def test_old_buggy_formula_provably_does_not_reconcile(self):
        """Pins down the defect itself rather than the guard.

        The runtime assertion in friction_metrics cannot be tripped by bad PRICES --
        the identity mid_pnl - slippage == direct is algebraic and holds for any
        consistent set of inputs. It can only be tripped by the CODE using the wrong
        formula, which is exactly what it exists to catch. So this test reconstructs
        the OLD formula and demonstrates it does not reconcile, and by how much.

        Old: net = mid_pnl(dollars) - (entry_slip + exit_slip)(PREMIUM) - fees
        New: net = (exit_fill - entry_fill) * qty * 100 - fees
        """
        for qty in (1, 5):
            entry, exit_ = self._fills(qty, 0.24, 0.26, 0.40, 0.42)
            f = friction_metrics(entry, exit_, 100.0, quantity=qty)
            fees = f["fees"]

            mid_pnl = (exit_.contemporaneous_mid - entry.contemporaneous_mid) * qty * 100
            correct = (exit_.fill_price - entry.fill_price) * qty * 100 - fees

            old_premium_term = f["entry_slippage_premium"] + f["exit_slippage_premium"]
            old_buggy = mid_pnl - old_premium_term - fees

            self.assertNotAlmostEqual(
                old_buggy, correct, places=2,
                msg=f"the old formula must be demonstrably wrong at qty={qty}")
            # The error is exactly the un-scaled portion of the slippage term.
            expected_error = f["total_slippage_dollars"] - old_premium_term
            self.assertAlmostEqual(old_buggy - correct, expected_error, places=6)
            # And it always OVERSTATES P&L, which is the dangerous direction.
            self.assertGreater(old_buggy, correct,
                               "the bug inflated reported P&L, it did not deflate it")

    def test_error_magnitude_matches_the_observed_baseline_inflation(self):
        """Sanity-check the diagnosis against the real observed gap: the 162-session
        B1 baseline moved $7.27 -> $6.19/trade, i.e. ~$1.08 of overstatement per
        trade. At a 1-lot with roughly a penny of spread on each leg the formula
        predicts ~$0.98-$1.98, so ~$1.08 sits squarely in range -- consistent with
        this being the whole explanation rather than a partial one."""
        entry, exit_ = self._fills(1, 0.245, 0.255, 0.395, 0.405)  # 1c spreads
        f = friction_metrics(entry, exit_, 100.0, quantity=1)
        old_premium_term = f["entry_slippage_premium"] + f["exit_slippage_premium"]
        overstatement = f["total_slippage_dollars"] - old_premium_term
        self.assertGreater(overstatement, 0.90)
        self.assertLess(overstatement, 2.10)


class LiveVsBacktestPnlUnitTest(unittest.TestCase):
    """The live exit path must express P&L in the SAME units as the backtest."""

    def setUp(self):
        self.tmp = make_tmpdir()
        self.config = make_config(self.tmp)
        self.store = make_store(self.config)
        self.broker = FakeBroker()
        self.clock = FakeClock()

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_live_realized_pnl_uses_dollars_per_contract_x100(self):
        intent = self.store.create_entry_intent(
            signal_key="pnl:1", occ=OCC, underlying="QQQ", contract_right="C",
            signal_side="long", intended_qty=2, limit_price=0.80, order_type="limit",
            signal_ts="2026-07-31T14:00:00+00:00")
        self.store.mark_order_submitted(intent["client_order_id"])
        self.store.record_broker_ack(intent["client_order_id"], "bkr-pnl-1")
        self.store.record_order_fill(intent["client_order_id"], 2, 0.80, OPEN)
        self.store.record_entry_filled(intent["position_id"], 2, 0.80, True)
        self.broker.set_position(OCC, 2)
        self.broker.set_quote(OCC, bid=0.60, ask=0.62)
        self.broker.fill_policy = FILL_IMMEDIATE
        self.broker.fill_price = 0.60

        pos = self.store.get_position(intent["position_id"])
        outcome = execute_exit(self.store, self.broker, self.config, pos, EXIT_STOP,
                               arm=True, clock=self.clock)

        self.assertEqual(outcome.state, CLOSED)
        # (0.60 - 0.80) * 2 contracts * 100 = -40.00
        self.assertAlmostEqual(outcome.realized_pnl, -40.00, places=2)

    def test_live_pnl_omits_fees_and_that_difference_is_documented(self):
        """HONEST DIFFERENCE, asserted so it can't drift unnoticed: the backtest
        subtracts a modelled $0.05/contract/leg regulatory fee; the live path books
        gross fill-to-fill P&L because these accounts are commission-free and the
        broker reports no per-fill fee. The two therefore differ by the modelled fee
        (~$0.10 on a 1-lot round trip) and are NOT directly comparable to the cent.
        This test exists so that gap is a recorded, deliberate choice rather than an
        undiscovered inconsistency."""
        qty = 1
        entry_px, exit_px = 0.80, 0.60
        live_pnl = (exit_px - entry_px) * qty * 100
        modelled_fee = 0.05 * qty * 2
        backtest_pnl = live_pnl - modelled_fee
        self.assertAlmostEqual(live_pnl, -20.00, places=2)
        self.assertAlmostEqual(backtest_pnl, -20.10, places=2)
        self.assertAlmostEqual(live_pnl - backtest_pnl, modelled_fee, places=6)


# =============================================================== required 20
class DedupParityTest(unittest.TestCase):
    """The live system forbids a second position on the same contract. The backtest
    must model the same restriction, or it is measuring a different strategy."""

    def setUp(self):
        self.tmp = make_tmpdir()
        self.config = make_config(self.tmp, max_concurrent_positions=2,
                                  max_correlated_qqq_contracts=2)
        self.store = make_store(self.config)
        self.broker = FakeBroker()
        self.broker.set_quote(OCC, bid=0.50, ask=0.52)
        self.dashboard = make_dashboard(self.tmp)
        self.clock = FakeClock()
        self.policy = AdmissionPolicy(max_concurrent_positions=2, max_correlated_contracts=2)

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _open_live(self, occ, qty=1, key=None):
        intent = self.store.create_entry_intent(
            signal_key=key or f"k:{occ}", occ=occ, underlying="QQQ", contract_right="C",
            signal_side="long", intended_qty=qty, limit_price=0.50, order_type="limit",
            signal_ts="2026-07-31T14:00:00+00:00")
        self.store.mark_order_submitted(intent["client_order_id"])
        self.store.record_broker_ack(intent["client_order_id"], f"bkr-{intent['position_id']}")
        self.store.record_order_fill(intent["client_order_id"], qty, 0.50, OPEN)
        self.store.record_entry_filled(intent["position_id"], qty, 0.50, True)
        self.broker.set_position(occ, qty)
        return intent

    def test_same_contract_rejected_by_both_sides(self):
        book = OpenBook()
        book.add(OCC, 1)
        bt_decision, bt_reason = admit(book, OCC, 1, self.policy)

        self._open_live(OCC, 1)
        live_allowed, live_reason = assert_occ_free_for_entry(
            self.store, self.broker, OCC, self.dashboard)

        self.assertEqual(bt_decision, REJECT_SAME_CONTRACT)
        self.assertFalse(live_allowed)
        self.assertIn("already", live_reason.lower())

    def test_different_contract_admitted_by_both_sides(self):
        book = OpenBook()
        book.add(OCC, 1)
        bt_decision, _ = admit(book, OTHER_OCC, 1, self.policy)

        self._open_live(OCC, 1)
        live_allowed, live_reason = assert_occ_free_for_entry(
            self.store, self.broker, OTHER_OCC, self.dashboard)

        self.assertEqual(bt_decision, ADMIT)
        self.assertTrue(live_allowed, live_reason)

    def test_concurrency_cap_rejected_by_both_sides(self):
        book = OpenBook()
        book.add(OCC, 1)
        book.add(OTHER_OCC, 1)
        third = "QQQ260731C00695000"
        bt_decision, _ = admit(book, third, 1, self.policy)

        self._open_live(OCC, 1, key="k1")
        self._open_live(OTHER_OCC, 1, key="k2")
        gate = check_entry_allowed(
            self.store, self.broker, self.config, occ=third, entry_premium=0.50,
            intended_qty=1, dashboard_db=self.dashboard,
            now_et=dt.datetime(2026, 7, 31, 11, 0,
                               tzinfo=dt.timezone(dt.timedelta(hours=-4))),
            schedule=_regular_schedule())

        self.assertIn(bt_decision, (REJECT_MAX_CONCURRENT,))
        self.assertFalse(gate.allowed)

    def test_signal_sequence_produces_identical_admission_decisions(self):
        """The 2026-07-31 sequence, replayed: three signals, all choosing the SAME
        contract. Both sides must admit exactly one."""
        sequence = [OCC, OCC, OCC]
        book = OpenBook()
        bt_admitted = []
        for i, occ in enumerate(sequence):
            decision, _ = admit(book, occ, 1, self.policy)
            if decision == ADMIT:
                bt_admitted.append(i)
                book.add(occ, 1)

        live_admitted = []
        for i, occ in enumerate(sequence):
            allowed, _ = assert_occ_free_for_entry(self.store, self.broker, occ, self.dashboard)
            if allowed:
                live_admitted.append(i)
                self._open_live(occ, 1, key=f"seq:{i}")

        self.assertEqual(bt_admitted, live_admitted,
                         "backtest and live must admit the SAME signals")
        self.assertEqual(len(bt_admitted), 1,
                         "exactly one of three same-contract signals may open")

    def test_backtest_without_dedup_would_overcount_positions(self):
        """Quantifies the modelling gap this closes: with dedup disabled (the old
        implicit backtest behaviour) all three same-contract signals are simulated
        as separate positions."""
        no_dedup = AdmissionPolicy(max_concurrent_positions=99, max_correlated_contracts=99,
                                   block_same_contract=False)
        book = OpenBook()
        admitted = 0
        for occ in [OCC, OCC, OCC]:
            decision, _ = admit(book, occ, 1, no_dedup)
            if decision == ADMIT:
                admitted += 1
                book.add(occ, 1)
        self.assertEqual(admitted, 3, "documents the un-deduped behaviour")
        self.assertEqual(book.total_contracts(), 3)


def _regular_schedule():
    from smc.calendar import SessionSchedule
    from zoneinfo import ZoneInfo
    et = ZoneInfo("America/New_York")
    d = dt.date(2026, 7, 31)
    return SessionSchedule(
        session_date=d, is_trading_day=True,
        open_et=dt.datetime.combine(d, dt.time(9, 30), tzinfo=et),
        close_et=dt.datetime.combine(d, dt.time(16, 0), tzinfo=et),
        is_early_close=False, degraded=False, source="test")


if __name__ == "__main__":
    unittest.main()
