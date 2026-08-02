"""Tests for heff_smc_engine.py -- a faithful Python port of
HEFF_SMC_V2_REFERENCE.pine. There is no historical ground truth to compare
against (the live indicator's alerts go straight to heff's phone, never
logged), so validation here is entirely through hand-constructed synthetic
bar sequences that exercise each module's documented behavior exactly as the
.pine file's own comments describe it -- same rigor convention as
test_bt2_selector.py / test_bt2_exits.py.

Bar tuples throughout are (o, h, l, c, v). Timestamps are auto-assigned
1-minute apart starting 09:30 ET on a fixed date via feed_bars()/_ts().
"""

from __future__ import annotations

import datetime as dt
import unittest
from zoneinfo import ZoneInfo

from thetadata_pipeline.heff_smc_engine import (
    TRIG_BOS, TRIG_MA_FADE, TRIG_MSS, TRIG_PULLBACK, TRIG_SWEEP_RECLAIM,
    EmaTracker, HeffSmcConfig, HeffSmcEngine, PivotTracker, SmaTracker, WilderAtrTracker,
)

ET = ZoneInfo("America/New_York")
BASE_DAY = dt.date(2026, 1, 5)  # a Monday


def _ts(minute_offset: int) -> dt.datetime:
    base = dt.datetime.combine(BASE_DAY, dt.time(9, 30), tzinfo=ET)
    return base + dt.timedelta(minutes=minute_offset)


def flat_bars(n: int, price: float = 100.0, rng: float = 1.0):
    """n identical (o,h,l,c,v) bars: h=price+rng/2, l=price-rng/2, o=c=price.
    Ties on every bar, so PivotTracker's strict-uniqueness rule never fires
    a pivot from these alone -- pure ATR/MA warmup filler with zero
    structural side effects."""
    return [(price, price + rng / 2, price - rng / 2, price, 1000.0) for _ in range(n)]


def feed_bars(engine: HeffSmcEngine, bars, pdh=None, pdl=None, new_session=True):
    if new_session:
        engine.start_new_session(pdh, pdl)
    results = []
    for i, (o, h, l, c, v) in enumerate(bars):
        results.append(engine.process_bar(_ts(i), o, h, l, c, v))
    return results


def zero_weight_config(**overrides) -> HeffSmcConfig:
    """All confluence weights zeroed, min_score=0, HTF gate off -- isolates
    trigger-detection behavior (bos_up/mss_up/pull_long/.../trig_long) from
    the weighted-score gate, which gets its own dedicated tests below."""
    base = dict(
        w_mss=0.0, w_bos=0.0, w_zone=0.0, w_sweep=0.0, w_pd=0.0, w_ma=0.0,
        w_rvol=0.0, w_htf=0.0, w_kz=0.0, w_pull=0.0, w_fade=0.0, min_score=0.0,
        htf_gate_mode="Off",
    )
    base.update(overrides)
    return HeffSmcConfig(**base)


# ============================================================================
# Module 1 core-series trackers
# ============================================================================
# ---------------------------------------------------------------------------
# v2.2: pend_hi / pend_hi_bar were replaced by an independent PendSweep array
# (each armed sweep now confirms and expires on its own instead of sharing one
# slot every source overwrote). These helpers keep the ORIGINAL assertions'
# intent -- "is a high sweep armed, and from which bar" -- against the new
# structure, so the tests still check behaviour rather than being deleted.
def _armed_high(engine):
    highs = [ps for ps in engine.pend_sweeps if ps.is_high]
    return highs[-1] if highs else None


def _armed_low(engine):
    lows = [ps for ps in engine.pend_sweeps if not ps.is_high]
    return lows[-1] if lows else None


class CoreTrackerTests(unittest.TestCase):
    def test_sma_returns_none_until_warm(self):
        t = SmaTracker(3)
        self.assertIsNone(t.update(1.0))
        self.assertIsNone(t.update(2.0))
        self.assertAlmostEqual(t.update(3.0), 2.0)
        self.assertAlmostEqual(t.update(6.0), (2.0 + 3.0 + 6.0) / 3)

    def test_ema_seeds_with_sma_then_recurses(self):
        t = EmaTracker(3)
        self.assertIsNone(t.update(1.0))
        self.assertIsNone(t.update(2.0))
        seed = t.update(3.0)
        self.assertAlmostEqual(seed, 2.0)  # SMA(1,2,3)
        alpha = 2.0 / 4
        expected = alpha * 6.0 + (1 - alpha) * 2.0
        self.assertAlmostEqual(t.update(6.0), expected)

    def test_wilder_atr_first_bar_uses_high_minus_low(self):
        t = WilderAtrTracker(3)
        t.update(high=10.0, low=9.0, close=9.5)   # TR = 1.0, no prior close
        t.update(high=11.0, low=9.5, close=10.0)  # TR = max(1.5, |11-9.5|=1.5, |9.5-9.5|=0) = 1.5
        v = t.update(high=10.5, low=9.8, close=10.2)  # TR = max(0.7, |10.5-10|=0.5, |9.8-10|=0.2) = 0.7
        self.assertAlmostEqual(v, (1.0 + 1.5 + 0.7) / 3)  # SMA seed at bar 3

    def test_wilder_atr_recurses_after_seed(self):
        t = WilderAtrTracker(2)
        t.update(high=10.0, low=9.0, close=9.5)
        seed = t.update(high=10.0, low=9.0, close=9.5)
        self.assertAlmostEqual(seed, 1.0)
        v = t.update(high=13.0, low=9.0, close=11.0)  # TR = max(4, |13-9.5|=3.5, |9-9.5|=0.5)=4
        self.assertAlmostEqual(v, 0.5 * 4.0 + 0.5 * 1.0)


# ============================================================================
# Module 2 pivot lag: "confirms exactly pivLen bars late, not pivLen-1 or +1"
# ============================================================================
class PivotTrackerTests(unittest.TestCase):
    def test_pivot_confirms_exactly_piv_len_bars_late_default_5(self):
        left = right = 5
        piv = PivotTracker(left, right)
        # bar index 5 (0-based) is the unique max/min of an 11-bar window.
        highs = [1, 2, 3, 4, 5, 6, 5, 4, 3, 2, 1]
        lows = [10, 9, 8, 7, 6, 5, 6, 7, 8, 9, 10]
        results = [piv.update(i, highs[i], lows[i]) for i in range(11)]
        # not confirmed on any bar before the window is fully populated
        for i in range(10):
            self.assertEqual(results[i], (None, None, None), f"bar {i} confirmed too early")
        ph, pl, pivot_bar = results[10]
        self.assertEqual(pivot_bar, 5)          # the extreme bar itself
        self.assertEqual(ph, 6)
        self.assertEqual(pl, 5)
        # confirmation happened while processing bar 10 -> exactly 5 bars
        # after the extreme printed at bar 5, not 4 (bar 9) and not 6 (bar 11)
        self.assertEqual(10 - pivot_bar, 5)

    def test_tied_extreme_is_not_a_pivot(self):
        piv = PivotTracker(1, 1)
        # center bar ties the left bar's high -> not a UNIQUE max
        results = [piv.update(0, 5.0, 1.0), piv.update(1, 5.0, 1.0), piv.update(2, 4.0, 1.0)]
        self.assertEqual(results[-1][0], None)


# ============================================================================
# Module 2 structure: BOS vs MSS, displacement gate
# ============================================================================
class StructureTests(unittest.TestCase):
    def test_bos_fires_without_displacement_mss_suppressed_without_it(self):
        cfg = zero_weight_config(piv_len=1, disp_mult=1.2, need_disp_mss=True, need_disp_bos=False)
        engine = HeffSmcEngine(cfg)
        bars = flat_bars(14, price=100.0, rng=1.0)  # seed ATR(14) ~= 1.0, no pivots (all tied)
        bars += [
            (100.0, 103.0, 99.5, 100.2, 1000.0),   # 14: spike high candidate (tie-free low=99.5)
            (100.2, 100.6, 100.0, 100.3, 1000.0),  # 15: confirms bar14 ph=103 (piv_len=1)
            (100.3, 104.0, 100.2, 103.5, 1000.0),  # 16: first break up (neutral->trend, no event)
            (103.5, 103.6, 103.3, 103.5, 1000.0),  # 17: filler
            (103.5, 106.0, 103.4, 104.0, 1000.0),  # 18: spike high candidate 2
            (104.0, 104.3, 103.9, 104.1, 1000.0),  # 19: confirms bar18 ph=106
            (106.0, 106.3, 105.9, 106.2, 1000.0),  # 20: SMALL-range break of 106 (same trend) -> BOS, no disp needed
            (106.2, 106.4, 106.0, 106.1, 1000.0),  # 21: filler
            (106.1, 106.3, 104.0, 105.5, 1000.0),  # 22: dip candidate (low=104.0)
            (105.5, 105.8, 105.2, 105.6, 1000.0),  # 23: confirms bar22 pl=104.0
            (104.05, 104.1, 103.8, 103.9, 1000.0),  # 24: SMALL-range break of 104 (against trend) -> MSS suppressed.
                                                     #     Its own low (103.8) is a tie-free local min, so it
                                                     #     confirms as a NEW pivot low one bar later (bar 25) --
                                                     #     this is real, intended pivot behavior, not a test bug.
            (103.9, 104.0, 103.85, 103.95, 1000.0),  # 25: confirms bar24 as pl=103.8 (piv_len=1)
            (103.95, 103.98, 102.0, 103.0, 1000.0),  # 26: HUGE-range break of 103.8 (against trend) -> real MSS
        ]
        results = feed_bars(engine, bars)

        self.assertTrue(results[19]["mss_up"] is False and results[19]["bos_up"] is False)  # just confirms pivot, no break yet
        self.assertTrue(results[20]["bos_up"], "BOS must fire without displacement (needDispBOS=False)")
        self.assertFalse(results[20]["mss_up"])
        self.assertEqual(results[20]["trend_dir"], 1)

        self.assertFalse(results[24]["mss_dn"], "small-range break against trend must NOT produce MSS (fakeout kill)")
        self.assertFalse(results[24]["bos_dn"])
        self.assertEqual(results[24]["trend_dir"], 1, "trend must not flip on a non-displaced break")

        self.assertTrue(results[26]["mss_dn"], "large-range break against trend must produce a real MSS")
        self.assertFalse(results[26]["bos_dn"])
        self.assertEqual(results[26]["trend_dir"], -1, "trend must flip on a displaced MSS")
        self.assertEqual(engine.trend_dir, -1)

    def test_neutral_first_break_sets_trend_with_no_event(self):
        cfg = zero_weight_config(piv_len=1)
        engine = HeffSmcEngine(cfg)
        bars = flat_bars(14, price=100.0, rng=1.0)
        bars += [
            (100.0, 103.0, 99.5, 100.2, 1000.0),
            (100.2, 100.6, 100.0, 100.3, 1000.0),
            (100.3, 104.0, 100.2, 103.5, 1000.0),  # first-ever break
        ]
        results = feed_bars(engine, bars)
        last = results[-1]
        self.assertFalse(last["bos_up"])
        self.assertFalse(last["mss_up"])
        self.assertEqual(engine.trend_dir, 1)


# ============================================================================
# Module 3: Fair Value Gaps
# ============================================================================
class FvgTests(unittest.TestCase):
    def test_three_bar_imbalance_sized_and_filtered_by_atr(self):
        cfg = zero_weight_config(piv_len=5, fvg_atr_min=0.15)
        engine = HeffSmcEngine(cfg)
        warm = flat_bars(20, price=100.0, rng=1.0)  # atrBase settles ~1.0
        # bull gap: low > high[2], middle candle bullish, gap size = low - high[2]
        big_gap = [
            (100.0, 100.5, 99.5, 100.0, 1000.0),   # bar N-2: high=100.5
            (100.0, 101.5, 99.9, 101.3, 1000.0),   # bar N-1 (middle, bullish close>open)
            (102.0, 102.5, 101.9, 102.3, 1000.0),  # bar N: low=101.9 > 100.5 -> gap=1.4 >> 0.15*atr
        ]
        results = feed_bars(engine, warm + big_gap)
        self.assertEqual(len(engine.fvgs), 1)
        z = engine.fvgs[0]
        self.assertTrue(z.bull)
        self.assertAlmostEqual(z.top, 101.9)
        self.assertAlmostEqual(z.bot, 100.5)

    def test_micro_gap_filtered_by_atr_minimum(self):
        cfg = zero_weight_config(piv_len=5, fvg_atr_min=0.15)
        engine = HeffSmcEngine(cfg)
        warm = flat_bars(20, price=100.0, rng=1.0)  # atrBase ~= 1.0 -> min gap ~0.15
        tiny_gap = [
            (100.0, 100.5, 99.5, 100.0, 1000.0),
            (100.0, 101.0, 99.9, 100.9, 1000.0),
            (101.0, 101.1, 100.51, 100.6, 1000.0),  # low=100.51 > 100.5 -> gap = 0.01, way under 0.15
        ]
        feed_bars(engine, warm + tiny_gap)
        self.assertEqual(len(engine.fvgs), 0, "gap smaller than fvgAtrMin*ATR must be filtered out")

    def test_fill_state_machine_progresses_and_mitigates_at_50pct(self):
        cfg = zero_weight_config(piv_len=5, fvg_atr_min=0.15, fvg_mitig="50% Fill")
        engine = HeffSmcEngine(cfg)
        warm = flat_bars(20, price=100.0, rng=1.0)
        gap = [
            (100.0, 100.5, 99.5, 100.0, 1000.0),
            (100.0, 101.5, 99.9, 101.3, 1000.0),
            (102.0, 102.5, 101.9, 102.3, 1000.0),  # bull gap top=101.9 bot=100.5, mid=101.2
        ]
        feed_bars(engine, warm + gap)
        z = engine.fvgs[0]
        self.assertEqual(z.state, 0)
        self.assertFalse(z.spent)

        # touch only (low dips to 101.7, still above top -> no, must be <= top for touch/fill checks)
        r_touch = engine.process_bar(_ts(len(warm) + 3), 102.2, 102.3, 101.85, 102.0, 1000.0)
        self.assertGreaterEqual(engine.fvgs[0].state, 1)
        self.assertFalse(engine.fvgs[0].spent, "touch alone must not mitigate under a 50% Fill rule")

        # half fill: low reaches the midpoint (101.2)
        engine.process_bar(_ts(len(warm) + 4), 101.85, 101.9, 101.2, 101.3, 1000.0)
        self.assertEqual(engine.fvgs[0].state, 2)
        self.assertTrue(engine.fvgs[0].spent, "50% Fill rule must mark the gap spent once state reaches 2 (half)")

    def test_inversion_flips_polarity_and_feeds_confluence_new_direction(self):
        cfg = zero_weight_config(piv_len=5, fvg_atr_min=0.15, show_ifvg=True, w_zone=1.0)
        engine = HeffSmcEngine(cfg)
        warm = flat_bars(20, price=100.0, rng=1.0)
        gap = [
            (100.0, 100.5, 99.5, 100.0, 1000.0),
            (100.0, 101.5, 99.9, 101.3, 1000.0),
            (102.0, 102.5, 101.9, 102.3, 1000.0),  # bull gap top=101.9 bot=100.5
        ]
        feed_bars(engine, warm + gap)
        self.assertFalse(engine.fvgs[0].inverted)

        # confirmed close through the far edge (bot=100.5) inverts it
        r = engine.process_bar(_ts(len(warm) + 3), 101.0, 101.1, 100.0, 100.2, 1000.0)
        self.assertTrue(engine.fvgs[0].inverted)
        self.assertFalse(engine.fvgs[0].spent)
        self.assertEqual(engine.fvgs[0].state, 0)

        # now acting as a BEAR zone: a tap from below should feed the SHORT
        # side (zone_short), not the long side, since polarity flipped
        r2 = engine.process_bar(_ts(len(warm) + 4), 100.3, 100.6, 100.2, 100.4, 1000.0)
        self.assertTrue(r2["zone_short"])
        self.assertFalse(r2["zone_long"])


# ============================================================================
# Module 4: Order Blocks
# ============================================================================
class OrderBlockTests(unittest.TestCase):
    def test_last_opposing_candle_within_lookback_becomes_ob(self):
        cfg = zero_weight_config(piv_len=1, ob_lookback=10, disp_mult=1.2, need_disp_bos=False)
        engine = HeffSmcEngine(cfg)
        warm = flat_bars(14, price=100.0, rng=1.0)
        bars = warm + [
            (100.0, 100.2, 98.0, 98.5, 1000.0),    # -6 from break bar: down candle (close<open) -> the OB
            (98.5, 99.5, 98.4, 99.4, 1000.0),      # -5: up candle
            (99.4, 100.0, 99.3, 99.9, 1000.0),     # -4: up candle
            (99.9, 100.4, 99.8, 100.3, 1000.0),    # -3: up candle
            (100.3, 100.8, 100.2, 100.7, 1000.0),  # -2: up candle
            (100.7, 103.0, 100.6, 100.8, 1000.0),  # -1: spike high candidate, up candle
            (100.8, 101.0, 100.7, 100.9, 1000.0),  # confirms pivot high (piv_len=1) at bar -1 (h=103 from prior)
        ]
        # confirm pivot high 103 at index len(warm)+5 (the spike bar), then break it with displacement
        break_bar = (100.9, 108.0, 100.8, 107.5, 1000.0)  # huge range, breaks 103 with disp, trend was 0 -> sets trend, no BOS/MSS yet
        # need an established trend first: feed break to set trend=1 (no event), then reform a pivot and break again for real BOS
        results = feed_bars(engine, bars + [break_bar])
        self.assertEqual(engine.trend_dir, 1)
        self.assertEqual(len(engine.obs), 0, "first break from neutral must not create an OB (no BOS/MSS event)")

        # now reform a pivot high above and break it WITH displacement for a real BOS -> OB should form
        more = [
            (107.5, 107.6, 107.3, 107.4, 1000.0),
            (107.4, 107.5, 105.0, 105.5, 1000.0),   # down candle (close 105.5 < open 107.4) -- should become the OB
            (105.5, 111.0, 105.4, 106.0, 1000.0),   # spike high candidate
            (106.0, 106.2, 105.9, 106.1, 1000.0),   # confirms pivot high 111
            (106.1, 118.0, 106.0, 117.0, 1000.0),   # huge-range break of 111 (same trend=1) -> BOS with displacement
        ]
        results2 = feed_bars(engine, more, new_session=False)
        self.assertTrue(results2[-1]["bos_up"])
        self.assertEqual(len(engine.obs), 1)
        ob = engine.obs[0]
        self.assertTrue(ob.bull)
        # the last (closest) down candle before the breakout bar: high/low of that candle
        self.assertAlmostEqual(ob.top, 107.5)
        self.assertAlmostEqual(ob.bot, 105.0)

    def test_bearish_ob_from_last_up_candle_on_a_displaced_down_break(self):
        """Mirror of the bullish-OB test: a displaced break DOWN must scan
        back for the last UP candle (close>open) as the bearish OB."""
        cfg = zero_weight_config(piv_len=1, ob_lookback=10, disp_mult=1.2, need_disp_bos=False)
        engine = HeffSmcEngine(cfg)
        warm = flat_bars(14, price=200.0, rng=1.0)
        # establish a downtrend from neutral (first break, no event)
        bars = warm + [
            (200.0, 200.5, 197.0, 199.8, 1000.0),   # spike low candidate
            (199.8, 200.0, 199.5, 199.7, 1000.0),   # confirms pivot low 197 (piv_len=1)
            (199.7, 199.8, 196.0, 196.5, 1000.0),   # huge break down of 197 -> sets trend=-1, no event
        ]
        results = feed_bars(engine, bars)
        self.assertEqual(engine.trend_dir, -1)
        self.assertEqual(len(engine.obs), 0)

        # NOTE: bar16's own low (196.0) is itself a tie-free local min, so it
        # confirms as a NEW pivot low one bar later (real, intended pivot
        # behavior -- see the analogous note in the bullish structure test
        # above). That reformed active_low is what the very next break
        # actually breaks, one bar sooner than a naive count would suggest.
        more = [
            (196.5, 196.6, 196.4, 196.5, 1000.0),   # confirms bar16 as pl=196.0 (piv_len=1)
            (196.5, 198.0, 196.4, 197.5, 1000.0),   # up candle (close 197.5 > open 196.5) -- should become the bearish OB
            (197.5, 197.6, 193.0, 194.0, 1000.0),   # HUGE-range break of 196.0 (same trend=-1) -> BOS with displacement
        ]
        results2 = feed_bars(engine, more, new_session=False)
        self.assertTrue(results2[-1]["bos_dn"])
        self.assertEqual(len(engine.obs), 1)
        ob = engine.obs[0]
        self.assertFalse(ob.bull)
        self.assertAlmostEqual(ob.top, 198.0)
        self.assertAlmostEqual(ob.bot, 196.4)


# ============================================================================
# Module 5: Liquidity -- sweeps and sweep-and-reclaim
# ============================================================================
class LiquidityTests(unittest.TestCase):
    def test_wick_through_and_close_back_inside_is_a_sweep_not_any_wick(self):
        cfg = zero_weight_config(piv_len=1, eq_tol=0.10)
        engine = HeffSmcEngine(cfg)
        warm = flat_bars(20, price=100.0, rng=1.0)
        # build two equal-high pivots to form a pool at ~105
        pool_setup = [
            (100.0, 101.0, 99.5, 100.5, 1000.0),
            (100.5, 105.0, 100.4, 101.0, 1000.0),   # pivot high candidate #1 @105
            (101.0, 101.2, 100.9, 101.1, 1000.0),   # confirms ph=105 (piv_len=1)
            (101.1, 101.3, 101.0, 101.2, 1000.0),
            (101.2, 105.02, 101.1, 101.5, 1000.0),  # pivot high candidate #2 @105.02 (within eq tol)
            (101.5, 101.6, 101.4, 101.5, 1000.0),   # confirms ph=105.02 -> should JOIN pool with #1
        ]
        feed_bars(engine, warm + pool_setup)
        self.assertEqual(len(engine.pools), 1, "two equal pivot highs within tolerance must form ONE pool")
        pool_level = engine.pools[0].level

        # a bar that wicks through but closes back inside -> a real sweep
        r_sweep = engine.process_bar(_ts(100), 101.5, pool_level + 0.5, 101.4, pool_level - 0.3, 1000.0)
        self.assertTrue(r_sweep["sweep_reclaim_long"] is False)  # not yet, this is the sweep bar itself
        armed = _armed_high(engine)
        self.assertTrue(armed is not None and armed.born == r_sweep["bar_index"])
        self.assertEqual(armed.src, "POOL")
        self.assertTrue(all(p.dead for p in engine.pools if p.is_high))

    def test_clean_close_through_consumes_pool_without_a_sweep(self):
        cfg = zero_weight_config(piv_len=1, eq_tol=0.10)
        engine = HeffSmcEngine(cfg)
        warm = flat_bars(20, price=100.0, rng=1.0)
        pool_setup = [
            (100.0, 101.0, 99.5, 100.5, 1000.0),
            (100.5, 105.0, 100.4, 101.0, 1000.0),
            (101.0, 101.2, 100.9, 101.1, 1000.0),
            (101.1, 101.3, 101.0, 101.2, 1000.0),
            (101.2, 105.02, 101.1, 101.5, 1000.0),
            (101.5, 101.6, 101.4, 101.5, 1000.0),
        ]
        feed_bars(engine, warm + pool_setup)
        pool_level = engine.pools[0].level
        # closes cleanly ABOVE the level (no wick-back-under) -> consumed, no sweep, nothing armed
        engine.process_bar(_ts(100), 101.5, pool_level + 1.0, 101.4, pool_level + 0.8, 1000.0)
        self.assertIsNone(_armed_high(engine))
        self.assertTrue(all(p.dead for p in engine.pools if p.is_high))

    def test_sweep_and_reclaim_within_window_and_cancellation_beyond_it(self):
        cfg = zero_weight_config(piv_len=1, eq_tol=0.10, reclaim_win=3)
        engine = HeffSmcEngine(cfg)
        warm = flat_bars(20, price=100.0, rng=1.0)
        pool_setup = [
            (100.0, 101.0, 99.5, 100.5, 1000.0),
            (100.5, 105.0, 100.4, 101.0, 1000.0),
            (101.0, 101.2, 100.9, 101.1, 1000.0),
            (101.1, 101.3, 101.0, 101.2, 1000.0),
            (101.2, 105.02, 101.1, 101.5, 1000.0),
            (101.5, 101.6, 101.4, 101.5, 1000.0),
        ]
        feed_bars(engine, warm + pool_setup)
        pool_level = engine.pools[0].level
        sweep = engine.process_bar(_ts(100), 101.5, pool_level + 0.5, 101.4, pool_level - 0.3, 1000.0)
        sweep_bar = sweep["bar_index"]

        # next bar: close still below the level -> reclaim fires immediately (sweep bar + 1)
        r = engine.process_bar(_ts(101), pool_level - 0.3, pool_level - 0.1, pool_level - 0.5, pool_level - 0.4, 1000.0)
        self.assertTrue(r["sweep_reclaim_short"])
        self.assertIsNone(_armed_high(engine))

    def test_reclaim_cancelled_if_price_closes_back_beyond_level(self):
        cfg = zero_weight_config(piv_len=1, eq_tol=0.10, reclaim_win=3)
        engine = HeffSmcEngine(cfg)
        warm = flat_bars(20, price=100.0, rng=1.0)
        pool_setup = [
            (100.0, 101.0, 99.5, 100.5, 1000.0),
            (100.5, 105.0, 100.4, 101.0, 1000.0),
            (101.0, 101.2, 100.9, 101.1, 1000.0),
            (101.1, 101.3, 101.0, 101.2, 1000.0),
            (101.2, 105.02, 101.1, 101.5, 1000.0),
            (101.5, 101.6, 101.4, 101.5, 1000.0),
        ]
        feed_bars(engine, warm + pool_setup)
        pool_level = engine.pools[0].level
        engine.process_bar(_ts(100), 101.5, pool_level + 0.5, 101.4, pool_level - 0.3, 1000.0)
        # next bar closes back ABOVE the level -> cancels the reclaim watch, no short fires
        r = engine.process_bar(_ts(101), pool_level - 0.3, pool_level + 0.6, pool_level - 0.4, pool_level + 0.5, 1000.0)
        self.assertFalse(r["sweep_reclaim_short"])
        self.assertIsNone(_armed_high(engine))

    def test_reclaim_expires_after_window(self):
        cfg = zero_weight_config(piv_len=1, eq_tol=0.10, reclaim_win=2)
        engine = HeffSmcEngine(cfg)
        warm = flat_bars(20, price=100.0, rng=1.0)
        pool_setup = [
            (100.0, 101.0, 99.5, 100.5, 1000.0),
            (100.5, 105.0, 100.4, 101.0, 1000.0),
            (101.0, 101.2, 100.9, 101.1, 1000.0),
            (101.1, 101.3, 101.0, 101.2, 1000.0),
            (101.2, 105.02, 101.1, 101.5, 1000.0),
            (101.5, 101.6, 101.4, 101.5, 1000.0),
        ]
        feed_bars(engine, warm + pool_setup)
        pool_level = engine.pools[0].level
        engine.process_bar(_ts(100), 101.5, pool_level + 0.5, 101.4, pool_level - 0.3, 1000.0)
        # stay exactly AT the level (neither < nor >, so neither the reclaim
        # nor the cancel branch fires on its own) for reclaim_win bars, then
        # one more bar pushes bar_index - born strictly past the
        # window (reclaim_win=2 -> expires once the gap exceeds 2, i.e. the
        # 3rd bar after the sweep)
        engine.process_bar(_ts(101), pool_level, pool_level, pool_level, pool_level, 1000.0)
        engine.process_bar(_ts(102), pool_level, pool_level, pool_level, pool_level, 1000.0)
        r = engine.process_bar(_ts(103), pool_level, pool_level, pool_level, pool_level, 1000.0)
        self.assertFalse(r["sweep_reclaim_short"])
        self.assertIsNone(_armed_high(engine), "pending watch must expire once bar_index - born > reclaim_win")


# ============================================================================
# Module 7.5: pullback / 200MA fade triggers
# ============================================================================
class PullbackFadeTests(unittest.TestCase):
    def test_pullback_cooldown_suppresses_second_fire_within_window(self):
        cfg = zero_weight_config(piv_len=1, trig_cooldown=5, ma_fast=3)
        engine = HeffSmcEngine(cfg)
        warm = flat_bars(14, price=100.0, rng=1.0)
        # establish an uptrend
        trend_setup = [
            (100.0, 103.0, 99.5, 100.2, 1000.0),
            (100.2, 100.6, 100.0, 100.3, 1000.0),
            (100.3, 104.0, 100.2, 103.5, 1000.0),  # first break sets trend=1
        ]
        results = feed_bars(engine, warm + trend_setup)
        self.assertEqual(engine.trend_dir, 1)

        # feed bars that repeatedly touch the fast MA and close back above with a bullish candle
        pulls = []
        for i in range(8):
            pulls.append((103.5, 104.0, 103.0, 103.8, 1000.0))
        results2 = feed_bars(engine, pulls, new_session=False)
        fire_indices = [i for i, r in enumerate(results2) if r["pull_long"]]
        self.assertTrue(len(fire_indices) >= 1)
        first_fire = fire_indices[0]
        for i in range(first_fire + 1, min(first_fire + 1 + cfg.trig_cooldown, len(results2))):
            self.assertFalse(results2[i]["pull_long"], f"cooldown must suppress a fire at offset {i - first_fire}")

    def test_200ma_fade_requires_prior_bar_on_opposite_side(self):
        cfg = zero_weight_config(piv_len=5, ma_slow=5, fade_buf=0.5, trig_cooldown=1)
        engine = HeffSmcEngine(cfg)
        # seed the slow MA with a flat run at 100 so ma_s ~= 100
        warm = flat_bars(20, price=100.0, rng=0.2)
        feed_bars(engine, warm)
        ma_s_now = engine.ema_s.value
        self.assertIsNotNone(ma_s_now)

        # prior close ALREADY below the MA (already riding it) -> not a genuine first touch
        r_already_below = engine.process_bar(
            _ts(20), ma_s_now - 0.2, ma_s_now - 0.1, ma_s_now - 0.3, ma_s_now - 0.15, 1000.0,
        )
        self.assertLess(r_already_below["c"], ma_s_now)
        # follow with a bar that otherwise looks exactly like a fade-long shape
        # (low near the MA, closes back above it, bullish candle)
        r_next = engine.process_bar(
            _ts(21), ma_s_now - 0.1, ma_s_now + 0.1, ma_s_now - 0.4, ma_s_now + 0.05, 1000.0,
        )
        # since close[1] (r_already_below's close) is ALSO below ma_s (not
        # above), fade_long's "prior close > maS" genuine-first-touch
        # condition must be false here for r_next
        self.assertFalse(r_next["fade_long"])

    def test_200ma_fade_fires_on_genuine_first_touch_from_above(self):
        cfg = zero_weight_config(piv_len=5, ma_slow=5, fade_buf=0.5, trig_cooldown=1)
        engine = HeffSmcEngine(cfg)
        warm = flat_bars(20, price=100.0, rng=0.2)
        feed_bars(engine, warm)
        ma_s_now = engine.ema_s.value

        # prior close genuinely ABOVE the MA (real approach from above)
        r_above = engine.process_bar(_ts(20), ma_s_now + 0.2, ma_s_now + 0.3, ma_s_now + 0.1, ma_s_now + 0.15, 1000.0)
        self.assertGreater(r_above["c"], ma_s_now)
        # this bar dips to touch the MA (within fadeBuf*atr) and rejects back
        # up with a clear margin -- the EMA itself updates using THIS bar's
        # own close (ta.ema is self-referential), so the close needs enough
        # separation to still clear the just-updated maS, not merely the
        # pre-bar snapshot ma_s_now
        r_fade = engine.process_bar(_ts(21), ma_s_now - 0.1, ma_s_now + 0.3, ma_s_now - 0.4, ma_s_now + 0.2, 1000.0)
        self.assertGreater(r_fade["c"], r_fade["ma_slow"])
        self.assertTrue(r_fade["fade_long"], "genuine first-touch-from-above must fire fade_long")


# ============================================================================
# Module 8: weighted confluence -- MA-stack partial credit, RVOL spike bonus,
# HTF gate trigger-aware exemptions
# ============================================================================
class ConfluenceWeightTests(unittest.TestCase):
    def test_ma_stack_partial_credit_half_weight_when_only_fast_over_mid(self):
        cfg = HeffSmcConfig(
            w_mss=0.0, w_bos=0.0, w_zone=0.0, w_sweep=0.0, w_pd=0.0, w_rvol=0.0,
            w_htf=0.0, w_kz=0.0, w_pull=0.0, w_fade=0.0, w_ma=1.0, min_score=0.0,
            htf_gate_mode="Off", ma_fast=2, ma_mid=4, ma_slow=20,
        )
        engine = HeffSmcEngine(cfg)
        # a long downtrend bunches the MAs bear-stacked (slow highest, lagging
        # the most), then a sharp reversal up: the fast MA (reacts quickest)
        # crosses above mid BEFORE mid manages to cross the much-slower-moving
        # slow MA -- there must be at least one bar where fast>mid but the
        # full stack (mid>slow too) isn't true yet.
        down = [(100 - i, 100.5 - i, 99.5 - i, 100 - i, 1000.0) for i in range(30)]
        up = [(70 + i, 70.5 + i, 69.5 + i, 70 + i, 1000.0) for i in range(10)]
        results = feed_bars(engine, down + up)
        half_credit_bars = [
            r for r in results
            if r["ma_fast"] is not None and r["ma_mid"] is not None and r["ma_slow"] is not None
            and r["ma_fast"] > r["ma_mid"] and not r["bull_stack"]
        ]
        self.assertTrue(half_credit_bars, "expected at least one bar where fast>mid but not fully stacked")
        for r in half_credit_bars:
            self.assertAlmostEqual(r["factors_long"]["ma_stack"], 0.5)

    def test_ma_stack_full_credit_when_fully_stacked(self):
        cfg = HeffSmcConfig(
            w_mss=0.0, w_bos=0.0, w_zone=0.0, w_sweep=0.0, w_pd=0.0, w_rvol=0.0,
            w_htf=0.0, w_kz=0.0, w_pull=0.0, w_fade=0.0, w_ma=1.0, min_score=0.0,
            htf_gate_mode="Off", ma_fast=3, ma_mid=5, ma_slow=8,
        )
        engine = HeffSmcEngine(cfg)
        bars = [(100 + i, 100.5 + i, 99.5 + i, 100 + i, 1000.0) for i in range(30)]
        results = feed_bars(engine, bars)
        last = results[-1]
        self.assertTrue(last["bull_stack"])
        self.assertAlmostEqual(last["factors_long"]["ma_stack"], 1.0)

    def _rvol_config(self, rvol_len=3, rvol_mult=1.4):
        return HeffSmcConfig(
            w_mss=0.0, w_bos=0.0, w_zone=0.0, w_sweep=0.0, w_pd=0.0, w_ma=0.0,
            w_htf=0.0, w_kz=0.0, w_pull=0.0, w_fade=0.0, w_rvol=1.0, min_score=0.0,
            htf_gate_mode="Off", rvol_len=rvol_len, rvol_mult=rvol_mult,
        )

    def test_rvol_base_weight_exactly_at_threshold(self):
        # avgVol (ta.sma semantics) includes the CURRENT bar's own volume,
        # same as Pine's ta.sma(volume, rvolLen) -- so with 2 warm bars at
        # vol=100 and this bar's own v solved so rvol == rvol_mult exactly:
        # v / ((100+100+v)/3) = 1.4  =>  v = 175, avg = 125, rvol = 1.4
        cfg = self._rvol_config(rvol_len=3, rvol_mult=1.4)
        engine = HeffSmcEngine(cfg)
        warm = [(100.0, 100.5, 99.5, 100.0, 100.0) for _ in range(2)]
        feed_bars(engine, warm)
        r = engine.process_bar(_ts(2), 100.0, 100.5, 99.5, 100.0, 175.0)
        self.assertAlmostEqual(r["rvol"], 1.4, places=6)
        self.assertAlmostEqual(r["factors_long"]["rvol"], 1.0)

    def test_rvol_spike_bonus_1_5x_at_double_threshold(self):
        # v / ((100+100+v)/3) = 2.8  =>  v = 2800, avg = 1000, rvol = 2.8
        cfg = self._rvol_config(rvol_len=3, rvol_mult=1.4)
        engine = HeffSmcEngine(cfg)
        warm = [(100.0, 100.5, 99.5, 100.0, 100.0) for _ in range(2)]
        feed_bars(engine, warm)
        r = engine.process_bar(_ts(2), 100.0, 100.5, 99.5, 100.0, 2800.0)
        self.assertAlmostEqual(r["rvol"], 2.8, places=6)
        self.assertAlmostEqual(r["factors_long"]["rvol"], 1.5, msg="2x the RVOL threshold must pay 1.5x weight, not just 1.0x")

    def test_rvol_below_threshold_pays_zero(self):
        cfg = self._rvol_config(rvol_len=3, rvol_mult=1.4)
        engine = HeffSmcEngine(cfg)
        warm = [(100.0, 100.5, 99.5, 100.0, 100.0) for _ in range(2)]
        feed_bars(engine, warm)
        r = engine.process_bar(_ts(2), 100.0, 100.5, 99.5, 100.0, 100.0)  # rvol well under threshold
        self.assertLess(r["rvol"], 1.4)
        self.assertAlmostEqual(r["factors_long"]["rvol"], 0.0)

    def test_htf_gate_continuation_only_exempts_reversal_triggers(self):
        """Continuation-only gate: BOS/pullback need htfBias>=0 for longs,
        MSS/sweep-reclaim/200MA-fade are exempt."""
        cfg = zero_weight_config(
            piv_len=1, w_mss=5.0, w_bos=5.0, min_score=1.0, htf_gate_mode="Continuation only",
        )
        engine = HeffSmcEngine(cfg, htf_dir_lookup=lambda i: (-1, -1))  # net-bearish HTF throughout
        warm = flat_bars(14, price=100.0, rng=1.0)
        bars = warm + [
            (100.0, 103.0, 99.5, 100.2, 1000.0),
            (100.2, 100.6, 100.0, 100.3, 1000.0),
            (100.3, 104.0, 100.2, 103.5, 1000.0),  # sets trend=1 (neutral start, no event)
        ]
        results = feed_bars(engine, bars)
        self.assertEqual(engine.trend_dir, 1)

        # reform a pivot high and break it (same trend) with big displacement for a real BOS
        more = [
            (103.5, 103.6, 103.3, 103.5, 1000.0),
            (103.5, 106.0, 103.4, 104.0, 1000.0),
            (104.0, 104.3, 103.9, 104.1, 1000.0),  # confirms ph=106
            (104.1, 120.0, 104.0, 119.0, 1000.0),  # huge break -> BOS, but HTF is net-bearish
        ]
        results2 = feed_bars(engine, more, new_session=False)
        bos_result = results2[-1]
        self.assertTrue(bos_result["bos_up"])
        self.assertFalse(bos_result["htf_ok_long"], "BOS (continuation) must be blocked by a net-opposing HTF bias")
        self.assertFalse(bos_result["long_signal"])

    def test_htf_gate_exempts_mss_even_when_htf_opposes(self):
        cfg = zero_weight_config(piv_len=1, w_mss=5.0, min_score=1.0, htf_gate_mode="Continuation only")
        engine = HeffSmcEngine(cfg, htf_dir_lookup=lambda i: (-1, -1))  # net-bearish throughout
        warm = flat_bars(14, price=100.0, rng=1.0)
        bars = warm + [
            (100.0, 103.0, 99.5, 100.2, 1000.0),
            (100.2, 100.6, 100.0, 100.3, 1000.0),
            (100.3, 104.0, 100.2, 103.5, 1000.0),  # trend=1
        ]
        feed_bars(engine, bars)
        more = [
            (103.5, 103.6, 103.3, 103.5, 1000.0),
            (103.5, 106.0, 103.4, 104.0, 1000.0),
            (104.0, 104.3, 103.9, 104.1, 1000.0),   # confirms ph=106
            (104.1, 103.9, 100.0, 100.5, 1000.0),   # dip below active_low? need a low pivot first, use a big down break instead
        ]
        # simpler: directly force a down-break against trend for MSS
        low_setup = [
            (104.1, 104.3, 104.0, 104.2, 1000.0),
            (104.2, 104.3, 101.0, 101.5, 1000.0),   # low pivot candidate
            (101.5, 101.7, 101.4, 101.6, 1000.0),   # confirms pl
            (101.6, 101.7, 90.0, 91.0, 1000.0),     # huge displaced break down -> MSS (reversal, HTF-exempt)
        ]
        results = feed_bars(engine, more[:3] + low_setup, new_session=False)
        mss_result = results[-1]
        self.assertTrue(mss_result["mss_dn"])
        self.assertTrue(mss_result["htf_ok_short"], "MSS is reversal-class and must be exempt from the continuation-only HTF gate")
        self.assertTrue(mss_result["short_signal"])


if __name__ == "__main__":
    unittest.main()
