"""Tests for smc/exit_monitor.py -- stream-driven exits.

unittest, no network, no broker. Proves the properties that failed on
2026-07-31: exits fire on the quote that crosses the threshold (not on a
2-minute tick), submission is immediate, an unfilled exit is never
re-priced on a schedule, and a stale quote cannot trigger an exit.
"""
from __future__ import annotations

import datetime as dt
import time
import unittest

from smc.calendar import SessionSchedule
from smc.exit_monitor import StreamingExitMonitor, theta_to_quote
from smc.lifecycle import EXIT_FORCED_CLOSE, EXIT_STOP, EXIT_TARGET, EXIT_TIME_STOP


class SQ:
    """Minimal ThetaData StreamQuote stand-in."""

    def __init__(self, occ="QQQ260803C00580000", bid=0.80, ask=0.84,
                 age=0.1, generation=3):
        self.occ, self.bid, self.ask = occ, bid, ask
        self.bid_size, self.ask_size = 10, 10
        self._age = age
        self.generation = generation
        self.exchange_ts = dt.datetime(2026, 8, 3, 14, 30, tzinfo=dt.timezone.utc)

    def age_seconds(self):
        return self._age


class ExitCfg:
    target_return = 0.25
    premium_stop_pct = -0.20
    time_stop_minutes = 30


class Cfg:
    max_quote_age_seconds = 10.0
    forced_close_buffer_minutes = 30
    early_close_flatten_buffer_minutes = 15


def Sched(close_hours_ahead=6, early=False, degraded=False):
    """Real SessionSchedule, not a hand-rolled fake -- an earlier version of
    this test invented open_dt/close_dt and blew up inside must_flatten,
    which expects open_et/close_et."""
    from zoneinfo import ZoneInfo
    et = ZoneInfo("America/New_York")
    now_et = dt.datetime.now(dt.timezone.utc).astimezone(et)
    return SessionSchedule(
        session_date=now_et.date(), is_trading_day=True,
        open_et=now_et - dt.timedelta(hours=1),
        close_et=now_et + dt.timedelta(hours=close_hours_ahead),
        is_early_close=early, degraded=degraded, source="test")


def position(entry=1.00, occ="QQQ260803C00580000", pid="pos-1", age_min=1):
    return {"position_id": pid, "occ": occ, "entry_fill_price": entry,
            "opened_at": dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=age_min)}


def monitor(positions=None, submit=None, cfg=None):
    submitted = []

    def _submit(pos, reason, quote):
        submitted.append((pos["position_id"], reason, quote.bid))
        return f"coid-{pos['position_id']}"

    m = StreamingExitMonitor(
        open_positions=lambda: (positions if positions is not None else [position()]),
        submit_exit=submit or _submit,
        exit_config=ExitCfg(), schedule_for=lambda now: Sched(),
        config=cfg or Cfg())
    m._submitted_log = submitted
    return m


class AdapterTests(unittest.TestCase):
    def test_theta_quote_is_treated_as_real_nbbo(self):
        q = theta_to_quote(SQ())
        self.assertTrue(q.is_nbbo)
        self.assertEqual(q.feed, "thetadata")
        self.assertIn("ThetaData", q.label)

    def test_none_passes_through(self):
        self.assertIsNone(theta_to_quote(None))

    def test_fields_carried(self):
        q = theta_to_quote(SQ(bid=0.5, ask=0.6))
        self.assertEqual((q.bid, q.ask), (0.5, 0.6))
        self.assertTrue(q.two_sided)


class ThresholdTests(unittest.TestCase):
    def test_stop_fires_on_the_crossing_quote(self):
        m = monitor()
        rec = m.on_quote(SQ(bid=0.79))          # entry 1.00, stop at 0.80
        self.assertIsNotNone(rec)
        self.assertEqual(rec.reason, EXIT_STOP)
        self.assertTrue(rec.submitted)

    def test_target_fires(self):
        m = monitor()
        rec = m.on_quote(SQ(bid=1.26))          # target 1.25
        self.assertEqual(rec.reason, EXIT_TARGET)

    def test_no_exit_between_thresholds(self):
        self.assertIsNone(monitor().on_quote(SQ(bid=1.00)))

    def test_forced_close_fires_near_session_close(self):
        m = monitor()
        m.schedule_for = lambda now: Sched(close_hours_ahead=0)
        rec = m.on_quote(SQ(bid=1.00))
        self.assertEqual(rec.reason, EXIT_FORCED_CLOSE)

    def test_time_stop_when_flat_and_old(self):
        m = monitor(positions=[position(age_min=45)])
        rec = m.on_quote(SQ(bid=0.95))
        self.assertEqual(rec.reason, EXIT_TIME_STOP)

    def test_thresholds_come_from_config_not_this_module(self):
        """Nothing in exit_monitor defines a threshold; changing the config
        must change behaviour."""
        import inspect

        import smc.exit_monitor as em
        src = inspect.getsource(em)
        for literal in ("0.25", "-0.20", "0.20"):
            self.assertNotIn(f"= {literal}", src)


class ImmediacyTests(unittest.TestCase):
    def test_submission_is_immediate_on_the_same_call(self):
        m = monitor()
        rec = m.on_quote(SQ(bid=0.70))
        self.assertTrue(rec.submitted)
        self.assertEqual(len(m._submitted_log), 1)
        self.assertIsNotNone(rec.client_order_id)

    def test_latency_chain_recorded(self):
        m = monitor()
        rec = m.on_quote(SQ(bid=0.70))
        self.assertIsNotNone(rec.decide_latency_ms)
        self.assertIsNotNone(rec.submit_latency_ms)
        self.assertIsNotNone(rec.total_latency_ms)
        self.assertLess(rec.decide_latency_ms, 100.0)

    def test_quote_provenance_recorded(self):
        m = monitor()
        rec = m.on_quote(SQ(bid=0.70, generation=3))
        self.assertEqual(rec.quote_generation, 3)
        self.assertIsNotNone(rec.quote_exchange_ts)
        self.assertLess(rec.quote_age_seconds, 1.0)


class StaleQuoteTests(unittest.TestCase):
    def test_stale_quote_cannot_trigger_an_exit(self):
        m = monitor()
        self.assertIsNone(m.on_quote(SQ(bid=0.10, age=45.0)))
        self.assertEqual(m.health()["suppressed_stale_quote"], 1)

    def test_fresh_quote_after_stale_still_works(self):
        m = monitor()
        m.on_quote(SQ(bid=0.10, age=45.0))
        self.assertIsNotNone(m.on_quote(SQ(bid=0.10, age=0.2)))


class NoRepricingLadderTests(unittest.TestCase):
    """The 2026-07-31 failure: an unfilled exit re-priced downward every
    ~2 minutes, walking a -20% stop to -66%."""

    def test_second_quote_does_not_submit_a_second_exit(self):
        m = monitor()
        m.on_quote(SQ(bid=0.79))
        m.on_quote(SQ(bid=0.60))
        m.on_quote(SQ(bid=0.40))
        self.assertEqual(len(m._submitted_log), 1)
        self.assertEqual(m.health()["suppressed_inflight"], 2)

    def test_retry_allowed_only_after_the_exit_reaches_terminal(self):
        m = monitor()
        m.on_quote(SQ(bid=0.79))
        m.on_exit_terminal("pos-1")          # e.g. cancelled or rejected
        m.on_quote(SQ(bid=0.60))
        self.assertEqual(len(m._submitted_log), 2)

    def test_no_timer_sleep_or_rest_call_in_the_exit_path(self):
        """Scans CODE only -- the module docstring legitimately mentions the
        cron it replaces, and an earlier version of this test flagged that."""
        import ast
        import inspect

        import smc.exit_monitor as em
        tree = ast.parse(inspect.getsource(em))
        for node in ast.walk(tree):
            if isinstance(node, (ast.Expr,)) and isinstance(node.value, ast.Constant)                     and isinstance(node.value.value, str):
                continue                      # docstring
            if isinstance(node, ast.Attribute):
                dotted = f"{getattr(node.value, 'id', '')}.{node.attr}"
                self.assertNotIn(dotted, ("time.sleep", "requests.get",
                                          "requests.post", "requests.request"))


class FailureTests(unittest.TestCase):
    def test_submit_failure_recorded_and_not_marked_inflight(self):
        def boom(pos, reason, quote):
            raise RuntimeError("broker down")
        m = monitor(submit=boom)
        rec = m.on_quote(SQ(bid=0.70))
        self.assertFalse(rec.submitted)
        self.assertIn("broker down", rec.submit_error)
        self.assertEqual(m.health()["inflight"], 0)

    def test_failed_submit_retried_on_next_quote(self):
        calls = []

        def flaky(pos, reason, quote):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("transient")
            return "coid-2"
        m = monitor(submit=flaky)
        m.on_quote(SQ(bid=0.70))
        rec = m.on_quote(SQ(bid=0.70))
        self.assertTrue(rec.submitted)

    def test_unrelated_occ_ignored(self):
        m = monitor()
        self.assertIsNone(m.on_quote(SQ(occ="QQQ260803P00500000", bid=0.10)))

    def test_health_shape(self):
        h = monitor().health()
        for k in ("decisions", "submitted", "inflight", "suppressed_stale_quote",
                  "suppressed_inflight", "max_decide_latency_ms", "by_reason"):
            self.assertIn(k, h)


if __name__ == "__main__":
    unittest.main()
