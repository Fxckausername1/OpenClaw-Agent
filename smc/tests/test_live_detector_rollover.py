import datetime as dt
import unittest

import pandas as pd

from smc.live_detector_clock import LiveDetectorCycle


class RolloverDetector:
    def __init__(self):
        self._today_session = "2026-07-31"
        self._history = {"2026-07-30": pd.DataFrame()}
        self.warmups = []
        self.last_runtime_ms = 1.0

    def warmup(self, session, prior_sessions):
        self.warmups.append((session, prior_sessions))
        self._today_session = session

    def fetch(self, _session):
        return pd.DataFrame([{
            "t": "2026-08-03T13:30:00Z", "o": 1, "h": 2,
            "l": 1, "c": 2, "v": 10,
        }])

    def ingest_today(self, frame):
        return {"appended": len(frame)}

    def detect(self):
        return []


class DetectorRolloverTests(unittest.TestCase):
    def test_overnight_daemon_rewarms_for_new_session(self):
        detector = RolloverDetector()
        cycle = LiveDetectorCycle(
            detector,
            now_utc=lambda: dt.datetime(2026, 8, 3, 13, 31,
                                        tzinfo=dt.timezone.utc),
            sleep=lambda _seconds: None)
        cycle()
        self.assertEqual([("2026-08-03", ["2026-07-30", "2026-07-31"])],
                         detector.warmups)
        self.assertTrue(cycle.history[-1]["session_rollover"])


if __name__ == "__main__":
    unittest.main()
