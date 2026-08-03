"""BT-3 B1 indicator-only test suite. Same conventions as
test_bt3_b0_random_control.py: golden-path + adversarial cases, hand-checked
numbers, no real network or real production files touched. Two groups:

1. Signal generation (GenerateB1SignalsTests) -- exercises
   generate_b1_signals against synthetic triangle-event fixtures (the real
   shape heff_smc_replay.run_and_persist writes), no disk I/O beyond an
   injected event list.

2. Pipeline + report math (SimulateB1SignalTests, SessionBootstrapTests,
   SummarizeB1Tests, RenderSummaryMdTests) -- exercises simulate_b1_signal()
   (which calls the REAL bt2_simulator.simulate_trade, unmodified) against
   synthetic trades_df/greeks fixtures in the same shape
   test_bt3_b0_random_control.py's own SimulateB0SignalTests use, and the
   session-level cluster bootstrap against small, hand-checkable multi-
   session ledgers (the key B1-vs-B0 divergence: B1 can have MULTIPLE
   trades per session, so the bootstrap must resample SESSIONS, not
   individual trades -- see session_bootstrap_mean_ci's own docstring).

All ledger-writer tests use tmp paths -- never the real BT3_B1_LEDGER_PATH.
"""

import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

import thetadata_pipeline.bt3_b1_indicator_only as b1
from thetadata_pipeline.bt2_schemas import GATE_FAIL, GATE_PASS, validate_ledger_row
from thetadata_pipeline.schemas import contract_id as build_cid

ET = ZoneInfo("America/New_York")


def _event(session, side, time_str, trigger="MSS", score=6.0, price=500.0, bar_index=100):
    return {
        "event": "CONFLUENCE", "ticker": "QQQ", "tf": "1", "side": side, "price": price,
        "score": score, "threshold": 5.0, "trigger": trigger, "mode": "honest",
        "factors": {"structure": 2.5}, "htf_bias": 1, "time": f"{session} {time_str}",
        "session": session, "bar_index": bar_index,
    }


class GenerateB1SignalsTests(unittest.TestCase):
    def test_one_signal_per_event_with_correct_direction_mapping(self):
        events = [_event("2026-05-04", "long", "10:15:00"), _event("2026-05-04", "short", "11:00:00")]
        signals = b1.generate_b1_signals(events)
        self.assertEqual(len(signals), 2)
        self.assertEqual(signals[0]["direction"], "CALL WATCH")
        self.assertEqual(signals[1]["direction"], "PUT WATCH")
        self.assertEqual(signals[0]["underlying_price"], 500.0)

    def test_decision_ts_is_et_localized_and_matches_event_time(self):
        events = [_event("2026-05-04", "long", "10:15:00")]
        sig = b1.generate_b1_signals(events)[0]
        self.assertEqual(sig["decision_ts"].tzinfo.key if hasattr(sig["decision_ts"].tzinfo, "key") else str(sig["decision_ts"].tzinfo), str(ET))
        self.assertEqual(sig["decision_ts"].hour, 10)
        self.assertEqual(sig["decision_ts"].minute, 15)

    def test_charter_window_flag_true_inside_1000_1530_false_outside(self):
        events = [
            _event("2026-05-04", "long", "09:45:00"),   # before 10:00
            _event("2026-05-04", "long", "12:00:00"),   # inside
            _event("2026-05-04", "long", "15:45:00"),   # after 15:30
        ]
        signals = b1.generate_b1_signals(events)
        self.assertEqual([s["in_charter_window"] for s in signals], [False, True, False])

    def test_multiple_events_same_session_all_preserved(self):
        events = [_event("2026-05-04", "long", "10:00:00", bar_index=1),
                  _event("2026-05-04", "short", "10:05:00", bar_index=2),
                  _event("2026-05-04", "long", "10:10:00", bar_index=3)]
        signals = b1.generate_b1_signals(events)
        self.assertEqual(len(signals), 3)
        self.assertEqual([s["bar_index"] for s in signals], [1, 2, 3])


# --- Feeding a signal through the real BT-2 pipeline ------------------------

SESSION = "2026-07-24"
SESSION_DATE = dt.date(2026, 7, 24)
DECISION_TS = pd.Timestamp("2026-07-24 10:15:00", tz=ET)
CALL_CID = build_cid("QQQ", SESSION_DATE, 555.0, "C")


def _row(cid, expiration, strike, right, ts, bid, ask, bid_size=20, ask_size=20):
    return dict(contract_id=cid, expiration=expiration, strike=strike, right=right,
                trade_timestamp=ts, bid=bid, ask=ask, bid_size=bid_size, ask_size=ask_size)


def _bullish_trades():
    return pd.DataFrame([
        _row(CALL_CID, "2026-07-24", 555.0, "C", DECISION_TS - pd.Timedelta(seconds=2), 0.24, 0.26),
        _row(CALL_CID, "2026-07-24", 555.0, "C", DECISION_TS + pd.Timedelta(seconds=5), 0.24, 0.26),
        _row(CALL_CID, "2026-07-24", 555.0, "C", DECISION_TS + pd.Timedelta(minutes=5), 0.40, 0.41),
        _row(CALL_CID, "2026-07-24", 555.0, "C", DECISION_TS + pd.Timedelta(minutes=5, seconds=4), 0.42, 0.44),
    ])


class SimulateB1SignalTests(unittest.TestCase):
    def _sig(self, **overrides):
        base = dict(
            symbol="QQQ", session=SESSION, decision_ts=DECISION_TS, direction="CALL WATCH",
            underlying_price=555.0,
            trigger="MSS", score=6.25, bar_index=42, in_charter_window=True,
        )
        base.update(overrides)
        return base

    def test_filled_trade_matches_real_pipeline_arithmetic(self):
        row = b1.simulate_b1_signal(self._sig(), _bullish_trades(), greeks={CALL_CID: {"delta": 0.35}})
        self.assertEqual(validate_ledger_row(row), [])
        self.assertEqual(row["contract_gate"], GATE_PASS)
        self.assertAlmostEqual(row["gross_pnl"], 18.00, places=2)
        # CORRECTED 2026-07-31: net P&L now reconciles directly to
        # (exit_fill - entry_fill) * qty * 100 - dollar_fees. The old 17.86 was
        # produced by subtracting PREMIUM-unit slippage from a DOLLAR-denominated
        # midpoint P&L, understating execution cost by qty*100/2 ($1.96 here).
        self.assertAlmostEqual(row["net_pnl"], 15.90, places=2)
        self.assertIn(b1.LABEL_B1, row["rule_flags"])
        self.assertEqual(row["experiment_id"], b1.EXPERIMENT_ID)
        self.assertIsNone(row["invalidation"], "B1 wiring must leave invalidation_level unset, identical to B0")
        self.assertEqual(row["heff_smc_trigger"], "MSS")
        self.assertAlmostEqual(row["heff_smc_score"], 6.25)
        self.assertTrue(row["heff_smc_in_charter_window"])

    def test_no_contract_outcome_is_a_clean_ledger_row(self):
        trades = pd.DataFrame([_row(CALL_CID, "2026-07-24", 555.0, "C",
                                     DECISION_TS - pd.Timedelta(seconds=2), 0.60, 0.90)])
        row = b1.simulate_b1_signal(self._sig(), trades, greeks={CALL_CID: {"delta": 0.55}})
        self.assertEqual(row["contract_gate"], GATE_FAIL)
        self.assertEqual(row["exit_reason"], "NO_CONTRACT")
        self.assertIsNone(row["net_pnl"])
        self.assertEqual(validate_ledger_row(row), [])

    def test_context_snapshot_id_names_b1_and_includes_trigger(self):
        row = b1.simulate_b1_signal(self._sig(), pd.DataFrame(), {})
        self.assertTrue(row["context_snapshot_id"].startswith("B1-HEFF-SMC:"))
        self.assertIn("MSS", row["context_snapshot_id"])

    def test_quantity_is_always_one(self):
        row = b1.simulate_b1_signal(self._sig(), _bullish_trades(), greeks={CALL_CID: {"delta": 0.35}})
        self.assertEqual(row["quantity"], [1])


class ChronologicalAdmissionTests(unittest.TestCase):
    def test_overlapping_same_contract_signal_is_rejected_not_resimulated(self):
        trades = pd.DataFrame([
            _row(CALL_CID, SESSION, 555.0, "C", DECISION_TS - pd.Timedelta(seconds=2), 0.24, 0.26),
            _row(CALL_CID, SESSION, 555.0, "C", DECISION_TS + pd.Timedelta(seconds=5), 0.24, 0.26),
            _row(CALL_CID, SESSION, 555.0, "C", DECISION_TS + pd.Timedelta(seconds=58), 0.24, 0.26),
            _row(CALL_CID, SESSION, 555.0, "C", DECISION_TS + pd.Timedelta(seconds=65), 0.24, 0.26),
            _row(CALL_CID, SESSION, 555.0, "C", DECISION_TS + pd.Timedelta(minutes=5), 0.40, 0.41),
            _row(CALL_CID, SESSION, 555.0, "C", DECISION_TS + pd.Timedelta(minutes=5, seconds=4), 0.42, 0.44),
        ])
        sig1 = dict(
            symbol="QQQ", session=SESSION, decision_ts=DECISION_TS, direction="CALL WATCH", underlying_price=555.0,
            trigger="MSS", score=6.25, bar_index=42, in_charter_window=True,
        )
        sig2 = dict(sig1, decision_ts=DECISION_TS + pd.Timedelta(minutes=1), bar_index=43)
        rows = b1.simulate_b1_session([sig2, sig1], trades)
        self.assertIsNotNone(rows[0]["net_pnl"])
        self.assertEqual(rows[0]["admission_decision"], "ADMIT")
        self.assertEqual(rows[1]["exit_reason"], "ADMISSION_REJECT")
        self.assertEqual(rows[1]["data_quality"], "SAME_CONTRACT_ALREADY_OPEN")
        self.assertEqual(rows[1]["entry_fill"], [])
        self.assertEqual(validate_ledger_row(rows[1]), [])
        summary = b1.summarize_b1(rows)
        self.assertEqual(summary["n_admission_rejected"], 1)
        self.assertEqual(summary["n_filled"], 1)


# --- Session-level cluster bootstrap ----------------------------------------

class SessionBootstrapCiTests(unittest.TestCase):
    def test_none_below_two_sessions(self):
        self.assertIsNone(b1.session_bootstrap_mean_ci({}))
        self.assertIsNone(b1.session_bootstrap_mean_ci({"2026-05-04": [10.0, -5.0]}))

    def test_reproducible_with_fixed_seed(self):
        data = {"2026-05-04": [10.0, -5.0], "2026-05-05": [20.0], "2026-05-06": [-8.0, 3.0, 4.0]}
        ci_a = b1.session_bootstrap_mean_ci(data, seed=99)
        ci_b = b1.session_bootstrap_mean_ci(data, seed=99)
        self.assertEqual(ci_a, ci_b)

    def test_point_estimate_is_the_real_pooled_mean(self):
        data = {"2026-05-04": [10.0, -5.0], "2026-05-05": [20.0], "2026-05-06": [-8.0, 3.0, 4.0]}
        ci = b1.session_bootstrap_mean_ci(data, seed=1)
        all_vals = [10.0, -5.0, 20.0, -8.0, 3.0, 4.0]
        self.assertAlmostEqual(ci["point_estimate"], sum(all_vals) / len(all_vals), places=4)
        self.assertEqual(ci["n_sessions"], 3)

    def test_a_single_extreme_session_moves_the_ci_a_lot_more_than_a_single_extreme_trade_would(self):
        """The real reason a per-trade bootstrap would be wrong here: if one
        session dumps many correlated losing trades, a trade-level bootstrap
        would treat each as an independent data point and understate the
        true uncertainty. A session-level bootstrap must be able to draw
        (or omit) that WHOLE session as one unit."""
        calm = {f"s{i}": [1.0, 1.0] for i in range(10)}
        one_bad_session = dict(calm)
        one_bad_session["s10"] = [-500.0, -500.0, -500.0]
        ci_calm = b1.session_bootstrap_mean_ci(calm, seed=5)
        ci_bad = b1.session_bootstrap_mean_ci(one_bad_session, seed=5)
        self.assertLess(ci_bad["lower"], ci_calm["lower"])
        # the bad session's own point estimate should be far below the calm one
        self.assertLess(ci_bad["point_estimate"], ci_calm["point_estimate"])


# --- summarize_b1 ------------------------------------------------------------

def _filled_row(session, net_pnl, trigger="MSS", in_window=True, friction_share=None):
    return {
        "session": session, "net_pnl": net_pnl, "gross_pnl": net_pnl,
        "contract_gate": GATE_PASS, "exit_reason": "TARGET",
        "heff_smc_trigger": trigger, "heff_smc_in_charter_window": in_window,
        "friction_detail": {"friction_share_of_target": friction_share} if friction_share is not None else None,
    }


def _no_contract_row(session, trigger="MSS"):
    return {"session": session, "net_pnl": None, "contract_gate": GATE_FAIL, "exit_reason": "NO_CONTRACT",
            "heff_smc_trigger": trigger, "heff_smc_in_charter_window": True}


class SummarizeB1Tests(unittest.TestCase):
    def test_hand_checked_counts_and_expectancy(self):
        rows = [
            _filled_row("2026-05-04", 100.0, trigger="MSS", friction_share=0.1),
            _filled_row("2026-05-04", -40.0, trigger="BOS", friction_share=0.2),
            _filled_row("2026-05-05", 30.0, trigger="MSS"),
            _no_contract_row("2026-05-05"),
        ]
        summary = b1.summarize_b1(rows)
        self.assertEqual(summary["n_total_signals"], 4)
        self.assertEqual(summary["n_no_contract"], 1)
        self.assertEqual(summary["n_filled"], 3)
        self.assertEqual(summary["n_sessions_with_signal"], 2)
        self.assertEqual(summary["n_sessions_with_fill"], 2)
        self.assertAlmostEqual(summary["net_expectancy_per_filled_trade"], (100 - 40 + 30) / 3, places=2)
        self.assertAlmostEqual(summary["win_rate"], 2 / 3, places=4)
        self.assertAlmostEqual(summary["avg_friction_share_of_target"], 0.15, places=4)
        self.assertIsNotNone(summary["net_expectancy_session_bootstrap_95ci"])
        # rows[0] (filled), rows[2] (filled), rows[3] (no-contract) are all MSS
        self.assertEqual(summary["trigger_breakdown"]["MSS"]["n_signals"], 3)
        self.assertEqual(summary["trigger_breakdown"]["MSS"]["n_filled"], 2)
        self.assertEqual(summary["trigger_breakdown"]["BOS"]["n_filled"], 1)

    def test_ci_none_with_fewer_than_two_sessions_with_fills(self):
        rows = [_filled_row("2026-05-04", 100.0), _no_contract_row("2026-05-05")]
        summary = b1.summarize_b1(rows)
        self.assertEqual(summary["n_sessions_with_fill"], 1)
        self.assertIsNone(summary["net_expectancy_session_bootstrap_95ci"])

    def test_robustness_excludes_the_real_top_5_sessions(self):
        rows = [_filled_row(f"s{i}", 100.0) for i in range(5)] + [_filled_row("s5", -10.0), _filled_row("s6", -20.0)]
        summary = b1.summarize_b1(rows)
        self.assertAlmostEqual(summary["robustness_expectancy_excluding_best_5_sessions"], -15.0, places=2)

    def test_charter_window_split(self):
        rows = [
            _filled_row("2026-05-04", 50.0, in_window=True),
            _filled_row("2026-05-04", -10.0, in_window=False),
        ]
        summary = b1.summarize_b1(rows)
        self.assertEqual(summary["n_signals_in_charter_10_1530_window"], 1)
        self.assertAlmostEqual(summary["net_expectancy_in_charter_window_only"], 50.0, places=2)

    def test_all_no_trade_produces_no_crash_and_null_metrics(self):
        rows = [_no_contract_row("2026-05-04"), _no_contract_row("2026-05-05")]
        summary = b1.summarize_b1(rows)
        self.assertEqual(summary["n_filled"], 0)
        self.assertIsNone(summary["net_expectancy_per_filled_trade"])
        self.assertIsNone(summary["net_expectancy_session_bootstrap_95ci"])

    def test_label_marks_this_as_not_promotion_evaluated(self):
        summary = b1.summarize_b1([_filled_row("s1", 1.0), _filled_row("s2", 2.0)])
        self.assertEqual(summary["label"], b1.LABEL_B1)
        self.assertIn("NOT_PROMOTION_EVALUATED", summary["label"])


class RenderSummaryMdTests(unittest.TestCase):
    def test_renders_without_error_on_full_and_empty_summaries(self):
        full = b1.summarize_b1([_filled_row("s1", 100.0), _filled_row("s2", -40.0), _no_contract_row("s3")])
        empty = b1.summarize_b1([_no_contract_row("s1")])
        text_full = b1.render_summary_md(full)
        text_empty = b1.render_summary_md(empty)
        self.assertIn("NOT evaluated against BT0_CHARTER.md Section 5", text_full)
        self.assertIn("NOT evaluated against BT0_CHARTER.md Section 5", text_empty)
        self.assertIn("Sessions with at least one signal: 3", text_full)
        self.assertNotIn("/ 62", text_full)


# --- Ledger writer isolation --------------------------------------------

class LedgerWriterIsolationTests(unittest.TestCase):
    def test_ledger_write_uses_explicit_tmp_path(self):
        from thetadata_pipeline.bt2_simulator import append_ledger_rows
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bt3_b1_ledger.json"
            sig = dict(symbol="QQQ", session=SESSION, decision_ts=DECISION_TS, direction="CALL WATCH",
                       trigger="MSS", score=5.5, bar_index=1, in_charter_window=True)
            row = b1.simulate_b1_signal(sig, pd.DataFrame(), {})
            append_ledger_rows([row], path=path)
            on_disk = json.loads(path.read_text())
            self.assertEqual(len(on_disk["trades"]), 1)
            self.assertNotEqual(path, b1.BT3_B1_LEDGER_PATH)


    def test_run_b1_replaces_an_old_ledger_instead_of_appending_duplicates(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "ledger.json"
            ledger.write_text(json.dumps({"trades": [{"trade_id": "old"}]}))
            new_rows = [{"trade_id": "new"}]
            with patch.object(b1, "load_triangle_events", return_value=[_event(SESSION, "long", "10:15:00")]):
                with patch.object(b1, "_free_ram_mb", return_value=10_000):
                    with patch.object(b1, "_load_raw_session_trades", return_value=pd.DataFrame()):
                        with patch.object(b1, "simulate_b1_session", return_value=new_rows):
                            rows = b1.run_b1(raw_dir=Path(tmp), backfill_dir=Path(tmp), ledger_path=ledger)
            payload = json.loads(ledger.read_text())
            self.assertEqual(rows, new_rows)
            self.assertEqual(payload["trades"], new_rows)
            self.assertNotIn({"trade_id": "old"}, payload["trades"])

    def test_low_memory_abort_preserves_existing_complete_ledger(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            events_path = root / "events.json"
            ledger_path = root / "ledger.json"
            original = {"schema_version": b1.SCHEMA_VERSION, "trades": [{"sentinel": "complete"}]}
            ledger_path.write_text(json.dumps(original))
            events_path.write_text(json.dumps({"events": [_event(SESSION, "long", "10:15:00")]}))

            with patch.object(b1, "_free_ram_mb", return_value=b1.MIN_FREE_RAM_MB - 1), \
                 patch.object(b1, "_load_raw_session_trades") as load_trades:
                with self.assertRaisesRegex(RuntimeError, "refusing to overwrite"):
                    b1.run_b1(
                        events_path=events_path,
                        raw_dir=root / "raw",
                        backfill_dir=root / "backfill",
                        ledger_path=ledger_path,
                    )

            self.assertEqual(json.loads(ledger_path.read_text()), original)
            load_trades.assert_not_called()

if __name__ == "__main__":
    unittest.main()
