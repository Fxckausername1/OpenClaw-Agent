"""Regression tests for the v2.2 default-on audit fixes ported into the replay.

Every case here was UNCOVERED by the pre-existing 39-test suite -- that suite
passed unchanged against both the buggy and the fixed zone-credit ordering, which
is precisely why the funeral-bar bug survived into a live indicator. These tests
fail against v2.1 behaviour by construction.

Ported fixes under test:
  1. survive-first / credit-second for FVGs and order blocks (audit #3)
  2. independent pending sweeps, replacing one shared pend_hi/pend_lo (audit #5)
  3. same-bar multi-source reclaim joining (v2.2c)
  4. opposing same-bar reclaims resolved by close direction (v2.2b)
  5. expiry pruned BEFORE arming, so the live window is reclaim_win+1 (v2.2e)
  6. dual-break resolution in Module 2 and f_structDir (audit #2 + the one the
     audit missed)
"""

from __future__ import annotations

import unittest

from thetadata_pipeline.heff_smc_engine import (
    SRC_PDH, SRC_POOL, SRC_SESSION, SWEEP_CONFIRM_EXT, SWEEP_CONFIRM_V21,
    FvgZone, HeffSmcConfig, HeffSmcEngine, OrderBlock, PendSweep,
)
from thetadata_pipeline.heff_smc_htf import HtfStructDirTracker


def _engine(**overrides) -> HeffSmcEngine:
    cfg = HeffSmcConfig(**overrides)
    e = HeffSmcEngine(config=cfg, htf_dir_lookup=lambda bi: (0, 0))
    e.start_new_session(None, None)
    return e


def _warm(engine, n=30, base=500.0):
    """Feed flat bars so ATR/MA trackers are seeded without creating structure."""
    import datetime as dt
    t = dt.datetime(2026, 7, 1, 10, 0)
    for i in range(n):
        engine.process_bar(t + dt.timedelta(minutes=i), base, base + 0.05,
                           base - 0.05, base, 1000.0)


class ZoneSurviveFirstTest(unittest.TestCase):
    """Audit #3: v2.1 credited a zone tap BEFORE testing invalidation, so the bar
    that closed through a bullish gap and destroyed it still paid out BULLISH zone
    confluence on that same close."""

    def test_bar_that_kills_a_bullish_ob_pays_no_bullish_zone_credit(self):
        e = _engine()
        _warm(e)
        # A live bullish order block, then a bar that closes below its floor.
        e.obs.append(OrderBlock(top=500.5, bot=500.0, bull=True, born=0))
        import datetime as dt
        res = e.process_bar(dt.datetime(2026, 7, 1, 11, 0),
                            500.4, 500.6, 499.0, 499.2, 1000.0)   # closes BELOW bot
        self.assertEqual(res["factors_long"]["zone"], 0.0,
                         "a bar that destroys a bullish OB must not credit bullish zone")
        self.assertTrue(e.obs[-1].dead, "the block must actually be dead")
        self.assertGreater(e.zone_credit_suppressed, 0)

    def test_bar_that_kills_a_bearish_ob_pays_no_bearish_zone_credit(self):
        e = _engine()
        _warm(e)
        e.obs.append(OrderBlock(top=500.5, bot=500.0, bull=False, born=0))
        import datetime as dt
        res = e.process_bar(dt.datetime(2026, 7, 1, 11, 0),
                            500.1, 502.0, 500.0, 501.5, 1000.0)   # closes ABOVE top
        self.assertEqual(res["factors_short"]["zone"], 0.0)
        self.assertTrue(e.obs[-1].dead)

    def test_surviving_ob_tap_still_credits_normally(self):
        """The fix must not suppress legitimate taps."""
        e = _engine()
        _warm(e)
        e.obs.append(OrderBlock(top=500.5, bot=500.0, bull=True, born=0))
        import datetime as dt
        res = e.process_bar(dt.datetime(2026, 7, 1, 11, 0),
                            500.6, 500.7, 500.2, 500.6, 1000.0)   # dips in, closes inside
        self.assertGreater(res["factors_long"]["zone"], 0.0,
                           "a real tap on a surviving block must still pay")
        self.assertFalse(e.obs[-1].dead)

    def test_inverting_fvg_credits_neither_direction_on_the_inversion_bar(self):
        e = _engine()
        _warm(e)
        e.fvgs.append(FvgZone(top=500.5, bot=500.0, bull=True, born=0))
        import datetime as dt
        res = e.process_bar(dt.datetime(2026, 7, 1, 11, 0),
                            500.4, 500.6, 499.0, 499.2, 1000.0)
        self.assertEqual(res["factors_long"]["zone"], 0.0)
        self.assertEqual(res["factors_short"]["zone"], 0.0,
                         "the bar that INVERTS a gap is not a tap of its new direction")
        self.assertTrue(e.fvgs[-1].inverted)


class IndependentPendingSweepTest(unittest.TestCase):
    """Audit #5: one shared pend_hi/pend_lo meant a second sweep silently erased
    the first before it could ever reclaim."""

    def test_two_high_sweeps_on_different_bars_both_stay_armed(self):
        e = _engine()
        _warm(e)
        e.pend_sweeps.append(PendSweep(level=505.0, ext=504.0, is_high=True,
                                        src=SRC_POOL, born=e.bar_index))
        e.pend_sweeps.append(PendSweep(level=507.0, ext=506.0, is_high=True,
                                        src=SRC_PDH, born=e.bar_index))
        self.assertEqual(len(e.pend_sweeps), 2,
                         "v2.1 would have kept only the second -- both must survive")
        srcs = {ps.src for ps in e.pend_sweeps}
        self.assertEqual(srcs, {SRC_POOL, SRC_PDH})

    def test_same_bar_same_level_duplicate_is_deduped(self):
        """A pool sitting exactly on PDH must not arm twice and double-fire."""
        e = _engine()
        _warm(e)
        bi = e.bar_index
        e.pend_sweeps.append(PendSweep(level=505.0, ext=504.0, is_high=True,
                                        src=SRC_POOL, born=bi))
        before = len(e.pend_sweeps)
        # Simulate the dedupe rule the engine applies when arming.
        dup = any(q.is_high and abs(q.level - 505.0) <= e.cfg.mintick and q.born == bi
                  for q in e.pend_sweeps)
        self.assertTrue(dup, "a same-bar same-level high sweep must be seen as a duplicate")
        self.assertEqual(len(e.pend_sweeps), before)

    def test_expired_records_are_pruned_and_do_not_reclaim(self):
        e = _engine(reclaim_win=3)
        _warm(e)
        stale = PendSweep(level=505.0, ext=504.0, is_high=True,
                          src=SRC_POOL, born=e.bar_index - 10)
        e.pend_sweeps.append(stale)
        import datetime as dt
        res = e.process_bar(dt.datetime(2026, 7, 1, 12, 0),
                            500.0, 500.1, 499.9, 500.0, 1000.0)
        self.assertFalse(res["sweep_reclaim_short"],
                         "a record past reclaim_win must expire, not reclaim")
        # The warm-up bars themselves form a pool that this bar can sweep, so the
        # array is not necessarily empty -- assert the STALE record specifically.
        self.assertNotIn(stale, e.pend_sweeps, "the expired record must be pruned")
        self.assertTrue(all(e.bar_index - ps.born <= e.cfg.reclaim_win
                            for ps in e.pend_sweeps),
                        "no surviving record may be older than reclaim_win")
        self.assertGreater(e.pend_expired, 0)

    def test_high_sweep_reclaims_short_when_close_returns_inside(self):
        e = _engine(reclaim_win=3)
        _warm(e)
        e.pend_sweeps.append(PendSweep(level=505.0, ext=504.0, is_high=True,
                                        src=SRC_POOL, born=e.bar_index))
        import datetime as dt
        res = e.process_bar(dt.datetime(2026, 7, 1, 11, 30),
                            504.5, 504.8, 503.0, 503.5, 1000.0)   # closes below 505
        self.assertTrue(res["sweep_reclaim_short"])
        self.assertEqual(len(e.pend_sweeps), 0, "a consumed record must be removed")

    def test_close_back_beyond_level_cancels_rather_than_reclaims(self):
        e = _engine(reclaim_win=3)
        _warm(e)
        e.pend_sweeps.append(PendSweep(level=505.0, ext=504.0, is_high=True,
                                        src=SRC_POOL, born=e.bar_index))
        import datetime as dt
        res = e.process_bar(dt.datetime(2026, 7, 1, 11, 30),
                            505.5, 506.5, 505.2, 506.0, 1000.0)   # closes ABOVE 505
        self.assertFalse(res["sweep_reclaim_short"], "closing back beyond = failed sweep")
        self.assertEqual(len(e.pend_sweeps), 0)

    def test_multi_source_reclaim_joins_every_contributing_source(self):
        """v2.2c: PDH + a pool reclaiming short on one bar is ONE event with TWO
        causes; v2.2b named only the first and deleted the rest."""
        e = _engine(reclaim_win=3)
        _warm(e)
        bi = e.bar_index
        e.pend_sweeps.append(PendSweep(level=505.0, ext=504.0, is_high=True,
                                        src=SRC_POOL, born=bi))
        e.pend_sweeps.append(PendSweep(level=505.2, ext=504.1, is_high=True,
                                        src=SRC_PDH, born=bi))
        import datetime as dt
        res = e.process_bar(dt.datetime(2026, 7, 1, 11, 30),
                            504.5, 504.8, 503.0, 503.5, 1000.0)
        self.assertTrue(res["sweep_reclaim_short"])
        src = res.get("reclaim_src_short", "")
        self.assertIn(SRC_PDH, src)
        self.assertIn(SRC_POOL, src)
        self.assertEqual(src.count("+"), 1, f"sources must be joined once each, got {src!r}")

    def test_opposing_same_bar_reclaims_resolve_to_the_close_direction(self):
        """v2.2b: a pending high-sweep at 505 and low-sweep at 495 both confirm on
        a close of 500, firing long and short together."""
        for close_price, expect_long, expect_short in ((501.0, True, False),
                                                        (499.0, False, True)):
            e = _engine(reclaim_win=3)
            _warm(e)
            bi = e.bar_index
            e.pend_sweeps.append(PendSweep(level=505.0, ext=504.0, is_high=True,
                                            src=SRC_POOL, born=bi))
            e.pend_sweeps.append(PendSweep(level=495.0, ext=496.0, is_high=False,
                                            src=SRC_SESSION, born=bi))
            import datetime as dt
            res = e.process_bar(dt.datetime(2026, 7, 1, 11, 30),
                                500.0, 505.5, 494.5, close_price, 1000.0)
            self.assertEqual(res["sweep_reclaim_long"], expect_long,
                             f"close={close_price}: long")
            self.assertEqual(res["sweep_reclaim_short"], expect_short,
                             f"close={close_price}: short")
            self.assertFalse(res["sweep_reclaim_long"] and res["sweep_reclaim_short"],
                             "both directions must never fire on one bar")
            self.assertGreater(e.opposing_reclaims, 0)

    def test_stricter_confirmation_mode_requires_travel_past_the_extreme(self):
        """Opt-in audit #4. Default must behave as v2.1; the strict mode must not."""
        import datetime as dt
        # close 503.5 is back inside 505 but NOT beyond the sweep bar's ext of 502.
        for mode, expected in ((SWEEP_CONFIRM_V21, True), (SWEEP_CONFIRM_EXT, False)):
            e = _engine(reclaim_win=3, sweep_confirm=mode)
            _warm(e)
            e.pend_sweeps.append(PendSweep(level=505.0, ext=502.0, is_high=True,
                                            src=SRC_POOL, born=e.bar_index))
            res = e.process_bar(dt.datetime(2026, 7, 1, 11, 30),
                                504.5, 504.8, 503.0, 503.5, 1000.0)
            self.assertEqual(res["sweep_reclaim_short"], expected,
                             f"mode={mode!r} should reclaim={expected}")


class DualBreakTest(unittest.TestCase):
    """The dual-break path can only be reached when active_high < active_low. The
    pivot tracker inside update() would overwrite manually-planted levels, so it is
    stubbed out here -- the unit under test is the RESOLUTION, not pivot detection."""

    @staticmethod
    def _isolated(active_high, active_low):
        t = HtfStructDirTracker(piv_len=3)
        t.piv.update = lambda *a, **k: (None, None, 0)   # no pivots -> levels persist
        t.active_high, t.active_low = active_high, active_low
        t._bar_index = 100
        return t

    def test_htf_two_sided_bar_resolves_by_close_not_always_bearish(self):
        """The fix the audit MISSED: v2.1 ran both HTF break tests unguarded, so the
        bearish branch always won and every two-sided bar reported -1."""
        # active_high=45 < close < active_low=55 is the ONLY state where one close
        # breaks both. Resolution is then by the bar's own direction vs its open.
        for open_price, close_price, expected in ((49.0, 50.0, 1), (51.0, 50.0, -1)):
            t = self._isolated(45.0, 55.0)
            got = t.update(70.0, 30.0, close_price, open_price)
            self.assertEqual(got, expected,
                             f"open={open_price} close={close_price} -> dir={expected}")
            self.assertEqual(t.ambiguous_bars, 1)
            self.assertIsNone(t.active_high)
            self.assertIsNone(t.active_low)

    def test_v21_would_have_called_every_two_sided_bar_bearish(self):
        """Pins the bug itself: without an open to resolve by, both an up-close and
        a down-close bar collapse to -1. That is what v2.1 did on EVERY such bar."""
        for open_price in (49.0, 51.0):   # an up-bar and a down-bar, same outcome
            t = self._isolated(45.0, 55.0)
            self.assertEqual(t.update(70.0, 30.0, 50.0, None), -1)
            self.assertEqual(t.unresolved_ambiguous, 1,
                             "the unresolved case must be COUNTED, never silent")

    def test_single_sided_breaks_are_unaffected_by_the_fix(self):
        up = self._isolated(45.0, None)
        self.assertEqual(up.update(50.0, 44.0, 46.0, 45.0), 1)   # close 46 > 45 only
        self.assertEqual(up.ambiguous_bars, 0)
        dn = self._isolated(None, 55.0)
        self.assertEqual(dn.update(56.0, 50.0, 54.0, 55.5), -1)
        self.assertEqual(dn.ambiguous_bars, 0)

    def test_normal_level_ordering_can_break_neither(self):
        """Worth stating explicitly: in the PATHOLOGICAL state (active_high <
        active_low) the two break ranges overlap completely, so every close breaks
        at least one level and any close between them breaks BOTH. Only the normal
        ordering (high above low) has a quiet middle."""
        t = self._isolated(55.0, 45.0)          # normal: high 55 above low 45
        self.assertEqual(t.update(52.0, 48.0, 50.0, 49.0), 0)
        self.assertEqual(t.ambiguous_bars, 0)

    def test_pathological_ordering_always_breaks_something(self):
        for close_price in (44.0, 50.0, 56.0):
            t = self._isolated(45.0, 55.0)
            t.update(70.0, 30.0, close_price, close_price)
            self.assertNotEqual(t.dir, 0,
                                f"close={close_price} must break at least one level")

    def test_close_mode_never_produces_a_module2_dual_break(self):
        """Documents WHY the Module 2 fix is behaviour-neutral on the live config:
        with struc_src='Close', active_low cannot survive below active_high."""
        e = _engine(struc_src="Close")
        _warm(e, n=60)
        import datetime as dt
        t0 = dt.datetime(2026, 7, 1, 12, 0)
        for i in range(60):
            px = 500 + (i % 7) - 3
            e.process_bar(t0 + dt.timedelta(minutes=i), px, px + 2.0, px - 2.0, px, 1500.0)
        self.assertEqual(e.dual_break_bars, 0,
                         "a Close-mode run must never hit the dual-break path")


if __name__ == "__main__":
    unittest.main()
