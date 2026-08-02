import datetime as dt
import threading
import time
import unittest
from zoneinfo import ZoneInfo

import pandas as pd

from smc.live_detector_clock import ConfirmedMinuteScheduler, LiveDetectorCycle


ET = ZoneInfo("America/New_York")


class FakeDetector:
    def __init__(self, frames):
        self.frames = list(frames)
        self.ingested = 0
        self.detected = 0
        self.last_runtime_ms = 12.5

    def fetch(self, _session):
        if len(self.frames) > 1:
            return self.frames.pop(0)
        return self.frames[0]

    def ingest_today(self, frame):
        n = 0 if frame is None or frame.empty else len(frame)
        self.ingested += n
        return {"appended": n}

    def detect(self):
        self.detected += 1
        return [object()]


class TestLiveDetectorClock(unittest.TestCase):
    def test_cycle_waits_for_expected_closed_bar_then_detects(self):
        empty = pd.DataFrame(columns=["t", "o", "h", "l", "c", "v"])
        ready = pd.DataFrame([{
            "t": "2026-08-03T13:30:00Z", "o": 1, "h": 2,
            "l": 1, "c": 2, "v": 10,
        }])
        detector = FakeDetector([empty, ready])
        cycle = LiveDetectorCycle(
            detector, availability_timeout=1.0, poll_seconds=0,
            now_utc=lambda: dt.datetime(2026, 8, 3, 13, 31, 0, 100000,
                                        tzinfo=dt.timezone.utc),
            sleep=lambda _seconds: None)

        signals = cycle()

        self.assertEqual(1, len(signals))
        self.assertEqual(1, detector.detected)
        self.assertEqual(2, cycle.history[-1]["polls"])
        self.assertEqual("2026-08-03T13:30:00+00:00",
                         cycle.history[-1]["bar_open_utc"])
        self.assertIsNotNone(cycle.history[-1]["availability_ms"])

    def test_cycle_timeout_never_replays_stale_window(self):
        empty = pd.DataFrame(columns=["t", "o", "h", "l", "c", "v"])
        detector = FakeDetector([empty])
        ticks = iter([0.0, 0.0, 0.0, 0.6, 0.6, 0.6])
        cycle = LiveDetectorCycle(
            detector, availability_timeout=0.5, poll_seconds=0,
            monotonic=lambda: next(ticks),
            now_utc=lambda: dt.datetime(2026, 8, 3, 13, 31,
                                        tzinfo=dt.timezone.utc),
            sleep=lambda _seconds: None)

        self.assertEqual([], cycle())
        self.assertEqual(0, detector.detected)
        self.assertIsNone(cycle.history[-1]["availability_ms"])

    def test_scheduler_includes_final_1559_bar_once(self):
        called = []
        arrived = threading.Event()

        def request(session, reason):
            called.append((session, reason))
            arrived.set()

        scheduler = ConfirmedMinuteScheduler(
            request,
            now_et=lambda: dt.datetime(2026, 8, 3, 16, 0, 0, 100000,
                                       tzinfo=ET),
            tick_seconds=0.001)
        scheduler.start()
        self.assertTrue(arrived.wait(0.5))
        time.sleep(0.01)
        scheduler.stop()

        self.assertEqual([("2026-08-03", "confirmed_bar")], called)
        self.assertEqual("2026-08-03T16:00:00-04:00",
                         scheduler.last_requested_close)


if __name__ == "__main__":
    unittest.main()
