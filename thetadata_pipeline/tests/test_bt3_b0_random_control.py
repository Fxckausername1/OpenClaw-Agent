"""BT-3 B0 random-control test suite. Mirrors test_bt2_simulator.py's own
convention (golden-path + adversarial/edge cases, hand-checked numbers, no
real network or real production files touched). Two groups:

1. Generator tests (GenerateSignalsTests, SampleDecisionTsTests,
   SampleDirectionTests, ListSessionsTests) -- exercise sample_decision_ts /
   sample_direction / generate_b0_signals / list_b0_sessions against
   synthetic, injected fixtures only. This is the "does it respect the
   session window, does it only pick times with real data, is it
   reproducible with a fixed seed" coverage the task asked for.

2. Pipeline + report tests (SimulateB0SignalTests, ReportMathTests) --
   exercise simulate_b0_signal() (which calls the REAL bt2_simulator.
   simulate_trade, unmodified) against synthetic trades_df/greeks fixtures
   in the same shape test_bt2_simulator.py's own GoldenPathTests use, and
   summarize_b0()/bootstrap_mean_ci() against small hand-checkable ledgers.

All ledger-writer / manifest-reading tests use tmp paths -- never the real
BT3_B0_LEDGER_PATH or BACKFILL_MANIFEST_PATH defaults, same discipline
test_bt2_simulator.py's own LedgerWriterTests docstring documents (the real
incident it guards against: a test once wrote to a real production path by
omission).
"""

import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

import thetadata_pipeline.bt3_b0_random_control as b0
from thetadata_pipeline.bt2_schemas import GATE_FAIL, GATE_PASS, VALID_DIRECTION_GATES, validate_ledger_row
from thetadata_pipeline.schemas import contract_id as build_cid

ET = b0.ET


def _ts(date_str: str, hh: int, mm: int, ss: int = 0) -> pd.Timestamp:
    return pd.Timestamp(f"{date_str} {hh:02d}:{mm:02d}:{ss:02d}", tz=ET)


class SessionWindowBoundsTests(unittest.TestCase):
    def test_bounds_are_1000_to_1530_et(self):
        start, end = b0.session_window_bounds(dt.date(2026, 5, 4))
        self.assertEqual(start, _ts("2026-05-04", 10, 0))
        self.assertEqual(end, _ts("2026-05-04", 15, 30))


class SampleDecisionTsTests(unittest.TestCase):
    """Core coverage the task asked for: window respect, real-data-only
    timestamps, reproducibility."""

    def setUp(self):
        self.date = dt.date(2026, 5, 4)
        self.in_window = [
            _ts("2026-05-04", 10, 0, 5), _ts("2026-05-04", 11, 17, 30),
            _ts("2026-05-04", 12, 45, 0), _ts("2026-05-04", 14, 59, 59),
            _ts("2026-05-04", 15, 29, 58),
        ]
        self.out_of_window = [
            _ts("2026-05-04", 9, 45, 0),   # premarket, before 10:00
            _ts("2026-05-04", 15, 45, 0),  # after 15:30
            _ts("2026-05-04", 9, 30, 0),   # open, before 10:00
        ]

    def test_result_is_always_within_window(self):
        rng = np.random.default_rng(1)
        pool = self.in_window + self.out_of_window
        for _ in range(50):
            result = b0.sample_decision_ts(pool, self.date, rng)
            self.assertIsNotNone(result)
            self.assertGreaterEqual(result, _ts("2026-05-04", 10, 0))
            self.assertLess(result, _ts("2026-05-04", 15, 30))

    def test_result_is_always_one_of_the_real_provided_timestamps(self):
        """Never fabricates a decision_ts that has no underlying tick behind
        it -- must be a value literally drawn from the in_window set."""
        rng = np.random.default_rng(2)
        pool = self.in_window + self.out_of_window
        for _ in range(50):
            result = b0.sample_decision_ts(pool, self.date, rng)
            self.assertIn(result, self.in_window)

    def test_out_of_window_timestamps_are_never_selectable(self):
        rng = np.random.default_rng(3)
        pool = self.in_window + self.out_of_window
        seen = set()
        for _ in range(200):
            seen.add(b0.sample_decision_ts(pool, self.date, rng))
        self.assertTrue(seen.issubset(set(self.in_window)))
        self.assertFalse(seen & set(self.out_of_window))

    def test_returns_none_when_nothing_in_window(self):
        rng = np.random.default_rng(4)
        result = b0.sample_decision_ts(self.out_of_window, self.date, rng)
        self.assertIsNone(result)

    def test_returns_none_on_empty_input(self):
        rng = np.random.default_rng(5)
        self.assertIsNone(b0.sample_decision_ts([], self.date, rng))
        self.assertIsNone(b0.sample_decision_ts(None, self.date, rng))

    def test_reproducible_with_fixed_seed(self):
        pool = self.in_window + self.out_of_window
        rng_a = np.random.default_rng(777)
        rng_b = np.random.default_rng(777)
        results_a = [b0.sample_decision_ts(pool, self.date, rng_a) for _ in range(20)]
        results_b = [b0.sample_decision_ts(pool, self.date, rng_b) for _ in range(20)]
        self.assertEqual(results_a, results_b)

    def test_accepts_a_pandas_series_input_too(self):
        rng = np.random.default_rng(9)
        series = pd.Series(self.in_window)
        result = b0.sample_decision_ts(series, self.date, rng)
        self.assertIn(result, self.in_window)


class SampleDirectionTests(unittest.TestCase):
    def test_always_a_valid_direction(self):
        rng = np.random.default_rng(11)
        for _ in range(50):
            self.assertIn(b0.sample_direction(rng), VALID_DIRECTION_GATES)

    def test_reproducible_with_fixed_seed(self):
        rng_a = np.random.default_rng(42)
        rng_b = np.random.default_rng(42)
        seq_a = [b0.sample_direction(rng_a) for _ in range(30)]
        seq_b = [b0.sample_direction(rng_b) for _ in range(30)]
        self.assertEqual(seq_a, seq_b)

    def test_both_directions_actually_occur(self):
        rng = np.random.default_rng(123)
        seq = {b0.sample_direction(rng) for _ in range(100)}
        self.assertEqual(seq, set(VALID_DIRECTION_GATES))


class GenerateSignalsTests(unittest.TestCase):
    """Exercises generate_b0_signals with an injected fake timestamp_loader
    -- no disk I/O, so this is real unit coverage of the orchestration
    logic itself (per-session isolation, reproducibility, data-gap
    flagging), independent of the real backfill being present."""

    def setUp(self):
        self.sessions = [
            {"symbol": "SPY", "date": "2026-05-04", "quality_grade": "PASS"},
            {"symbol": "QQQ", "date": "2026-05-04", "quality_grade": "PASS"},
            {"symbol": "SPY", "date": "2026-05-05", "quality_grade": "PARTIAL"},
        ]
        self.pools = {
            ("SPY", "2026-05-04"): [_ts("2026-05-04", 10, 5), _ts("2026-05-04", 13, 0)],
            ("QQQ", "2026-05-04"): [_ts("2026-05-04", 11, 0), _ts("2026-05-04", 14, 30)],
            ("SPY", "2026-05-05"): [_ts("2026-05-05", 12, 0)],
        }

    def _loader(self, symbol, date):
        return self.pools[(symbol, date)]

    def test_one_signal_per_session_in_order(self):
        signals = b0.generate_b0_signals(self.sessions, self._loader, seed=1)
        self.assertEqual(len(signals), 3)
        self.assertEqual([(s["symbol"], s["session"]) for s in signals],
                          [("SPY", "2026-05-04"), ("QQQ", "2026-05-04"), ("SPY", "2026-05-05")])

    def test_each_signal_only_draws_from_its_own_session_pool(self):
        """A real bug this guards against: leaking another session's
        timestamps into this session's draw."""
        signals = b0.generate_b0_signals(self.sessions, self._loader, seed=2)
        for sig in signals:
            pool = self.pools[(sig["symbol"], sig["session"])]
            self.assertIn(sig["decision_ts"], pool)

    def test_reproducible_with_fixed_seed(self):
        signals_a = b0.generate_b0_signals(self.sessions, self._loader, seed=555)
        signals_b = b0.generate_b0_signals(self.sessions, self._loader, seed=555)
        self.assertEqual(signals_a, signals_b)

    def test_different_seed_can_change_output(self):
        signals_a = b0.generate_b0_signals(self.sessions, self._loader, seed=1)
        signals_b = b0.generate_b0_signals(self.sessions, self._loader, seed=2)
        self.assertNotEqual(
            [(s["decision_ts"], s["direction"]) for s in signals_a],
            [(s["decision_ts"], s["direction"]) for s in signals_b],
        )

    def test_data_gap_session_flagged_and_still_gets_a_direction(self):
        pools = dict(self.pools)
        pools[("SPY", "2026-05-05")] = []  # no data at all this session
        signals = b0.generate_b0_signals(self.sessions, lambda sym, d: pools[(sym, d)], seed=3)
        gap_signal = signals[-1]
        self.assertTrue(gap_signal["data_gap"])
        self.assertIsNone(gap_signal["decision_ts"])
        self.assertIn(gap_signal["direction"], VALID_DIRECTION_GATES)


class ListSessionsTests(unittest.TestCase):
    def test_sorted_by_date_then_symbol_and_grade_preserved(self):
        payload = {
            "sessions": [
                {"symbol": "SPY", "date": "2026-05-05", "quality_grade": "PASS"},
                {"symbol": "QQQ", "date": "2026-05-04", "quality_grade": "PARTIAL"},
                {"symbol": "SPY", "date": "2026-05-04", "quality_grade": "PASS"},
                {"symbol": "QQQ", "date": "2026-05-05", "quality_grade": "PASS"},
            ]
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "manifest.json"
            path.write_text(json.dumps(payload))
            sessions = b0.list_b0_sessions(path)
        self.assertEqual(
            [(s["date"], s["symbol"]) for s in sessions],
            [("2026-05-04", "QQQ"), ("2026-05-04", "SPY"), ("2026-05-05", "QQQ"), ("2026-05-05", "SPY")],
        )
        self.assertEqual(sessions[0]["quality_grade"], "PARTIAL")


# --- Feeding a signal through the real BT-2 pipeline --------------------

SESSION = "2026-07-24"
SESSION_DATE = dt.date(2026, 7, 24)
DECISION_TS = pd.Timestamp("2026-07-24 14:00:00", tz="UTC")  # 10:00 ET
CALL_CID = build_cid("SPY", SESSION_DATE, 745.0, "C")


def _row(cid, expiration, strike, right, ts, bid, ask, bid_size=20, ask_size=20):
    return dict(contract_id=cid, expiration=expiration, strike=strike, right=right,
                trade_timestamp=ts, bid=bid, ask=ask, bid_size=bid_size, ask_size=ask_size)


def _bullish_trades():
    return pd.DataFrame([
        _row(CALL_CID, "2026-07-24", 745.0, "C", DECISION_TS - pd.Timedelta(seconds=2), 0.24, 0.26),
        _row(CALL_CID, "2026-07-24", 745.0, "C", DECISION_TS + pd.Timedelta(seconds=5), 0.24, 0.26),
        _row(CALL_CID, "2026-07-24", 745.0, "C", DECISION_TS + pd.Timedelta(minutes=5), 0.40, 0.41),
        _row(CALL_CID, "2026-07-24", 745.0, "C", DECISION_TS + pd.Timedelta(minutes=5, seconds=4), 0.42, 0.44),
    ])


class SimulateB0SignalTests(unittest.TestCase):
    """Proves simulate_b0_signal() genuinely calls the real bt2_simulator
    pipeline (same P&L arithmetic test_bt2_simulator.py's GoldenPathTests
    hand-checks), and that every outcome (filled, NO_CONTRACT, NO_FILL,
    no-eligible-timestamp) produces a schema-clean ledger row, never a
    silently dropped signal."""

    def test_filled_trade_matches_real_pipeline_arithmetic(self):
        sig = {"symbol": "SPY", "session": SESSION, "decision_ts": DECISION_TS, "direction": "CALL WATCH", "data_gap": False}
        row = b0.simulate_b0_signal(sig, _bullish_trades(), greeks={CALL_CID: {"delta": 0.35}})
        self.assertEqual(validate_ledger_row(row), [])
        self.assertEqual(row["contract_gate"], GATE_PASS)
        self.assertAlmostEqual(row["gross_pnl"], 18.00, places=2)
        # CORRECTED 2026-07-31: net P&L now reconciles directly to
        # (exit_fill - entry_fill) * qty * 100 - dollar_fees. The old 17.86 was
        # produced by subtracting PREMIUM-unit slippage from a DOLLAR-denominated
        # midpoint P&L, understating execution cost by qty*100/2 ($1.96 here).
        self.assertAlmostEqual(row["net_pnl"], 15.90, places=2)
        self.assertIn(b0.FLAG_B0_RANDOM_CONTROL, row["rule_flags"])
        self.assertEqual(row["experiment_id"], b0.EXPERIMENT_ID)
        self.assertEqual(row["invalidation"], None)  # B0 has no real invalidation level

    def test_no_contract_outcome_is_a_clean_ledger_row(self):
        trades = pd.DataFrame([_row(CALL_CID, "2026-07-24", 745.0, "C",
                                     DECISION_TS - pd.Timedelta(seconds=2), 0.60, 0.90)])
        sig = {"symbol": "SPY", "session": SESSION, "decision_ts": DECISION_TS, "direction": "CALL WATCH", "data_gap": False}
        row = b0.simulate_b0_signal(sig, trades, greeks={CALL_CID: {"delta": 0.55}})
        self.assertEqual(row["contract_gate"], GATE_FAIL)
        self.assertEqual(row["exit_reason"], "NO_CONTRACT")
        self.assertIsNone(row["net_pnl"])
        self.assertEqual(validate_ledger_row(row), [])
        self.assertIn(b0.FLAG_B0_RANDOM_CONTROL, row["rule_flags"])

    def test_no_eligible_timestamp_outcome_never_calls_the_real_simulator(self):
        sig = {"symbol": "SPY", "session": SESSION, "decision_ts": None, "direction": "PUT WATCH", "data_gap": True}
        row = b0.simulate_b0_signal(sig, pd.DataFrame(), {})
        self.assertEqual(row["exit_reason"], b0.EXIT_REASON_NO_SIGNAL_TIME)
        self.assertEqual(row["data_quality"], b0.DATA_QUALITY_NO_ELIGIBLE_TIMESTAMP)
        self.assertIsNone(row["contract_id"])
        self.assertIsNone(row["net_pnl"])
        self.assertEqual(validate_ledger_row(row), [])
        self.assertIn(b0.FLAG_B0_RANDOM_CONTROL, row["rule_flags"])

    def test_context_snapshot_id_names_b0_not_a_real_catalyst_brief(self):
        sig = {"symbol": "QQQ", "session": SESSION, "decision_ts": DECISION_TS, "direction": "CALL WATCH", "data_gap": False}
        row = b0.simulate_b0_signal(sig, pd.DataFrame(), {})
        self.assertTrue(row["context_snapshot_id"].startswith("B0-RANDOM-CONTROL:"))


# --- Report math -----------------------------------------------------

class BootstrapCiTests(unittest.TestCase):
    def test_none_below_two_observations(self):
        self.assertIsNone(b0.bootstrap_mean_ci([]))
        self.assertIsNone(b0.bootstrap_mean_ci([5.0]))

    def test_reproducible_with_fixed_seed(self):
        values = [10.0, -5.0, 20.0, -8.0, 3.0]
        ci_a = b0.bootstrap_mean_ci(values, seed=99)
        ci_b = b0.bootstrap_mean_ci(values, seed=99)
        self.assertEqual(ci_a, ci_b)

    def test_point_estimate_is_the_real_mean(self):
        values = [10.0, -5.0, 20.0, -8.0, 3.0]
        ci = b0.bootstrap_mean_ci(values, seed=1)
        self.assertAlmostEqual(ci["point_estimate"], sum(values) / len(values), places=4)

    def test_bounds_bracket_the_point_estimate_reasonably(self):
        values = [10.0, -5.0, 20.0, -8.0, 3.0, 15.0, -2.0]
        ci = b0.bootstrap_mean_ci(values, seed=7)
        self.assertLessEqual(ci["lower"], ci["point_estimate"])
        self.assertGreaterEqual(ci["upper"], ci["point_estimate"])


class ProfitFactorTests(unittest.TestCase):
    def test_normal_case(self):
        self.assertAlmostEqual(b0._profit_factor([10.0, -5.0, 20.0, -8.0]), 30.0 / 13.0, places=4)

    def test_none_when_no_losses(self):
        self.assertIsNone(b0._profit_factor([10.0, 5.0]))


def _filled_row(net_pnl, gross_pnl=None, friction_share=None):
    return {
        "net_pnl": net_pnl, "gross_pnl": gross_pnl if gross_pnl is not None else net_pnl,
        "contract_gate": GATE_PASS, "exit_reason": "TARGET",
        "friction_detail": {"friction_share_of_target": friction_share} if friction_share is not None else None,
    }


def _no_contract_row():
    return {"net_pnl": None, "contract_gate": GATE_FAIL, "exit_reason": "NO_CONTRACT"}


def _no_fill_row():
    return {"net_pnl": None, "contract_gate": GATE_PASS, "exit_reason": "NO_FILL"}


def _no_time_row():
    return {"net_pnl": None, "contract_gate": GATE_FAIL, "exit_reason": b0.EXIT_REASON_NO_SIGNAL_TIME}


class SummarizeB0Tests(unittest.TestCase):
    def test_hand_checked_counts_and_expectancy(self):
        rows = [
            _filled_row(100.0, friction_share=0.1),
            _filled_row(-40.0, friction_share=0.2),
            _no_contract_row(),
            _no_fill_row(),
            _no_time_row(),
        ]
        summary = b0.summarize_b0(rows)
        self.assertEqual(summary["n_total_draws"], 5)
        self.assertEqual(summary["n_no_eligible_timestamp"], 1)
        self.assertEqual(summary["n_no_contract"], 1)
        self.assertEqual(summary["n_contract_pass_no_fill"], 1)
        self.assertEqual(summary["n_filled"], 2)
        self.assertAlmostEqual(summary["net_expectancy_per_filled_trade"], 30.0, places=2)  # (100-40)/2
        self.assertAlmostEqual(summary["win_rate"], 0.5, places=4)
        self.assertAlmostEqual(summary["profit_factor"], 2.5, places=4)  # 100/40
        self.assertAlmostEqual(summary["avg_friction_share_of_target"], 0.15, places=4)
        self.assertEqual(summary["n_friction_share_observations"], 2)
        # (100 - 40 + 0 + 0 + 0) / 5 = 12.0
        self.assertAlmostEqual(summary["expectancy_all_draws_including_no_trade_as_zero"], 12.0, places=2)
        self.assertIsNotNone(summary["net_expectancy_bootstrap_95ci"])  # n_filled == 2

    def test_ci_is_none_with_fewer_than_two_filled_trades(self):
        rows = [_filled_row(100.0), _no_contract_row(), _no_fill_row()]
        summary = b0.summarize_b0(rows)
        self.assertEqual(summary["n_filled"], 1)
        self.assertIsNone(summary["net_expectancy_bootstrap_95ci"])

    def test_all_no_trade_produces_no_crash_and_null_metrics(self):
        rows = [_no_contract_row(), _no_fill_row(), _no_time_row()]
        summary = b0.summarize_b0(rows)
        self.assertEqual(summary["n_filled"], 0)
        self.assertIsNone(summary["net_expectancy_per_filled_trade"])
        self.assertIsNone(summary["win_rate"])
        self.assertIsNone(summary["profit_factor"])
        self.assertIsNone(summary["net_expectancy_bootstrap_95ci"])
        self.assertAlmostEqual(summary["expectancy_all_draws_including_no_trade_as_zero"], 0.0, places=4)

    def test_label_marks_this_as_a_null_baseline_not_a_strategy_result(self):
        summary = b0.summarize_b0([_filled_row(1.0), _filled_row(2.0)])
        self.assertEqual(summary["label"], b0.LABEL_NULL_BASELINE)
        self.assertIn("NOT_A_STRATEGY_RESULT", summary["label"])


class RenderSummaryMdTests(unittest.TestCase):
    def test_renders_without_error_on_full_and_empty_summaries(self):
        full = b0.summarize_b0([_filled_row(100.0), _filled_row(-40.0), _no_contract_row()])
        empty = b0.summarize_b0([_no_contract_row(), _no_fill_row()])
        text_full = b0.render_summary_md(full)
        text_empty = b0.render_summary_md(empty)
        self.assertIn("NULL-HYPOTHESIS REFERENCE POINT, NOT A STRATEGY RESULT", text_full)
        self.assertIn("NULL-HYPOTHESIS REFERENCE POINT, NOT A STRATEGY RESULT", text_empty)
        self.assertIn("N/A (no filled trades)", text_empty)


# --- Ledger writer isolation --------------------------------------------

class LedgerWriterIsolationTests(unittest.TestCase):
    """Never the real BT3_B0_LEDGER_PATH default -- always an explicit tmp
    path, same discipline as test_bt2_simulator.py's LedgerWriterTests."""

    def test_run_b0_style_ledger_write_uses_explicit_tmp_path(self):
        from thetadata_pipeline.bt2_simulator import append_ledger_rows
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bt3_b0_control_ledger.json"
            sig = {"symbol": "SPY", "session": SESSION, "decision_ts": None, "direction": "CALL WATCH", "data_gap": True}
            row = b0.simulate_b0_signal(sig, pd.DataFrame(), {})
            append_ledger_rows([row], path=path)
            on_disk = json.loads(path.read_text())
            self.assertEqual(len(on_disk["trades"]), 1)
            self.assertNotEqual(path, b0.BT3_B0_LEDGER_PATH)


if __name__ == "__main__":
    unittest.main()
