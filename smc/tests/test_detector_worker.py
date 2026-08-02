"""Tests for smc/detector_worker.py."""
from __future__ import annotations

import threading
import time
import unittest

from smc.detector_worker import DetectorWorker


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


def worker(run=None, **kw):
    return DetectorWorker(run or (lambda: []), **kw)


class CoalescingTests(unittest.TestCase):
    def test_at_most_one_pending_task(self):
        """A slow patch must not build an unbounded pile of replays."""
        w = worker()
        for _ in range(10):
            w.request("2026-08-03")
        self.assertLessEqual(w.backlog_depth, 2)
        self.assertGreater(w.coalesced, 0)

    def test_newest_request_wins(self):
        w = worker()
        w.request("2026-08-03", reason="first")
        t = w.request("2026-08-03", reason="second")
        self.assertEqual(w._pending.task_id, t.task_id)
        self.assertEqual(w._pending.reason, "second")

    def test_backlog_depth_reported(self):
        w = worker()
        self.assertEqual(w.backlog_depth, 0)
        w.request("2026-08-03")
        self.assertEqual(w.backlog_depth, 1)

    def test_oldest_task_age(self):
        clk = Clock()
        w = worker(clock=clk)
        w.request("2026-08-03")
        clk.advance(3.0)
        self.assertAlmostEqual(w.oldest_task_age_s, 3.0, places=2)


class ExecutionTests(unittest.TestCase):
    def test_runs_off_the_calling_thread(self):
        seen = {}

        def run():
            seen["thread"] = threading.current_thread().name
            return [{"sig": 1}]
        w = worker(run)
        w.start()
        try:
            w.request("2026-08-03")
            got = None
            for _ in range(100):
                got = w.poll()
                if got:
                    break
                time.sleep(0.02)
            self.assertIsNotNone(got)
            task, signals = got
            self.assertEqual(len(signals), 1)
            self.assertEqual(task.n_signals, 1)
            self.assertNotEqual(seen["thread"], threading.current_thread().name)
        finally:
            w.stop()

    def test_all_timestamps_captured(self):
        w = worker(lambda: [])
        w.start()
        try:
            w.request("2026-08-03")
            got = None
            for _ in range(100):
                got = w.poll()
                if got:
                    break
                time.sleep(0.02)
            task, _ = got
            for field in ("requested_monotonic", "started_monotonic",
                          "finished_monotonic", "result_queued_monotonic",
                          "received_monotonic"):
                self.assertIsNotNone(getattr(task, field), field)
            self.assertIsNotNone(task.runtime_ms)
            self.assertIsNotNone(task.queue_wait_ms)
            self.assertIsNotNone(task.delivery_delay_ms)
            self.assertIsNotNone(task.total_ms)
        finally:
            w.stop()

    def test_replay_exception_does_not_kill_worker(self):
        calls = []

        def flaky():
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("replay blew up")
            return [{"ok": 1}]
        w = worker(flaky)
        w.start()
        try:
            w.request("2026-08-03")
            first = None
            for _ in range(100):
                first = w.poll()
                if first:
                    break
                time.sleep(0.02)
            self.assertIsNotNone(first[0].error)
            w.request("2026-08-03")
            second = None
            for _ in range(100):
                second = w.poll()
                if second:
                    break
                time.sleep(0.02)
            self.assertIsNone(second[0].error)
        finally:
            w.stop()

    def test_poll_empty_returns_none(self):
        self.assertIsNone(worker().poll())


class CeilingTests(unittest.TestCase):
    def test_ceiling_violation_counted_and_degrades(self):
        clk = Clock()

        def slow():
            clk.advance(2.0)          # 2000 ms
            return []
        w = worker(slow, runtime_ceiling_ms=900.0, clock=clk)
        w.start()
        try:
            w.request("2026-08-03")
            for _ in range(100):
                if w.ceiling_violations:
                    break
                time.sleep(0.02)
            self.assertGreaterEqual(w.ceiling_violations, 1)
            self.assertTrue(w.degraded)
        finally:
            w.stop()

    def test_fast_replay_is_not_degraded(self):
        w = worker(lambda: [])
        w.start()
        try:
            w.request("2026-08-03")
            for _ in range(100):
                if w.health()["tasks_completed"]:
                    break
                time.sleep(0.02)
            self.assertFalse(w.degraded)
            self.assertEqual(w.ceiling_violations, 0)
        finally:
            w.stop()

    def test_backlog_alone_degrades(self):
        w = worker()
        for _ in range(5):
            w.request("2026-08-03")
        w._running = w._pending          # simulate one running plus one queued
        self.assertGreater(w.backlog_depth, 1)
        self.assertTrue(w.degraded)

    def test_ceiling_is_frozen_and_reported(self):
        w = worker(runtime_ceiling_ms=900.0)
        self.assertEqual(w.health()["runtime_ceiling_ms"], 900.0)


class HealthTests(unittest.TestCase):
    def test_percentiles_present_after_runs(self):
        w = worker(lambda: [])
        w.start()
        try:
            for _ in range(3):
                w.request("2026-08-03")
                for _ in range(100):
                    if w.poll():
                        break
                    time.sleep(0.01)
            h = w.health()
            self.assertIsNotNone(h["runtime_p50_ms"])
            self.assertIsNotNone(h["runtime_p95_ms"])
            self.assertIsNotNone(h["runtime_p99_ms"])
        finally:
            w.stop()

    def test_health_shape(self):
        h = worker().health()
        for k in ("tasks_completed", "backlog_depth", "oldest_task_age_s",
                  "coalesced_requests", "runtime_ceiling_ms", "ceiling_violations",
                  "degraded", "runtime_p50_ms", "delivery_p95_ms", "last_task"):
            self.assertIn(k, h)

    def test_history_is_bounded(self):
        w = worker(lambda: [])
        w.history = [object()] * 500
        w.start()
        try:
            w.request("2026-08-03")
            for _ in range(100):
                if w.poll():
                    break
                time.sleep(0.01)
            self.assertLessEqual(len(w.history), 200)
        finally:
            w.stop()


if __name__ == "__main__":
    unittest.main()
