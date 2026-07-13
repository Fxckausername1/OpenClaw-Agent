import copy
import unittest

from signal_quality_replay import aligned_magnitude, extract_features, passes, signed_pass


def bar(timestamp, open_price, high, low, close, volume=100, z=0, rsi=50, vdev=0):
    return {
        "time": timestamp,
        "open": open_price,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
        "z": z,
        "rsi": rsi,
        "vwap_dev": vdev,
    }


class SymmetryTests(unittest.TestCase):
    def test_gap_and_drive_alignment_are_side_symmetric(self):
        self.assertTrue(aligned_magnitude(0.004, "LONG", 0.003))
        self.assertTrue(aligned_magnitude(-0.004, "SHORT", 0.003))
        self.assertFalse(aligned_magnitude(-0.004, "LONG", 0.003))

    def test_breadth_alignment_is_side_symmetric(self):
        self.assertTrue(signed_pass(0.60, "LONG", 0.55))
        self.assertTrue(signed_pass(0.40, "SHORT", 0.55))
        self.assertFalse(signed_pass(0.60, "SHORT", 0.55))


class FeatureTimingTests(unittest.TestCase):
    def setUp(self):
        self.bars = [
            bar("2025-01-02T09:30:00", 100.0, 100.5, 99.5, 99.7, z=-2.1, rsi=24, vdev=-0.021),
            bar("2025-01-02T09:35:00", 99.7, 100.0, 99.2, 99.4, z=-2.2, rsi=23, vdev=-0.022),
            bar("2025-01-02T09:40:00", 99.4, 99.9, 99.1, 99.6, z=-1.8, rsi=28, vdev=-0.018),
            bar("2025-01-02T09:45:00", 99.6, 100.3, 99.5, 100.2, volume=200, z=-1.0, rsi=35, vdev=-0.01),
            bar("2025-01-02T09:50:00", 100.2, 500.0, 1.0, 400.0, volume=999999),
        ]
        self.source = {
            "signal_index": 3,
            "signal_time": "2025-01-02T09:45:00",
            "side": "LONG",
            "entry": 100.0,
        }
        self.market = {
            __import__("datetime").datetime.fromisoformat("2025-01-02T09:40:00"): 0.30,
            __import__("datetime").datetime.fromisoformat("2025-01-02T09:45:00"): 0.35,
        }
        self.sector = {( __import__("datetime").datetime.fromisoformat("2025-01-02T09:45:00"), "XLK"): 0.60}

    def features(self, bars):
        return extract_features(self.source, bars, 99.0, self.market, self.sector, "XLK")

    def test_future_bar_cannot_change_signal_features(self):
        first = self.features(self.bars)
        changed = copy.deepcopy(self.bars)
        changed[4].update({"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1})
        second = self.features(changed)
        self.assertEqual(first, second)

    def test_exhaustion_reclaim_and_breadth_turn(self):
        features = self.features(self.bars)
        self.assertTrue(features["reclaim"])
        self.assertEqual(features["exhaustion_score"], 3)
        self.assertTrue(features["breadth_turn"])
        candidate = {"reclaim": True, "exhaustion_score": 2, "breadth_turn": True}
        self.assertTrue(passes(features, self.source, candidate))

    def test_overnight_gap_uses_prior_close_and_regular_open(self):
        features = self.features(self.bars)
        self.assertAlmostEqual(features["overnight_gap"], 100.0 / 99.0 - 1.0)


if __name__ == "__main__":
    unittest.main()
