#!/usr/bin/env python3
"""Integration regressions for ORB evaluator wiring around the shared model."""
import datetime as dt
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

import pandas as pd

import orb_paper_eval as orb
from paper_execution_model import existing_trade_ids, terminal_ids


def bars(rows):
    index = [pd.Timestamp(f"2026-07-31T{clock}:00") for clock, *_ in rows]
    data = [
        {"Open": close, "High": high, "Low": low, "Close": close, "Volume": 1000}
        for _, high, low, close in rows
    ]
    return pd.DataFrame(data, index=pd.DatetimeIndex(index))


class OrbEvaluatorIntegrationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.directory = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_append_csv_uses_the_real_orb_ledger_path(self):
        path = self.directory / "orb.csv"
        row = {"trade_id": "ORB:AAA:2026-07-31", "strategy": "ORB"}
        with mock.patch.object(orb, "CSV_PATH", path):
            self.assertEqual(orb.append_csv([row]), 1)
            self.assertEqual(orb.append_csv([row]), 0)
        self.assertEqual(existing_trade_ids(path), {row["trade_id"]})

    def test_main_serializes_state_and_writes_atomically(self):
        events = []

        @contextmanager
        def locked(path):
            events.append(("lock_enter", path))
            yield
            events.append(("lock_exit", path))

        def ingest(today, state):
            events.append(("ingest", today))

        def evaluate(today, state, eod=False):
            events.append(("evaluate", eod))
            return []

        def atomic(path, value):
            events.append(("atomic", path, value))

        with mock.patch.object(sys, "argv", ["orb_paper_eval.py"]), \
                mock.patch.object(orb, "evaluator_lock", locked), \
                mock.patch.object(orb, "load_open", return_value={}), \
                mock.patch.object(orb, "ingest", ingest), \
                mock.patch.object(orb, "evaluate", evaluate), \
                mock.patch.object(orb, "atomic_write_json", atomic), \
                mock.patch.object(orb, "OPEN_PATH", self.directory / "open.json"):
            orb.main()

        self.assertEqual(
            [event[0] for event in events],
            ["lock_enter", "ingest", "evaluate", "atomic", "lock_exit"],
        )

    def test_detected_at_prevents_predetection_phantom_fill(self):
        frame = bars([
            ("09:50", 100.5, 99.5, 100.1),
            ("10:00", 102.0, 101.0, 101.5),
        ])
        trade_id = "ORB:AAA:2026-07-31"
        state = {
            trade_id: {
                "trade_id": trade_id,
                "strategy": "ORB",
                "ticker": "AAA",
                "side": "LONG",
                "entry": 100.0,
                "stop": 99.0,
                "target": None,
                "entry_time": "2026-07-31T09:45:00",
                "detected_at": "2026-07-31T09:58:00",
            }
        }
        terminal = self.directory / "terminal.jsonl"
        with mock.patch.object(orb, "today_5m", return_value=frame), \
                mock.patch.object(orb, "TERMINAL_PATH", terminal):
            closed = orb.evaluate(dt.date(2026, 7, 31), state, eod=True)
        self.assertEqual(closed, [])
        self.assertEqual(state, {})
        self.assertIn(trade_id, terminal_ids(terminal))


if __name__ == "__main__":
    unittest.main(verbosity=2)
