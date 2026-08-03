import datetime as dt
import json
import math
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

from thetadata_pipeline import snapshot as snap


class SanitizeForJsonTests(unittest.TestCase):
    def test_numpy_scalars_become_native(self):
        out = snap.sanitize_for_json({
            "a": np.int64(5), "b": np.float64(1.5), "c": np.bool_(True),
        })
        self.assertEqual(out, {"a": 5, "b": 1.5, "c": True})
        self.assertIsInstance(out["a"], int)
        self.assertIsInstance(out["b"], float)
        self.assertIsInstance(out["c"], bool)

    def test_nan_and_inf_become_none(self):
        out = snap.sanitize_for_json({"a": float("nan"), "b": float("inf"), "c": np.nan})
        self.assertIsNone(out["a"])
        self.assertIsNone(out["b"])
        self.assertIsNone(out["c"])

    def test_timestamps_become_isoformat_strings(self):
        out = snap.sanitize_for_json({"t": pd.Timestamp("2026-07-24 10:00:00"), "d": dt.date(2026, 7, 24)})
        self.assertEqual(out["t"], "2026-07-24T10:00:00")
        self.assertEqual(out["d"], "2026-07-24")

    def test_nested_structures(self):
        out = snap.sanitize_for_json({"rows": [{"v": np.int64(1)}, {"v": np.float64(float("nan"))}]})
        self.assertEqual(out["rows"][0]["v"], 1)
        self.assertIsNone(out["rows"][1]["v"])

    def test_produces_valid_json_end_to_end(self):
        payload = {"a": np.int64(5), "b": np.nan, "rows": [{"x": np.bool_(False)}]}
        text = json.dumps(snap.sanitize_for_json(payload))
        reparsed = json.loads(text)
        self.assertEqual(reparsed["a"], 5)
        self.assertIsNone(reparsed["b"])


class AtomicWriteTests(unittest.TestCase):
    def test_write_and_read_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "out.json"
            snap._atomic_write_json(path, {"x": np.int64(3), "y": float("nan")})
            data = json.loads(path.read_text())
            self.assertEqual(data["x"], 3)
            self.assertIsNone(data["y"])

    def test_no_leftover_tmp_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "out.json"
            snap._atomic_write_json(path, {"x": 1})
            leftovers = [p for p in Path(d).iterdir() if p.name != "out.json"]
            self.assertEqual(leftovers, [])


class BuildSymbolSnapshotTests(unittest.TestCase):
    def test_degrades_honestly_with_no_trades(self):
        cycle_result = {
            "classified_trades": pd.DataFrame(),
            "universe": {"contracts": [], "expirations": [], "contract_count": 0, "spot": 700.0},
            "oi": {}, "delta": {},
            "health": {"quality": "DEGRADED"},
        }
        with patch("thetadata_pipeline.snapshot.get_spot_price", return_value=700.0), \
             patch("thetadata_pipeline.features.load_live_gex_row", return_value=None), \
             tempfile.TemporaryDirectory() as d:
            with patch.object(snap, "DATA", Path(d)):
                out = snap.build_symbol_snapshot("SPY", cycle_result, dt.date(2026, 7, 24))
        self.assertFalse(out["ghost_wall"])
        self.assertEqual(out["p_c"]["quality"], "UNAVAILABLE")
        self.assertEqual(out["options_cvd"]["delta_notional_flow"], None)


if __name__ == "__main__":
    unittest.main()
