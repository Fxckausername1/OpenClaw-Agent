"""BT-2 acceptance-gate suite (roadmap Section 18: "Golden-path and
adversarial tests pass"). Golden path proves one full synthetic session
resolves selector -> fill -> exit -> ledger row with hand-checked P&L.
Adversarial tests are the two roadmap Section 16 explicitly names: a
reversed signal must not look profitable, and a forced cheapest-contract
selection must show measurably worse friction than the real quality-ranked
choice. Component-level Section 16 scenario coverage (quote vanishes, bid
becomes zero, target/stop same bar, no preferred-premium contract, data
gap, trading halt) lives in test_bt2_fills.py/test_bt2_exits.py/
test_bt2_selector.py; this file adds full-pipeline integration checks on
top, not a second copy of the same unit tests.

Ledger-writer tests here ALWAYS pass an explicit tmp path to
append_ledger_rows -- never the real BT2_LEDGER_PATH default. Real
incident this guards against, found live in this same session: an
existing test (test_bt1_pilot.py) once wrote to the real production
manifest path by omission, silently clobbering heff's actual graded
5-session BT-1 pilot data. Fixed there; never repeated here.
"""

import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

import thetadata_pipeline.bt2_simulator as bt2sim
from thetadata_pipeline.bt2_exits import ExitConfig
from thetadata_pipeline.bt2_fills import FILL_MODEL_MIDPOINT, FillConfig
from thetadata_pipeline.bt2_schemas import GATE_FAIL, GATE_PASS, validate_ledger_row
from thetadata_pipeline.bt2_selector import SelectionResult, SelectorConfig, evaluate_candidate
from thetadata_pipeline.bt2_simulator import TradeInputs, append_ledger_rows, simulate_trade
from thetadata_pipeline.schemas import contract_id as build_cid

SESSION = "2026-07-24"
SESSION_DATE = dt.date(2026, 7, 24)
DECISION_TS = pd.Timestamp("2026-07-24 14:00:00", tz="UTC")  # 10:00 ET


def _row(cid, expiration, strike, right, ts, bid, ask, bid_size=20, ask_size=20):
    return dict(contract_id=cid, expiration=expiration, strike=strike, right=right,
                trade_timestamp=ts, bid=bid, ask=ask, bid_size=bid_size, ask_size=ask_size)


CALL_CID = build_cid("SPY", SESSION_DATE, 745.0, "C")


def _golden_path_trades():
    return pd.DataFrame([
        _row(CALL_CID, "2026-07-24", 745.0, "C", DECISION_TS - pd.Timedelta(seconds=2), 0.24, 0.26),
        _row(CALL_CID, "2026-07-24", 745.0, "C", DECISION_TS + pd.Timedelta(seconds=5), 0.24, 0.26),
        _row(CALL_CID, "2026-07-24", 745.0, "C", DECISION_TS + pd.Timedelta(minutes=5), 0.40, 0.41),
        _row(CALL_CID, "2026-07-24", 745.0, "C", DECISION_TS + pd.Timedelta(minutes=5, seconds=4), 0.42, 0.44),
    ])


def _golden_path_inputs(**overrides):
    kwargs = dict(
        session=SESSION, symbol="SPY", signal_ts=DECISION_TS - pd.Timedelta(seconds=30),
        decision_ts=DECISION_TS, direction="CALL WATCH", context_snapshot_id="CB-2026-07-24:SPY",
        experiment_id="exp-1", quantity=1,
    )
    kwargs.update(overrides)
    return TradeInputs(**kwargs)


class GoldenPathTests(unittest.TestCase):
    """One full synthetic session: signal fires, a contract is selected,
    fills happen at both entry and exit, and the ledger row comes out with
    every schema field populated and P&L arithmetic correct by hand-check."""

    def setUp(self):
        self.row = simulate_trade(
            _golden_path_inputs(), _golden_path_trades(), pd.DataFrame(),
            greeks={CALL_CID: {"delta": 0.35}},
        )

    def test_schema_is_clean(self):
        self.assertEqual(validate_ledger_row(self.row), [])

    def test_contract_selected_and_gate_pass(self):
        self.assertEqual(self.row["contract_id"], CALL_CID)
        self.assertEqual(self.row["contract_gate"], GATE_PASS)

    def test_entry_and_exit_arrays_populated(self):
        self.assertEqual(self.row["entry_ask"], [0.26])
        self.assertEqual(self.row["entry_bid"], [0.24])
        self.assertEqual(self.row["entry_fill"], [0.26])
        self.assertEqual(self.row["quantity"], [1])
        self.assertEqual(self.row["exit_bid"], [0.42])
        self.assertEqual(self.row["exit_ask"], [0.44])
        self.assertEqual(self.row["exit_fill"], [0.42])

    def test_exit_reason_is_target(self):
        self.assertEqual(self.row["exit_reason"], "TARGET")

    def test_target_and_stop_computed_off_real_entry_fill(self):
        # target = 0.26 * 1.25 = 0.325; stop = 0.26 * 0.80 = 0.208
        self.assertAlmostEqual(self.row["target"], 0.325, places=4)
        self.assertAlmostEqual(self.row["stop"], 0.208, places=4)

    def test_pnl_arithmetic_by_hand(self):
        """CORRECTED 2026-07-31. The previous expectation (net 17.86, slippage 0.04)
        encoded a real accounting bug: slippage was subtracted in OPTION PREMIUM
        units from a DOLLAR-denominated midpoint P&L, understating execution cost
        by qty*100/2. On this fixture that inflated net P&L by $1.96.

        Correct arithmetic, qty=1, fees $0.05/contract/leg:
          direct  net_pnl = (exit_fill 0.42 - entry_fill 0.26) * 1 * 100 - 0.10 = 15.90
          decomposition (must reconcile to the same number):
            gross_pnl (midpoint) = (0.43 - 0.25) * 1 * 100        = 18.00
            entry_slippage_dollars = (0.26 - 0.25) * 1 * 100      =  1.00
            exit_slippage_dollars  = (0.43 - 0.42) * 1 * 100      =  1.00
            18.00 - 2.00 - 0.10                                   = 15.90  ✓
        """
        self.assertAlmostEqual(self.row["gross_pnl"], 18.00, places=2)
        self.assertAlmostEqual(self.row["fees"], 0.10, places=4)
        self.assertAlmostEqual(self.row["slippage"], 2.00, places=4)
        self.assertAlmostEqual(self.row["net_pnl"], 15.90, places=2)

    def test_net_pnl_equals_direct_fill_formula(self):
        """The contract stated in BT-0: net P&L must reconcile directly to
        (exit_fill - entry_fill) * quantity * 100 - dollar_fees."""
        qty = self.row["quantity"][0]
        direct = ((self.row["exit_fill"][0] - self.row["entry_fill"][0]) * qty * 100
                  - self.row["fees"])
        self.assertAlmostEqual(self.row["net_pnl"], round(direct, 2), places=2)

    def test_pnl_decomposition_reconciles(self):
        recon = self.row["gross_pnl"] - self.row["slippage"] - self.row["fees"]
        self.assertAlmostEqual(self.row["net_pnl"], round(recon, 2), places=2)

    def test_mae_mfe(self):
        self.assertAlmostEqual(self.row["mae"], 0.0, places=4)
        self.assertAlmostEqual(self.row["mfe"], 0.16, places=4)

    def test_data_quality_ok_and_no_stray_flags(self):
        self.assertEqual(self.row["data_quality"], "OK")
        self.assertNotIn(bt2sim.FLAG_NO_CONTRACT, self.row["rule_flags"])
        self.assertNotIn(bt2sim.FLAG_NO_FILL, self.row["rule_flags"])



class AdmissionGateTests(unittest.TestCase):
    def test_rejection_happens_after_selection_but_before_any_fill(self):
        seen = []
        def reject(occ, qty):
            seen.append((occ, qty))
            return "SAME_CONTRACT_ALREADY_OPEN", "already open"
        row = simulate_trade(
            _golden_path_inputs(), _golden_path_trades(), pd.DataFrame(),
            greeks={CALL_CID: {"delta": 0.35}}, admission_check=reject,
        )
        self.assertEqual(seen, [(CALL_CID, 1)])
        self.assertEqual(row["exit_reason"], "ADMISSION_REJECT")
        self.assertEqual(row["contract_id"], CALL_CID)
        self.assertEqual(row["entry_fill"], [])
        self.assertEqual(row["data_quality"], "SAME_CONTRACT_ALREADY_OPEN")
        self.assertEqual(validate_ledger_row(row), [])
class NoContractOutcomeTests(unittest.TestCase):
    def test_no_passing_candidate_still_produces_a_valid_ledger_row(self):
        cid = build_cid("SPY", SESSION_DATE, 745.0, "C")
        trades = pd.DataFrame([_row(cid, "2026-07-24", 745.0, "C",
                                     DECISION_TS - pd.Timedelta(seconds=2), 0.60, 0.90)])  # premium way outside band
        row = simulate_trade(_golden_path_inputs(), trades, pd.DataFrame(), greeks={cid: {"delta": 0.55}})
        self.assertEqual(row["contract_gate"], GATE_FAIL)
        self.assertEqual(row["contract_id"], None)
        self.assertEqual(row["exit_reason"], "NO_CONTRACT")
        self.assertEqual(row["entry_fill"], [])
        self.assertIsNone(row["net_pnl"])
        self.assertIn(bt2sim.FLAG_NO_CONTRACT, row["rule_flags"])
        self.assertEqual(validate_ledger_row(row), [])


class NoFillOutcomeTests(unittest.TestCase):
    def test_selected_contract_with_no_post_decision_quote_is_no_fill(self):
        cid = build_cid("SPY", SESSION_DATE, 745.0, "C")
        # Only a pre-decision quote exists -- nothing at/after the
        # reaction-latency floor to actually execute against.
        trades = pd.DataFrame([_row(cid, "2026-07-24", 745.0, "C",
                                     DECISION_TS - pd.Timedelta(seconds=2), 0.24, 0.26)])
        row = simulate_trade(_golden_path_inputs(), trades, pd.DataFrame(), greeks={cid: {"delta": 0.35}})
        self.assertEqual(row["contract_gate"], GATE_PASS)
        self.assertEqual(row["contract_id"], cid)
        self.assertEqual(row["exit_reason"], "NO_FILL")
        self.assertEqual(row["entry_fill"], [])
        self.assertIsNone(row["net_pnl"])
        self.assertIn(bt2sim.FLAG_NO_FILL, row["rule_flags"])
        self.assertIsNotNone(row["target"])  # planned target/stop still recorded even without a real fill
        self.assertEqual(validate_ledger_row(row), [])


class MidpointModelFlagTests(unittest.TestCase):
    def test_midpoint_model_is_flagged_never_headline(self):
        row = simulate_trade(
            _golden_path_inputs(), _golden_path_trades(), pd.DataFrame(),
            greeks={CALL_CID: {"delta": 0.35}}, fill_config=FillConfig(fill_model=FILL_MODEL_MIDPOINT),
        )
        self.assertIn(bt2sim.FLAG_MIDPOINT_NEVER_HEADLINE, row["rule_flags"])
        # midpoint model: entry/exit fill exactly at contemporaneous mid ->
        # zero slippage by construction, which is exactly why it must never
        # be headlined as if it were realistic.
        self.assertAlmostEqual(row["slippage"], 0.0, places=6)


class LedgerWriterTests(unittest.TestCase):
    def test_atomic_write_and_append_across_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bt2_trade_ledger.json"
            row1 = simulate_trade(_golden_path_inputs(), _golden_path_trades(), pd.DataFrame(),
                                   greeks={CALL_CID: {"delta": 0.35}})
            append_ledger_rows([row1], path=path)
            on_disk = json.loads(path.read_text())
            self.assertEqual(len(on_disk["trades"]), 1)

            row2 = dict(row1)
            row2["trade_id"] = "second-trade"
            append_ledger_rows([row2], path=path)
            on_disk = json.loads(path.read_text())
            self.assertEqual(len(on_disk["trades"]), 2)
            self.assertEqual({t["trade_id"] for t in on_disk["trades"]}, {row1["trade_id"], "second-trade"})


class ReversedSignalNegativeControlTests(unittest.TestCase):
    """Section 16 adversarial test: reverse the signal direction as a
    negative control -- expectancy should NOT look good under a reversed
    signal, or something in the pipeline is leaking. This session's
    fixture is genuinely bullish (call premium rallies, matching
    GoldenPathTests); mirroring the same underlying move onto a put
    contract at the same strike/expiration must show a real loss, not a
    fabricated profit regardless of which side you pick."""

    def test_reversed_direction_on_a_bullish_session_loses_money(self):
        put_cid = build_cid("SPY", SESSION_DATE, 745.0, "P")
        put_trades = pd.DataFrame([
            _row(put_cid, "2026-07-24", 745.0, "P", DECISION_TS - pd.Timedelta(seconds=2), 0.24, 0.26),
            _row(put_cid, "2026-07-24", 745.0, "P", DECISION_TS + pd.Timedelta(seconds=5), 0.22, 0.24),
            _row(put_cid, "2026-07-24", 745.0, "P", DECISION_TS + pd.Timedelta(minutes=5), 0.05, 0.07),
            _row(put_cid, "2026-07-24", 745.0, "P", DECISION_TS + pd.Timedelta(minutes=5, seconds=4), 0.04, 0.06),
        ])
        call_row = simulate_trade(_golden_path_inputs(direction="CALL WATCH"), _golden_path_trades(),
                                   pd.DataFrame(), greeks={CALL_CID: {"delta": 0.35}})
        put_row = simulate_trade(_golden_path_inputs(direction="PUT WATCH"), put_trades,
                                  pd.DataFrame(), greeks={put_cid: {"delta": -0.35}})

        self.assertGreater(call_row["net_pnl"], 0)     # the real, correctly-signaled direction wins
        self.assertLess(put_row["net_pnl"], 0)         # the reversed direction genuinely loses -- no leakage


class ForcedCheapestContractAdversarialTests(unittest.TestCase):
    """Section 16 adversarial test: force the selector to pick the cheapest
    contract available and compare simulated damage/friction vs the real
    selector's choice. Both contracts see an identical flat (no-movement)
    price path to isolate the comparison to selection quality itself, not
    exit-path differences."""

    def _flat_session(self, cid, expiration, strike, right, bid, ask):
        return pd.DataFrame([
            _row(cid, expiration, strike, right, DECISION_TS - pd.Timedelta(seconds=2), bid, ask),
            _row(cid, expiration, strike, right, DECISION_TS + pd.Timedelta(seconds=5), bid, ask),
            _row(cid, expiration, strike, right, DECISION_TS + pd.Timedelta(hours=3), bid, ask),
            _row(cid, expiration, strike, right, pd.Timestamp("2026-07-24 19:30:05", tz="UTC"), bid, ask),
        ])

    def _forced_candidate(self, strike, bid, ask, delta):
        # Deliberately does NOT require candidate["passed"] -- "force the
        # selector to pick the cheapest contract available" (Section 16)
        # means bypassing normal gating on purpose, to measure how bad an
        # undisciplined cheapest-first policy would be. evaluate_candidate
        # still returns the full computed shape (bid/ask/spread/etc.)
        # regardless of pass/fail, which is all simulate_trade needs.
        row = dict(strike=strike, right="C", expiration=SESSION_DATE, bid=bid, ask=ask,
                   bid_size=20, ask_size=20, quote_age_seconds=2.0, delta=delta)
        candidate = evaluate_candidate(row, SESSION_DATE, SelectorConfig())
        candidate.pop("_delta_distance", None)
        return candidate

    def test_cheapest_contract_has_far_worse_friction_share(self):
        quality_cid = build_cid("SPY", SESSION_DATE, 745.0, "C")
        cheap_cid = build_cid("SPY", SESSION_DATE, 760.0, "C")
        quality_candidate = self._forced_candidate(745.0, 0.24, 0.26, 0.35)
        cheap_candidate = self._forced_candidate(760.0, 0.045, 0.05, 0.35)
        self.assertTrue(quality_candidate["passed"])  # the real selector's own choice is a legitimate PASS
        self.assertFalse(cheap_candidate["passed"])   # the forced cheapest pick would never legitimately clear the gate

        # time_stop disabled (set far beyond the session length) so this
        # comparison isolates selection/friction quality, not an unrelated
        # "no progress" time-stop exit -- both fixtures are deliberately
        # flat (bid held below entry premium the whole session).
        no_time_stop = ExitConfig(time_stop_minutes=100000)
        with mock.patch.object(bt2sim, "select_contract",
                                return_value=SelectionResult(contract=quality_candidate, candidates_checked=1, reason=None)):
            quality_row = simulate_trade(
                _golden_path_inputs(), self._flat_session(quality_cid, "2026-07-24", 745.0, "C", 0.24, 0.26),
                pd.DataFrame(), greeks={quality_cid: {"delta": 0.35}}, exit_config=no_time_stop,
            )
        with mock.patch.object(bt2sim, "select_contract",
                                return_value=SelectionResult(contract=cheap_candidate, candidates_checked=1, reason=None)):
            cheap_row = simulate_trade(
                _golden_path_inputs(), self._flat_session(cheap_cid, "2026-07-24", 760.0, "C", 0.045, 0.05),
                pd.DataFrame(), greeks={cheap_cid: {"delta": 0.35}}, exit_config=no_time_stop,
            )

        self.assertEqual(quality_row["exit_reason"], "FORCED_CLOSE")
        self.assertEqual(cheap_row["exit_reason"], "FORCED_CLOSE")
        quality_share = quality_row["friction_detail"]["friction_share_of_target"]
        cheap_share = cheap_row["friction_detail"]["friction_share_of_target"]
        self.assertIsNotNone(quality_share)
        self.assertIsNotNone(cheap_share)

        # THRESHOLD CORRECTED 2026-07-31. The old assertion was `cheap_share >
        # quality_share * 3`, which only held because friction_share was computed
        # with a PREMIUM-unit numerator over a DOLLAR denominator. That bug
        # compressed the expensive contract's apparent friction far more than the
        # cheap one's, manufacturing a ~4x ratio out of a real ~1.5x one.
        #
        # Real, unit-consistent numbers for these fixtures:
        #   quality (0.24/0.26): friction $2.10 / planned target $6.50 = 0.3231
        #   cheap  (0.045/0.05): friction $0.60 / planned target $1.25 = 0.4800
        #
        # The Section 16 conclusion still holds and is what we assert: forcing the
        # cheapest contract makes friction eat ~48% of the planned target versus
        # ~32% for the selector's own pick. The "3x" magnitude was never real, so
        # asserting it would be asserting the bug.
        self.assertGreater(cheap_share, quality_share * 1.4,
                           f"cheapest-first must be materially worse "
                           f"(cheap={cheap_share} quality={quality_share})")
        self.assertGreater(cheap_share, 0.4)
        self.assertLess(quality_share, 0.4)


if __name__ == "__main__":
    unittest.main()
