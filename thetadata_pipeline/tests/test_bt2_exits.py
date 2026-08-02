import datetime as dt
import unittest

import pandas as pd

from thetadata_pipeline.bt2_exits import (
    EXIT_DATA_BLOCKED, EXIT_FORCED_CLOSE, EXIT_INVALIDATION, EXIT_STOP,
    EXIT_TARGET, EXIT_TIME_STOP, FLAG_AMBIGUOUS_INTRABAR, FLAG_DATA_GAP,
    FLAG_PARTIAL_DATA_GAP, ExitConfig, resolve_exit,
)

ENTRY_TS = pd.Timestamp("2026-07-24 14:00:00", tz="UTC")  # 10:00 ET
SESSION_DATE = dt.date(2026, 7, 24)
ENTRY_PREMIUM = 0.30  # target=0.375 (default +25%), stop=0.24 (default -20%)


def _tick(minute_offset_seconds, bid):
    return {"trade_timestamp": ENTRY_TS + pd.Timedelta(seconds=minute_offset_seconds), "bid": bid}


def _bar(minute_offset, low, high):
    return {"t": ENTRY_TS + pd.Timedelta(minutes=minute_offset), "l": low, "h": high}


class SimplePremiumExitTests(unittest.TestCase):
    def test_target_hit_via_tick(self):
        ticks = pd.DataFrame([_tick(60, 0.40)])
        decision = resolve_exit(ticks, pd.DataFrame(), ENTRY_TS, ENTRY_PREMIUM, "C", None, SESSION_DATE, ExitConfig())
        self.assertEqual(decision.exit_reason, EXIT_TARGET)
        self.assertFalse(decision.ambiguous)
        self.assertEqual(decision.exit_ts, ENTRY_TS + pd.Timedelta(seconds=60))

    def test_stop_hit_via_tick(self):
        ticks = pd.DataFrame([_tick(60, 0.20)])
        decision = resolve_exit(ticks, pd.DataFrame(), ENTRY_TS, ENTRY_PREMIUM, "C", None, SESSION_DATE, ExitConfig())
        self.assertEqual(decision.exit_reason, EXIT_STOP)
        self.assertFalse(decision.ambiguous)


class IntrabarTickOrderingTests(unittest.TestCase):
    def test_stop_then_target_same_minute_resolves_to_stop(self):
        # Both ticks land in the SAME 1-minute bucket (14:00:00-14:01:00).
        # Real tick order, not a coin-flip: stop tick comes first.
        ticks = pd.DataFrame([_tick(20, 0.20), _tick(40, 0.40)])
        decision = resolve_exit(ticks, pd.DataFrame(), ENTRY_TS, ENTRY_PREMIUM, "C", None, SESSION_DATE, ExitConfig())
        self.assertEqual(decision.exit_reason, EXIT_STOP)
        self.assertFalse(decision.ambiguous)  # real tick order resolved it -- not a guess

    def test_target_then_stop_same_minute_resolves_to_target(self):
        ticks = pd.DataFrame([_tick(20, 0.40), _tick(40, 0.20)])
        decision = resolve_exit(ticks, pd.DataFrame(), ENTRY_TS, ENTRY_PREMIUM, "C", None, SESSION_DATE, ExitConfig())
        self.assertEqual(decision.exit_reason, EXIT_TARGET)
        self.assertFalse(decision.ambiguous)


class UnderlyingInvalidationTests(unittest.TestCase):
    def test_invalidation_alone_exits_clean(self):
        bars = pd.DataFrame([_bar(0, low=99.0, high=101.0)])
        decision = resolve_exit(pd.DataFrame(), bars, ENTRY_TS, ENTRY_PREMIUM, "C",
                                 invalidation_level=99.5, session_date=SESSION_DATE, config=ExitConfig())
        self.assertEqual(decision.exit_reason, EXIT_INVALIDATION)
        self.assertFalse(decision.ambiguous)

    def test_put_invalidation_checks_bar_high(self):
        bars = pd.DataFrame([_bar(0, low=99.0, high=105.0)])
        decision = resolve_exit(pd.DataFrame(), bars, ENTRY_TS, ENTRY_PREMIUM, "P",
                                 invalidation_level=104.0, session_date=SESSION_DATE, config=ExitConfig())
        self.assertEqual(decision.exit_reason, EXIT_INVALIDATION)

    def test_invalidation_not_crossed_does_not_exit(self):
        bars = pd.DataFrame([_bar(0, low=100.0, high=101.0)])
        ticks = pd.DataFrame([_tick(30, 0.20)])  # stop, so the walk still terminates deterministically
        decision = resolve_exit(ticks, bars, ENTRY_TS, ENTRY_PREMIUM, "C",
                                 invalidation_level=99.5, session_date=SESSION_DATE, config=ExitConfig())
        self.assertEqual(decision.exit_reason, EXIT_STOP)

    def test_same_minute_collision_is_ambiguous_and_adverse(self):
        # Real bug class this guards against: the underlying is only ever
        # 1-min OHLC (no intrabar tick sequencing possible for it), so when
        # its invalidation level falls inside the SAME minute as a tick-
        # resolved premium target, this module cannot honestly claim to
        # know which happened first. Section 16: assume the worse outcome
        # (invalidation) came first, and flag it.
        bars = pd.DataFrame([_bar(0, low=99.0, high=101.0)])
        ticks = pd.DataFrame([_tick(10, 0.40)])  # target, same minute as the bar
        decision = resolve_exit(ticks, bars, ENTRY_TS, ENTRY_PREMIUM, "C",
                                 invalidation_level=99.5, session_date=SESSION_DATE, config=ExitConfig())
        self.assertEqual(decision.exit_reason, EXIT_INVALIDATION)
        self.assertTrue(decision.ambiguous)
        self.assertIn(FLAG_AMBIGUOUS_INTRABAR, decision.rule_flags)


class TimeStopTests(unittest.TestCase):
    def test_no_progress_after_n_minutes_exits_time_stop(self):
        ticks = pd.DataFrame([_tick(60, 0.30)])  # flat -- no progress, no target/stop cross
        config = ExitConfig(time_stop_minutes=5)
        decision = resolve_exit(ticks, pd.DataFrame(), ENTRY_TS, ENTRY_PREMIUM, "C", None, SESSION_DATE, config)
        self.assertEqual(decision.exit_reason, EXIT_TIME_STOP)
        self.assertEqual(decision.exit_ts, ENTRY_TS + pd.Timedelta(minutes=5))


class ForcedCloseTests(unittest.TestCase):
    def test_reaches_forced_close_with_no_trigger(self):
        entry_ts = pd.Timestamp("2026-07-24 19:00:00", tz="UTC")  # 15:00 ET, 30min before 15:30 close
        ticks = pd.DataFrame([{"trade_timestamp": entry_ts + pd.Timedelta(minutes=1), "bid": 0.32}])
        config = ExitConfig(time_stop_minutes=60)  # won't fire inside the 30min window
        decision = resolve_exit(ticks, pd.DataFrame(), entry_ts, ENTRY_PREMIUM, "C", None, SESSION_DATE, config)
        self.assertEqual(decision.exit_reason, EXIT_FORCED_CLOSE)
        self.assertEqual(decision.exit_ts, pd.Timestamp("2026-07-24 19:30:00", tz="UTC"))


class DataGapTests(unittest.TestCase):
    def test_zero_ticks_whatsoever_is_data_blocked(self):
        config = ExitConfig(time_stop_minutes=1000)  # disable time-stop so this doesn't preempt the real check
        decision = resolve_exit(pd.DataFrame(), pd.DataFrame(), ENTRY_TS, ENTRY_PREMIUM, "C", None, SESSION_DATE, config)
        self.assertEqual(decision.exit_reason, EXIT_DATA_BLOCKED)
        self.assertIn(FLAG_DATA_GAP, decision.rule_flags)

    def test_partial_gap_flagged_but_resolves_once_data_resumes(self):
        # A real 19-minute tick-less stretch, then data resumes and
        # correctly resolves TARGET -- Section 16: mark the gap, but do not
        # interpolate/fabricate a directional read during it, and do not
        # let the gap alone force an exit if real data later confirms one.
        ticks = pd.DataFrame([
            _tick(60, 0.30),          # minute 1 -- flat, no cross
            _tick(20 * 60, 0.40),     # minute 20 -- target, after a 19-back gap
        ])
        config = ExitConfig(time_stop_minutes=1000)
        decision = resolve_exit(ticks, pd.DataFrame(), ENTRY_TS, ENTRY_PREMIUM, "C", None, SESSION_DATE, config)
        self.assertEqual(decision.exit_reason, EXIT_TARGET)
        self.assertIn(FLAG_PARTIAL_DATA_GAP, decision.rule_flags)


class MaeMfeTests(unittest.TestCase):
    def test_mae_mfe_tracked_across_ticks(self):
        # Three ticks in three different minutes: mildly adverse, then
        # favorable, then a real stop cross on the third -- MAE/MFE must
        # reflect the whole path, not just the triggering tick.
        ticks = pd.DataFrame([_tick(30, 0.26), _tick(90, 0.33), _tick(150, 0.20)])
        decision = resolve_exit(ticks, pd.DataFrame(), ENTRY_TS, ENTRY_PREMIUM, "C", None, SESSION_DATE, ExitConfig())
        # mae = max adverse excursion = entry(0.30) - min_bid_seen(0.20) = 0.10
        # mfe = max favorable excursion = max_bid_seen(0.33) - entry(0.30) = 0.03
        self.assertEqual(decision.exit_reason, EXIT_STOP)  # 0.20 <= stop_level 0.24, on the 3rd tick
        self.assertAlmostEqual(decision.mae, 0.10, places=4)
        self.assertAlmostEqual(decision.mfe, 0.03, places=4)


if __name__ == "__main__":
    unittest.main()
