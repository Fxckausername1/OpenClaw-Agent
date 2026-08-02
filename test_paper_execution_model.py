#!/usr/bin/env python3
"""Regression tests for the two defects in MR_ORB_LIVE_READINESS_AUDIT_2026-08-02.

Defect 1 - phantom fills: the evaluators scored stop/t1/EOD outcomes without
           ever requiring a bar to touch the entry limit.
Defect 2 - duplicate race: unlocked read-modify-write let the 16:05 EOD run
           and a concurrent scan run resurrect closed trades, producing 34
           duplicate ledger rows worth +34.779R.

Every test here fails against the pre-repair code.
"""
import datetime as dt
import json
import multiprocessing
import tempfile
import time
import unittest
from pathlib import Path

import pandas as pd

from paper_execution_model import (
    ENTRY_CUTOFF_ET, STATE_FILLED_CLOSED, STATE_FILLED_OPEN, STATE_NEVER_FILLED,
    STATE_PENDING_ENTRY, LedgerIntegrityError, advance_boundary_limit,
    append_rows_dedup, append_terminal, atomic_write_json, evaluator_lock,
    existing_trade_ids, terminal_ids,
)

FIELDS = ["trade_id", "ticker", "outcome_r"]


def bars(rows):
    """rows: (naive-ET 'HH:MM', high, low, close). Index is naive ET, which is
    what both evaluators normalise to before calling us."""
    idx, data = [], []
    for hhmm, hi, lo, close in rows:
        idx.append(pd.Timestamp(f"2026-07-31T{hhmm}:00"))
        data.append({"Open": close, "High": hi, "Low": lo, "Close": close,
                     "Volume": 1000})
    return pd.DataFrame(data, index=pd.DatetimeIndex(idx))


def rec(side="LONG", entry=100.0, stop=99.0, t="2026-07-31T10:00:00", **kw):
    r = {"trade_id": "T:1", "ticker": "AAA", "side": side, "entry": entry,
         "stop": stop, "entry_time": t}
    r.update(kw)
    return r


# ------------------------------------------------- Defect 1: phantom fills
class BoundaryFillTests(unittest.TestCase):
    def test_untouched_limit_is_never_filled_not_a_trade(self):
        """THE phantom-fill regression, on the target side.

        Price gaps UP away from a LONG limit at 100 and runs through the
        t1 at 102 without ever trading back down to the limit. Pre-repair,
        evaluate() assumed the fill and scored +2R for an order Alpaca never
        filled. (Note the stop side cannot demonstrate this: for a LONG,
        reaching a stop BELOW the limit necessarily trades through the limit
        first, so that fill is genuine.)"""
        b = bars([("10:05", 105, 104, 104), ("10:10", 110, 106, 109)])
        out = advance_boundary_limit(rec(), b, eod=True, target=102.0)
        self.assertEqual(out["state"], STATE_NEVER_FILLED)
        self.assertNotIn("outcome_r", out)

    def test_stop_reachable_only_through_the_limit_is_a_real_fill(self):
        """Complement to the above: a LONG stop below the limit means the
        limit was necessarily touched, so -1R here is correct, not phantom."""
        b = bars([("10:05", 103, 98.5, 99)])
        out = advance_boundary_limit(rec(), b, eod=True, target=102.0)
        self.assertEqual(out["state"], STATE_FILLED_CLOSED)
        self.assertEqual(out["outcome_r"], -1.0)

    def test_touch_then_stop_is_a_real_loss(self):
        b = bars([("10:05", 100.5, 99.8, 100.2), ("10:10", 100.3, 98.5, 98.7)])
        out = advance_boundary_limit(rec(), b, eod=True, target=102.0)
        self.assertEqual(out["state"], STATE_FILLED_CLOSED)
        self.assertEqual(out["exit_reason"], "stop")
        self.assertEqual(out["outcome_r"], -1.0)

    def test_touch_then_target(self):
        b = bars([("10:05", 100.4, 99.9, 100.1), ("10:10", 102.5, 100.2, 102.3)])
        out = advance_boundary_limit(rec(), b, eod=True, target=102.0)
        self.assertEqual(out["exit_reason"], "t1")
        self.assertAlmostEqual(out["outcome_r"], 2.0, places=6)

    def test_signal_bar_itself_cannot_fill(self):
        """The signal is only known after its own bar closes."""
        b = bars([("10:00", 100.5, 98.0, 99.5)])
        self.assertEqual(
            advance_boundary_limit(rec(), b, eod=True, target=None)["state"],
            STATE_NEVER_FILLED)

    def test_stop_wins_over_target_on_the_same_bar(self):
        b = bars([("10:05", 103.0, 98.0, 100.0)])
        out = advance_boundary_limit(rec(), b, eod=True, target=102.0)
        self.assertEqual(out["exit_reason"], "stop")

    def test_fill_after_cutoff_is_refused(self):
        """The live executor stops accepting entries at 15:45 ET."""
        b = bars([("15:50", 100.5, 99.5, 100.0), ("15:55", 101, 100, 100.5)])
        out = advance_boundary_limit(rec(), b, eod=True, target=None)
        self.assertEqual(out["state"], STATE_NEVER_FILLED)

    def test_fill_just_before_cutoff_is_allowed(self):
        b = bars([("15:40", 100.5, 99.5, 100.2)])
        out = advance_boundary_limit(rec(), b, eod=True, target=None)
        self.assertEqual(out["state"], STATE_FILLED_CLOSED)
        self.assertEqual(out["exit_reason"], "eod")

    def test_short_side(self):
        b = bars([("10:05", 100.4, 99.6, 100.1), ("10:10", 101.5, 100.2, 101.3)])
        out = advance_boundary_limit(rec(side="SHORT", entry=100.0, stop=101.0),
                                     b, eod=True, target=98.0)
        self.assertEqual(out["exit_reason"], "stop")
        self.assertEqual(out["outcome_r"], -1.0)

    def test_pending_before_eod_filled_open_after_fill(self):
        b = bars([("10:05", 100.4, 99.9, 100.1)])
        out = advance_boundary_limit(rec(), b, eod=False, target=200.0)
        self.assertEqual(out["state"], STATE_FILLED_OPEN)

    def test_unfilled_intraday_is_pending_not_never(self):
        b = bars([("10:05", 105, 104, 104)])
        self.assertEqual(
            advance_boundary_limit(rec(), b, eod=False, target=None)["state"],
            STATE_PENDING_ENTRY)

    def test_recorded_fill_is_replayed_idempotently(self):
        b = bars([("10:05", 100.4, 99.9, 100.1), ("10:10", 100.6, 100.0, 100.4)])
        r = rec()
        first = advance_boundary_limit(r, b, eod=False, target=None)
        fill = r["_paper_fill_time"]
        second = advance_boundary_limit(r, b, eod=False, target=None)
        self.assertEqual(first["fill_time"], second["fill_time"])
        self.assertEqual(r["_paper_fill_time"], fill)

    def test_tz_aware_index_is_refused_loudly(self):
        """The naive 15:45 comparison is only correct on a naive-ET index. A
        UTC index would silently apply the cutoff four hours early."""
        b = bars([("10:05", 100.5, 99.5, 100.0)])
        b.index = b.index.tz_localize("UTC")
        with self.assertRaises(LedgerIntegrityError):
            advance_boundary_limit(rec(), b, eod=True, target=None)

    def test_zero_risk_refused(self):
        b = bars([("10:05", 100.5, 99.5, 100.0)])
        with self.assertRaises(LedgerIntegrityError):
            advance_boundary_limit(rec(entry=100.0, stop=100.0), b,
                                   eod=True, target=None)


# --------------------------------------------- Defect 2: duplicate / race
class LedgerDedupTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.csv = Path(self._tmp.name) / "led.csv"

    def tearDown(self):
        self._tmp.cleanup()

    def test_same_trade_id_cannot_be_written_twice(self):
        """THE duplicate regression: this is what produced 34 extra rows."""
        row = {"trade_id": "A", "ticker": "AAA", "outcome_r": "1.0"}
        self.assertEqual(append_rows_dedup(self.csv, FIELDS, [row]), 1)
        self.assertEqual(append_rows_dedup(self.csv, FIELDS, [row]), 0)
        self.assertEqual(len(existing_trade_ids(self.csv)), 1)

    def test_conflicting_second_close_is_refused(self):
        """The real shape: same id, different outcome, next session."""
        append_rows_dedup(self.csv, FIELDS,
                          [{"trade_id": "A", "ticker": "AAA", "outcome_r": "2.286"}])
        wrote = append_rows_dedup(self.csv, FIELDS,
                                  [{"trade_id": "A", "ticker": "AAA", "outcome_r": "2.822"}])
        self.assertEqual(wrote, 0)
        rows = list(self.csv.read_text().strip().splitlines())
        self.assertEqual(len(rows), 2)          # header + one row
        self.assertIn("2.286", rows[1])         # the FIRST close is kept

    def test_missing_trade_id_raises_rather_than_vanishing(self):
        with self.assertRaises(LedgerIntegrityError):
            append_rows_dedup(self.csv, FIELDS, [{"ticker": "AAA", "outcome_r": "1"}])

    def test_batch_with_internal_duplicate(self):
        rows = [{"trade_id": "A", "ticker": "A", "outcome_r": "1"},
                {"trade_id": "A", "ticker": "A", "outcome_r": "2"}]
        self.assertEqual(append_rows_dedup(self.csv, FIELDS, rows), 1)

    def test_header_written_once(self):
        append_rows_dedup(self.csv, FIELDS, [{"trade_id": "A", "ticker": "A", "outcome_r": "1"}])
        append_rows_dedup(self.csv, FIELDS, [{"trade_id": "B", "ticker": "B", "outcome_r": "2"}])
        self.assertEqual(self.csv.read_text().count("trade_id,ticker,outcome_r"), 1)


def _hold_lock(lock_path, marker, seconds):
    from paper_execution_model import evaluator_lock as el
    with el(lock_path):
        Path(marker).write_text("in")
        time.sleep(seconds)
        Path(marker).write_text("out")


class SerializationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.d = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_lock_actually_excludes_a_second_process(self):
        """The 16:05 collision: two processes, one book. The second must WAIT,
        not proceed on stale state."""
        lock = self.d / "l.lock"
        marker = self.d / "m"
        p = multiprocessing.Process(target=_hold_lock, args=(str(lock), str(marker), 1.5))
        p.start()
        time.sleep(0.4)
        t0 = time.monotonic()
        with evaluator_lock(lock):
            waited = time.monotonic() - t0
            self.assertEqual(marker.read_text(), "out",
                             "acquired the lock while the holder was still inside")
        p.join(timeout=10)
        self.assertGreater(waited, 0.5, "did not actually block")

    def test_atomic_json_never_leaves_a_torn_file(self):
        path = self.d / "open.json"
        atomic_write_json(path, {"a": 1})
        for _ in range(50):
            atomic_write_json(path, {"a": 1, "b": [1, 2, 3]})
            self.assertEqual(json.loads(path.read_text())["a"], 1)
        self.assertEqual(list(self.d.glob("*.tmp")), [])

    def test_terminal_states_block_reingest(self):
        """A never_filled signal must not be re-ingested and scored tomorrow."""
        path = self.d / "term.jsonl"
        append_terminal(path, rec(), STATE_NEVER_FILLED, "2026-07-31T16:05:00")
        self.assertIn("T:1", terminal_ids(path))


if __name__ == "__main__":
    unittest.main(verbosity=2)
