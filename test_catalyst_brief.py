import importlib.util
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("catalyst_brief.py")
SPEC = importlib.util.spec_from_file_location("catalyst_brief", MODULE_PATH)
cb = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cb)


class CatalystBriefTests(unittest.TestCase):
    def test_summarize_bars_builds_opening_range(self):
        day = date(2026, 7, 22)
        summary = cb.summarize_bars(cb.fixture_bars(day)["SPY"], day)
        self.assertEqual(summary["bars"], 20)
        self.assertLess(summary["or_low"], summary["or_high"])
        self.assertIn(summary["location"], {"above_range", "below_range", "inside_range"})

    def test_posture_requires_alignment(self):
        self.assertEqual(cb.index_posture({"available": True, "regime": "negative"}, {"available": True, "regime": "negative"}), "expansion")
        self.assertEqual(cb.index_posture({"available": True, "regime": "positive"}, {"available": True, "regime": "positive"}), "pinning")
        self.assertEqual(cb.index_posture({"available": True, "regime": "positive"}, {"available": True, "regime": "negative"}), "mixed")

    def test_central_posture(self):
        label, compat = cb.central_posture({"posture": "expansion"}, {"posture": "expansion"})
        self.assertEqual(label, "CONFIRMED EXPANSION")
        self.assertEqual(compat, "ORB-COMPATIBLE")

    def test_question_and_scenarios_are_fixed(self):
        idx = {"ticker": "SPY", "posture": "pinning", "bars": {"or_low": 1, "or_high": 2, "vwap": 1.5}}
        qqq = {"ticker": "QQQ", "posture": "pinning", "bars": {"or_low": 3, "or_high": 4, "vwap": 3.5}}
        question = cb.build_question("CONFIRMED PINNING", idx, qqq)
        self.assertIn("positive-gamma", question)
        self.assertEqual(len(cb.build_scenarios("CONFIRMED PINNING", idx, qqq)), 3)

    def test_dynamic_question_calls_out_nonconfirmation(self):
        spy = {
            "ticker": "SPY", "posture": "expansion",
            "bars": {"location": "above_range", "or_low": 100, "or_high": 102, "vwap": 101},
            "zero_dte": {"put_wall": 99, "call_wall": 104},
        }
        qqq = {
            "ticker": "QQQ", "posture": "expansion",
            "bars": {"location": "inside_range", "or_low": 200, "or_high": 204, "vwap": 202},
            "zero_dte": {"put_wall": 198, "call_wall": 206},
        }
        question = cb.build_question("CONFIRMED EXPANSION", spy, qqq)
        self.assertIn("QQQ non-confirmation", question)
        read = cb.build_daily_read(
            "CONFIRMED EXPANSION", spy, qqq,
            {"bullets": ["0DTE GEX changed materially."]},
            [{"topic": "energy_geopolitics", "headline": "Oil risk", "cross_asset_confirmation": "USO confirms."}],
            {"quadrant": "Lagging", "rs_mom": -1.0},
        )
        self.assertIn("one-index lead", read["working_conclusion"])
        self.assertIn("Oil risk", read["constraint"])

    def test_change_fingerprint_uses_prior_score_and_largest_change(self):
        previous = {
            "session_date": "2026-07-23",
            "central": {"label": "CONFIRMED EXPANSION"},
            "generated_at": "2026-07-23T13:52:00+00:00",
            "indices": {
                symbol: {
                    "monthly": {"net_gex": -100},
                    "zero_dte": {"net_gex": -100},
                    "bars": {"location": "inside_range", "or_low": 10, "or_high": 12},
                } for symbol in ("SPY", "QQQ")
            },
        }
        current = {
            "SPY": {
                "monthly": {"net_gex": -120}, "zero_dte": {"net_gex": -200},
                "bars": {"location": "above_range", "or_low": 10, "or_high": 11},
            },
            "QQQ": {
                "monthly": {"net_gex": -110}, "zero_dte": {"net_gex": -500},
                "bars": {"location": "inside_range", "or_low": 20, "or_high": 22},
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            old_archive = cb.ARCHIVE
            cb.ARCHIVE = Path(tmp)
            base = Path(tmp) / "2026" / "07" / "2026-07-23"
            base.mkdir(parents=True)
            (base / "brief.json").write_text(json.dumps(previous))
            (base / "score.json").write_text(json.dumps({"result": "THESIS STRENGTHENED"}))
            try:
                result = cb.build_change_fingerprint(date(2026, 7, 24), "CONFIRMED EXPANSION", current)
            finally:
                cb.ARCHIVE = old_archive
        self.assertIn("QQQ 0DTE", result["bullets"][0])
        self.assertEqual(result["previous_result"], "THESIS STRENGTHENED")

    def test_render_pdf(self):
        day = date(2026, 7, 22)
        gex = {"available": True, "regime": "positive", "spot": 600, "flip": 590,
               "call_wall": 610, "put_wall": 590, "call_wall_distance_pct": 1.67,
               "put_wall_distance_pct": -1.67, "coverage": 1.0}
        bars = cb.summarize_bars(cb.fixture_bars(day)["SPY"], day)
        idx_spy = cb.describe_index("SPY", gex, gex, bars)
        bars_q = cb.summarize_bars(cb.fixture_bars(day)["QQQ"], day)
        idx_qqq = cb.describe_index("QQQ", gex, gex, bars_q)
        packet = {
            "session_date": day.isoformat(), "edition": "TEST", "data_quality": "FRESH",
            "report_id": "TEST", "generated_at": "2026-07-22T13:55:00+00:00",
            "evidence_cutoff": "2026-07-22T13:50:00+00:00",
            "central": {"label": "CONFIRMED PINNING", "strategy_compatibility": "MR-COMPATIBLE",
                        "question": cb.build_question("CONFIRMED PINNING", idx_spy, idx_qqq),
                        "working_conclusion": "Test conclusion.",
                        "narrative_hinge": cb.hinge_text("CONFIRMED PINNING", idx_spy, idx_qqq)},
            "indices": {"SPY": idx_spy, "QQQ": idx_qqq},
            "evidence": [cb.evidence("E1", "supports", "SPY", "Test observation", "Test role", ["S1"])],
            "scenarios": cb.build_scenarios("CONFIRMED PINNING", idx_spy, idx_qqq),
            "calendar": [], "optional_focus": None,
            "sources": [cb.source_record("S1", "monthly_gex", "fixture", "now", "now", True, "fixture")],
        }
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "brief.pdf"
            cb.render_pdf(packet, out)
            self.assertTrue(out.exists())
            self.assertGreater(out.stat().st_size, 5000)


if __name__ == "__main__":
    unittest.main()
