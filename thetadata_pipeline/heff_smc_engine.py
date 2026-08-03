"""Faithful Python port of HEFF_SMC_V2_REFERENCE.pine (Modules 1-6, 7.5, 8, 10)
-- BT-3 B1's real, historical, non-repainting replay engine for heff's live
"HEFF SMC Confluence v2" TradingView indicator.

Ground truth is the .pine file itself (1041 lines, read in full before this
was written), NOT this docstring or any prompt summary. Every module below is
commented with the exact Pine block it mirrors so a future reader can
line-check this against the .pine source directly.

NON-REPAINTING (honest / aggrMode=false) ONLY. Every bar handed to
process_bar() is treated as a CONFIRMED, CLOSED bar (evalOK = True always) --
this is correct for a historical replay, exactly matching the .pine file's
own contract ("historical bars are always confirmed, so replay == live").

Continuous state across sessions: bar_index, trendDir, live FVGs/OBs/pools,
and HTF bias all persist across day boundaries, matching Pine's own `var`
scoping (the live indicator's state is never reset intraday-to-intraday
either -- it's one continuous script history). Only a few things reset per
session, exactly as coded in the .pine file: running session H/L
(sessH/sessL) and the PDH/PDL/ONH/ONL "swept once per day" flags. The
replay driver (heff_smc_replay.py) calls `engine.start_new_session(pdh, pdl)`
once per session boundary to apply exactly those resets.

Known, explicitly out-of-scope gaps (see module docstrings below for detail):
  - GEX levels (Module 5's gex1/gex2/gex3 manual inputs): forced to 0 in
    every replay bar, i.e. INACTIVE for the entire historical replay -- there
    is no historical log of heff's daily manual GEX entries. This matches
    the .pine file's own "0 = hidden" convention, so the GEX-sweep code path
    is naturally a no-op here, not a special case.
  - ONH/ONL (overnight high/low): the replay is built on RTH-only 1-min bars
    (see qqq_bars_fetch.py's own docstring for why) -- ONH/ONL stay
    permanently None, which the .pine file's own docstring documents as
    expected on an RTH-only chart, not a bug.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from collections import deque
from typing import Optional
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

RIGHT_LONG = "long"
RIGHT_SHORT = "short"

TRIG_MSS = "MSS"
TRIG_BOS = "BOS"
TRIG_SWEEP_RECLAIM = "SWEEP_RECLAIM"
TRIG_PULLBACK = "PULLBACK"
TRIG_MA_FADE = "MA_FADE"

QQQ_MINTICK = 0.01

# v2.2 sweep-source tags. Carried into the alert payload's `trigger_src` field so
# a reclaim names which liquidity it reclaimed. No tag is a substring of another,
# which is what makes the join/dedupe in the resolution loop safe.
SRC_POOL = "POOL"
SRC_PDH = "PDH"
SRC_PDL = "PDL"
SRC_SESSION = "SESSION"
SRC_GEX = "GEX"

# v2.2 sweep-reclaim confirmation modes (opt-in; default is the v2.1 rule).
SWEEP_CONFIRM_V21 = "Close back inside (v2.1)"
SWEEP_CONFIRM_EXT = "Beyond sweep extreme"
SWEEP_CONFIRM_EXT_DISP = "Beyond sweep extreme + displacement"



# ============================================================================
# CONFIG -- every field is a direct port of the .pine file's `input.*` line,
# same name/default (see the file's own INPUTS section, lines 43-132).
# ============================================================================
@dataclasses.dataclass(frozen=True)
class HeffSmcConfig:
    # Structure (grpS)
    piv_len: int = 5
    struc_src: str = "Close"          # "Close" | "Wick"
    need_disp_mss: bool = True
    need_disp_bos: bool = False
    disp_mult: float = 1.2
    # Fair Value Gaps (grpF)
    fvg_atr_min: float = 0.15
    fvg_mitig: str = "50% Fill"        # "Touch" | "50% Fill" | "Full Fill"
    show_ifvg: bool = True
    fvg_max_age: int = 120
    max_fvg_keep: int = 40
    # Order Blocks (grpO)
    ob_lookback: int = 10
    ob_body_only: bool = False
    ob_max_age: int = 240
    max_ob_keep: int = 20
    # Liquidity (grpL)
    eq_tol: float = 0.10
    max_pools: int = 20
    reclaim_win: int = 3
    # v2.2 OPT-IN: default reproduces v2.1 exactly. See AUDIT_RESPONSE_v2.2.md #4.
    sweep_confirm: str = SWEEP_CONFIRM_V21
    sweep_disp_mult: float = 0.8
    show_pdhl: bool = True
    show_onhl: bool = True             # forced structurally inert: RTH-only replay, no ETH bars
    show_sesshl: bool = True
    # Pullback / Fade (grpT)
    trig_cooldown: int = 5
    fade_buf: float = 0.1
    # HTF Bias (grpH)
    htf_gate_mode: str = "Continuation only"   # "All triggers" | "Continuation only" | "Off"
    htf_tf1_minutes: int = 5
    htf_tf2_minutes: int = 15
    htf_piv_len: int = 3
    # Moving averages (grpM)
    ma_fast: int = 21
    ma_mid: int = 50
    ma_slow: int = 200
    ma_type: str = "EMA"               # "EMA" | "SMA"
    # Kill zone (grpK)
    kz_start: dt.time = dt.time(9, 30)
    kz_end: dt.time = dt.time(11, 0)
    kz_require: bool = False
    # Confluence weights (grpC)
    w_mss: float = 2.5
    w_bos: float = 1.0
    w_zone: float = 1.5
    w_sweep: float = 2.0
    w_pd: float = 1.0
    w_ma: float = 1.0
    w_rvol: float = 1.0
    w_htf: float = 1.5
    w_kz: float = 0.5
    w_pull: float = 1.5
    w_fade: float = 1.5
    rvol_len: int = 50
    rvol_mult: float = 1.4
    gate_ma: bool = False
    min_score: float = 5.0
    mintick: float = QQQ_MINTICK


# ============================================================================
# CORE SERIES TRACKERS (Module 1)
# ============================================================================
class SmaTracker:
    def __init__(self, length: int):
        self.length = length
        self.buf: deque = deque(maxlen=length)

    def update(self, src: float) -> Optional[float]:
        self.buf.append(src)
        if len(self.buf) < self.length:
            return None
        return sum(self.buf) / self.length


class EmaTracker:
    """ta.ema: SMA-seeded, then the standard exponential recursion --
    matches Pine's own na(ema[1]) ? sma(...) : alpha*src+(1-alpha)*ema[1]."""

    def __init__(self, length: int):
        self.length = length
        self.buf: deque = deque(maxlen=length)
        self.value: Optional[float] = None

    def update(self, src: float) -> Optional[float]:
        if self.value is None:
            self.buf.append(src)
            if len(self.buf) == self.length:
                self.value = sum(self.buf) / self.length
            return self.value
        alpha = 2.0 / (self.length + 1)
        self.value = alpha * src + (1 - alpha) * self.value
        return self.value


class WilderAtrTracker:
    """ta.atr(length) = ta.rma(true_range, length): SMA-seeded, then
    alpha=1/length exponential recursion. First bar's TR uses high-low only
    (no prior close to compare against), matching nz()-safe TR."""

    def __init__(self, length: int = 14):
        self.length = length
        self.buf: deque = deque(maxlen=length)
        self.value: Optional[float] = None
        self._prev_close: Optional[float] = None

    def update(self, high: float, low: float, close: float) -> Optional[float]:
        if self._prev_close is None:
            tr = high - low
        else:
            tr = max(high - low, abs(high - self._prev_close), abs(low - self._prev_close))
        self._prev_close = close
        if self.value is None:
            self.buf.append(tr)
            if len(self.buf) == self.length:
                self.value = sum(self.buf) / self.length
            return self.value
        alpha = 1.0 / self.length
        self.value = alpha * tr + (1 - alpha) * self.value
        return self.value


class PivotTracker:
    """ta.pivothigh/low(left, right): a pivot at the window center confirms
    once `right` bars exist after it. Valid only if the center is the
    STRICT (unique) extreme across the full left+1+right window -- this is
    this port's documented best-understanding of Pine's tie-breaking
    behavior (not independently verifiable without a live Pine environment;
    flagged in the B1 report). Returns (pivot_high, pivot_low, pivot_bar_index)
    every call once the window is warm; both pivot values may be None."""

    def __init__(self, left: int, right: int):
        self.left = left
        self.right = right
        self.window: deque = deque(maxlen=left + right + 1)

    def update(self, bar_index: int, high: float, low: float):
        self.window.append((bar_index, high, low))
        if len(self.window) < self.window.maxlen:
            return None, None, None
        items = list(self.window)
        center_bar, center_high, center_low = items[self.left]
        highs = [h for _, h, _ in items]
        lows = [l for _, _, l in items]
        ph = center_high if (center_high == max(highs) and highs.count(center_high) == 1) else None
        pl = center_low if (center_low == min(lows) and lows.count(center_low) == 1) else None
        return ph, pl, center_bar


# ============================================================================
# MODULE STATE OBJECTS (explicit, inspectable -- matches bt2_selector.py /
# bt2_exits.py's dataclass-state convention)
# ============================================================================
@dataclasses.dataclass
class FvgZone:
    top: float
    bot: float
    bull: bool
    inverted: bool = False
    spent: bool = False
    dead: bool = False
    state: int = 0
    born: int = 0


@dataclasses.dataclass
class OrderBlock:
    top: float
    bot: float
    bull: bool
    tapped: bool = False
    dead: bool = False
    born: int = 0



@dataclasses.dataclass
class PendSweep:
    """One armed-but-unconfirmed liquidity sweep, awaiting its reclaim window.

    v2.1 kept a single pend_hi/pend_lo pair that every source overwrote; this
    replaces it so each sweep confirms or expires on its own. `ext` is the sweep
    bar's FAR extreme (the low of a high-sweep, the high of a low-sweep), needed
    by the stricter opt-in confirmation modes."""
    level: float
    ext: float
    is_high: bool
    src: str
    born: int


@dataclasses.dataclass
class LiquidityPool:
    level: float
    is_high: bool
    count: int = 1
    swept: bool = False
    dead: bool = False
    born: int = 0


@dataclasses.dataclass
class Bar:
    t: object
    o: float
    h: float
    l: float
    c: float
    v: float


def _ma_type_tracker(ma_type: str, length: int):
    return EmaTracker(length) if ma_type == "EMA" else SmaTracker(length)


class HeffSmcEngine:
    """Bar-by-bar stateful replay of the .pine file's Modules 1-6, 7.5, 8, 10
    (honest / non-repainting mode only). One instance = one continuous
    replay across every session handed to it, in chronological order --
    call start_new_session() at each session boundary for the small set of
    per-day resets the .pine file itself applies (see class docstring)."""

    def __init__(self, config: HeffSmcConfig = HeffSmcConfig(), htf_dir_lookup=None):
        self.cfg = config
        # htf_dir_lookup(bar_index) -> (htf_dir1, htf_dir2), precomputed by
        # heff_smc_htf.py and injected here -- Module 7's request.security
        # call is, by construction, an EXTERNAL series read, not something
        # this bar-by-bar engine can derive from its own 1-min state.
        self._htf_dir_lookup = htf_dir_lookup or (lambda bar_index: (0, 0))

        self.bar_index = -1
        self.history: deque = deque(maxlen=max(config.ob_lookback, 3) + 2)

        # Module 1
        self.atr = WilderAtrTracker(14)
        self._atr_prev: Optional[float] = None
        self.ema_f = EmaTracker(config.ma_fast)
        self.sma_f = SmaTracker(config.ma_fast)
        self.ema_m = EmaTracker(config.ma_mid)
        self.sma_m = SmaTracker(config.ma_mid)
        self.ema_s = EmaTracker(config.ma_slow)
        self.sma_s = SmaTracker(config.ma_slow)
        self.avg_vol = SmaTracker(config.rvol_len)
        self._prev_close: Optional[float] = None
        self._prev_ma_fast: Optional[float] = None  # for pullback/fade "was on the other side" checks

        # Module 2 -- structure
        self.piv = PivotTracker(config.piv_len, config.piv_len)
        self.last_ph: Optional[float] = None
        self.last_ph_bar: Optional[int] = None
        self.last_pl: Optional[float] = None
        self.last_pl_bar: Optional[int] = None
        self.active_high: Optional[float] = None
        self.active_high_bar: Optional[int] = None
        self.active_low: Optional[float] = None
        self.active_low_bar: Optional[int] = None
        self.trend_dir = 0
        # v2.2 diagnostic: bars where one candle broke BOTH active levels. Expected
        # to be 0 with struc_src="Close" (the live default) and non-zero only in
        # Wick mode -- if this is ever non-zero on a Close-mode run, the assumption
        # that the fix is behaviour-neutral there is wrong and should be re-examined.
        self.dual_break_bars = 0
        # v2.2 diagnostic: zone taps that v2.1 WOULD have credited on the same bar
        # that destroyed (or inverted) the zone. Unlike the dual-break counters this
        # is expected to be non-zero on real data -- it is the fix that moves B1.
        self.zone_credit_suppressed = 0

        # Module 3 -- FVGs
        self.fvgs: list = []

        # Module 4 -- Order blocks
        self.obs: list = []

        # Module 5 -- liquidity
        self.pools: list = []
        self.ph_mem: deque = deque(maxlen=20)   # (bar_index, price)
        self.pl_mem: deque = deque(maxlen=20)
        self.pdh: Optional[float] = None
        self.pdl: Optional[float] = None
        self.pdh_swept = False
        self.pdl_swept = False
        self.sess_h: Optional[float] = None
        self.sess_l: Optional[float] = None
        self._is_first_bar_of_session = True
        # v2.2: independent pending sweeps, replacing the single pend_hi/pend_lo
        # slots that every source used to overwrite.
        self.pend_sweeps: list = []
        self.pend_armed = 0
        self.pend_deduped = 0
        self.pend_expired = 0
        self.opposing_reclaims = 0

        # Module 7.5 -- pullback / fade cooldowns
        self.last_pull_l_bar: Optional[int] = None
        self.last_pull_s_bar: Optional[int] = None
        self.last_fade_l_bar: Optional[int] = None
        self.last_fade_s_bar: Optional[int] = None

    # ------------------------------------------------------------------
    def start_new_session(self, pdh: Optional[float], pdl: Optional[float]) -> None:
        """.pine `newSession` resets ONLY: sessH/sessL, and the
        pdh/pdl/onh/onl-swept-once-per-day flags. Everything else
        (trendDir, FVGs, OBs, pools, HTF bias, bar_index) is continuous.
        `pdh`/`pdl` are the prior session's real RTH high/low, computed by
        the replay driver from the actual prior day's bars (functionally
        identical to the .pine file's request.security(D,...) daily-bar
        read for a chart with one row per prior day)."""
        self.pdh = pdh
        self.pdl = pdl
        self.pdh_swept = False
        self.pdl_swept = False
        self.sess_h = None
        self.sess_l = None
        self._is_first_bar_of_session = True

    # ------------------------------------------------------------------
    def _keep(self, arr: list, max_n: int) -> None:
        while len(arr) > max_n:
            arr.pop(0)

    def _tol_abs(self, atr_base: float) -> float:
        return max(atr_base * self.cfg.eq_tol, self.cfg.mintick * 2)

    # ------------------------------------------------------------------
    def process_bar(self, t, o: float, h: float, l: float, c: float, v: float) -> dict:
        cfg = self.cfg
        self.bar_index += 1
        bar_index = self.bar_index

        # ---- Module 1: core series --------------------------------------
        atr_now = self.atr.update(h, l, c)
        atr_base = self._atr_prev if self._atr_prev is not None else atr_now
        self._atr_prev = atr_now
        atr_base = atr_base if atr_base is not None else 0.0

        ema_f = self.ema_f.update(c)
        sma_f = self.sma_f.update(c)
        ema_m = self.ema_m.update(c)
        sma_m = self.sma_m.update(c)
        ema_s = self.ema_s.update(c)
        sma_s = self.sma_s.update(c)
        ma_f = ema_f if cfg.ma_type == "EMA" else sma_f
        ma_m = ema_m if cfg.ma_type == "EMA" else sma_m
        ma_s = ema_s if cfg.ma_type == "EMA" else sma_s

        avg_vol = self.avg_vol.update(v)
        rvol = (v / avg_vol) if (avg_vol is not None and avg_vol > 0) else 1.0
        vol_ok = rvol >= cfg.rvol_mult
        vol_spike = rvol >= cfg.rvol_mult * 2

        bull_stack = (ma_f is not None and ma_m is not None and ma_s is not None
                      and ma_f > ma_m and ma_m > ma_s)
        bear_stack = (ma_f is not None and ma_m is not None and ma_s is not None
                      and ma_f < ma_m and ma_m < ma_s)

        prev = self.history[-1] if self.history else None
        prev2 = self.history[-2] if len(self.history) >= 2 else None

        kz_on = cfg.kz_start <= t.time() < cfg.kz_end

        # ---- Module 2: structure -----------------------------------------
        ph, pl, piv_bar = self.piv.update(bar_index, h, l)
        if ph is not None:
            self.last_ph, self.last_ph_bar = ph, piv_bar
            self.active_high, self.active_high_bar = ph, piv_bar
        if pl is not None:
            self.last_pl, self.last_pl_bar = pl, piv_bar
            self.active_low, self.active_low_bar = pl, piv_bar

        break_up_src = c if cfg.struc_src == "Close" else h
        break_dn_src = c if cfg.struc_src == "Close" else l
        bar_range = h - l
        disp_ok = bar_range >= cfg.disp_mult * atr_base

        bos_up = bos_dn = mss_up = mss_dn = False
        broken_lvl_up = broken_lvl_dn = None

        # v2.2 AUDIT FIX -- dual-break resolution (ported from v2.2 Pine 373-395).
        #
        # v2.1 ran these as two independent `if`s. An outside bar through BOTH
        # active levels executed both branches, so one close could print BOS-up and
        # MSS-down together, move trend_dir twice, and have the second branch read a
        # trend_dir the first had just written.
        #
        # Narrowness worth recording: with struc_src == "Close" (the live default)
        # this is unreachable -- active_low cannot survive below active_high on a
        # close basis, since any close below active_low consumes it on that same
        # bar. It is live and routine in "Wick" mode. So on the tested config this
        # port is behaviour-neutral; it removes a real trap only if Wick is used.
        breaks_up = self.active_high is not None and break_up_src > self.active_high
        breaks_dn = self.active_low is not None and break_dn_src < self.active_low
        both_ways = breaks_up and breaks_dn
        if both_ways:
            self.dual_break_bars += 1
        # Ambiguity resolved by the side the bar CLOSED toward.
        resolve_up = breaks_up and (not both_ways or c >= o)
        resolve_dn = breaks_dn and (not both_ways or c < o)

        if resolve_up:
            broken_lvl_up = self.active_high
            if self.trend_dir == 1:
                if disp_ok or not cfg.need_disp_bos:
                    bos_up = True
            elif self.trend_dir == -1:
                if disp_ok or not cfg.need_disp_mss:
                    mss_up = True
                    self.trend_dir = 1
            else:
                self.trend_dir = 1
            self.active_high = None
        elif both_ways:
            # The losing side's level is still consumed, matching Pine, so a
            # stale reference cannot linger and re-fire on a later bar.
            self.active_high = None

        if resolve_dn:
            broken_lvl_dn = self.active_low
            if self.trend_dir == -1:
                if disp_ok or not cfg.need_disp_bos:
                    bos_dn = True
            elif self.trend_dir == 1:
                if disp_ok or not cfg.need_disp_mss:
                    mss_dn = True
                    self.trend_dir = -1
            else:
                self.trend_dir = -1
            self.active_low = None
        elif both_ways:
            self.active_low = None

        # ---- Module 3: FVGs -----------------------------------------------
        fvg_tap_bull = fvg_tap_bear = False
        if prev is not None and prev2 is not None:
            bull_gap = l > prev2.h and prev.c > prev.o
            bear_gap = h < prev2.l and prev.c < prev.o
            gap_size = (l - prev2.h) if bull_gap else (prev2.l - h) if bear_gap else 0.0
            gap_ok = gap_size >= atr_base * cfg.fvg_atr_min
            if bull_gap and gap_ok:
                self.fvgs.append(FvgZone(top=l, bot=prev2.h, bull=True, born=bar_index))
                if len(self.fvgs) > cfg.max_fvg_keep:
                    self.fvgs.pop(0)
            if bear_gap and gap_ok:
                self.fvgs.append(FvgZone(top=prev2.l, bot=h, bull=False, born=bar_index))
                if len(self.fvgs) > cfg.max_fvg_keep:
                    self.fvgs.pop(0)

        # v2.2 AUDIT FIX -- SURVIVE FIRST, CREDIT SECOND (ported from v2.2 Pine
        # 505-556). v2.1 credited the tap BEFORE testing invalidation, so the very
        # bar that closed through the far edge and DESTROYED a bullish gap still
        # handed out bullish zone confluence on that same close. A bar that ends a
        # zone is not a tap of it, and the bar that INVERTS a gap is not a tap of
        # its new direction either -- so a close-through bar now credits nothing.
        #
        # Unlike the two dual-break fixes (measured: zero occurrences on real QQQ
        # history), this one changes real scores on real bars, so it moves B1.
        alive_fvgs = []
        for f in self.fvgs:
            if not f.dead and bar_index > f.born:
                acts_bull = (not f.bull) if f.inverted else f.bull
                close_through = (c < f.bot) if acts_bull else (c > f.top)
                if close_through:
                    self.zone_credit_suppressed += 1
                    if not f.inverted and cfg.show_ifvg:
                        f.inverted = True
                        f.spent = False
                        f.state = 0
                    else:
                        f.dead = True
                    # NO tap credit and NO state-machine advance on a funeral bar.
                else:
                    overlap = l <= f.top and h >= f.bot
                    if overlap and not f.spent:
                        if acts_bull:
                            fvg_tap_bull = True
                        else:
                            fvg_tap_bear = True
                    mid = (f.top + f.bot) / 2
                    fill_full = (l <= f.bot) if acts_bull else (h >= f.top)
                    fill_half = (l <= mid) if acts_bull else (h >= mid)
                    fill_touch = (l <= f.top) if acts_bull else (h >= f.bot)
                    new_state = 3 if fill_full else 2 if fill_half else 1 if fill_touch else 0
                    f.state = max(f.state, new_state)
                    rule_hit = (f.state >= 1 if cfg.fvg_mitig == "Touch"
                                else f.state >= 2 if cfg.fvg_mitig == "50% Fill"
                                else f.state >= 3)
                    if rule_hit and not f.spent:
                        f.spent = True
            if bar_index - f.born <= cfg.fvg_max_age:
                alive_fvgs.append(f)
        self.fvgs = alive_fvgs

        # ---- Module 4: order blocks ----------------------------------------
        hist_list = list(self.history)  # hist_list[-1] = bar[1], hist_list[-2] = bar[2], ...
        if (bos_up or mss_up) and disp_ok and len(hist_list) >= 1:
            found_up = None
            for i in range(1, cfg.ob_lookback + 1):
                if i > len(hist_list):
                    break
                cand = hist_list[-i]
                if cand.c < cand.o:
                    found_up = cand
                    break
            if found_up is not None:
                ob_t = max(found_up.o, found_up.c) if cfg.ob_body_only else found_up.h
                ob_b = min(found_up.o, found_up.c) if cfg.ob_body_only else found_up.l
                self.obs.append(OrderBlock(top=ob_t, bot=ob_b, bull=True, born=bar_index))
                if len(self.obs) > cfg.max_ob_keep:
                    self.obs.pop(0)
        if (bos_dn or mss_dn) and disp_ok and len(hist_list) >= 1:
            found_dn = None
            for i in range(1, cfg.ob_lookback + 1):
                if i > len(hist_list):
                    break
                cand = hist_list[-i]
                if cand.c > cand.o:
                    found_dn = cand
                    break
            if found_dn is not None:
                ob_t = max(found_dn.o, found_dn.c) if cfg.ob_body_only else found_dn.h
                ob_b = min(found_dn.o, found_dn.c) if cfg.ob_body_only else found_dn.l
                self.obs.append(OrderBlock(top=ob_t, bot=ob_b, bull=False, born=bar_index))
                if len(self.obs) > cfg.max_ob_keep:
                    self.obs.pop(0)

        # v2.2 AUDIT FIX -- same survive-first rule as the FVG loop (v2.2 Pine
        # 620-640). A close through the far side kills the block, and a killed block
        # does not also collect a tap on its own funeral bar.
        ob_tap_bull = ob_tap_bear = False
        alive_obs = []
        for ob in self.obs:
            if not ob.dead and bar_index > ob.born:
                broken_o = (c < ob.bot) if ob.bull else (c > ob.top)
                if broken_o:
                    ob.dead = True
                    self.zone_credit_suppressed += 1
                else:
                    overlap_o = l <= ob.top and h >= ob.bot
                    if overlap_o:
                        if ob.bull:
                            ob_tap_bull = True
                        else:
                            ob_tap_bear = True
                        ob.tapped = True
            if bar_index - ob.born <= cfg.ob_max_age:
                alive_obs.append(ob)
        self.obs = alive_obs

        # ---- Module 5: liquidity -------------------------------------------
        tol_abs = self._tol_abs(atr_base)
        if ph is not None:
            joined_h = False
            for p in self.pools:
                if not p.dead and p.is_high and abs(p.level - ph) <= tol_abs:
                    p.level = max(p.level, ph)
                    p.count += 1
                    joined_h = True
                    break
            if not joined_h:
                for mem_bar, mem_val in reversed(self.ph_mem):
                    if abs(mem_val - ph) <= tol_abs:
                        lvl = max(mem_val, ph)
                        self.pools.append(LiquidityPool(level=lvl, is_high=True, count=2, born=bar_index))
                        if len(self.pools) > cfg.max_pools:
                            self.pools.pop(0)
                        break
            self.ph_mem.append((piv_bar, ph))
        if pl is not None:
            joined_l = False
            for p in self.pools:
                if not p.dead and not p.is_high and abs(p.level - pl) <= tol_abs:
                    p.level = min(p.level, pl)
                    p.count += 1
                    joined_l = True
                    break
            if not joined_l:
                for mem_bar, mem_val in reversed(self.pl_mem):
                    if abs(mem_val - pl) <= tol_abs:
                        lvl = min(mem_val, pl)
                        self.pools.append(LiquidityPool(level=lvl, is_high=False, count=2, born=bar_index))
                        if len(self.pools) > cfg.max_pools:
                            self.pools.pop(0)
                        break
            self.pl_mem.append((piv_bar, pl))

        sweep_hi_evt = sweep_lo_evt = False

        # v2.2 AUDIT FIX -- INDEPENDENT PENDING SWEEPS (v2.2 Pine 792-870, 985-1030).
        #
        # v2.1 had exactly ONE pend_hi and ONE pend_lo slot, written by five
        # sources (pools, PDH/PDL, ONH/ONL, session H/L, GEX). A PDH sweep one bar
        # after a pool sweep silently erased the pool setup before it ever got its
        # reclaim window, and two sweeps on the same bar meant only the last
        # survived. Each armed sweep now lives, confirms and expires independently.
        # This is the fix that INCREASES signal count -- setups that used to vanish
        # now survive to their reclaim.
        #
        # Ordering matters and is preserved from Pine: prune expired FIRST (v2.2e),
        # then arm this bar's sweeps, then resolve. Pruning before arming is what
        # makes the live window exactly reclaim_win+1 bars rather than +2.
        before_prune = len(self.pend_sweeps)
        self.pend_sweeps = [ps for ps in self.pend_sweeps
                            if bar_index - ps.born <= cfg.reclaim_win]
        self.pend_expired += before_prune - len(self.pend_sweeps)

        def _arm(lvl: float, is_high: bool, src: str) -> None:
            """Dedupe against records armed on THIS bar only -- a pool sitting on
            top of PDH would otherwise arm the same level twice and double-fire the
            reclaim. Records are append-only and removals preserve order, so every
            current-bar record sits at the tail; scanning backwards and stopping at
            the first older record bounds the scan to this bar's burst."""
            for q in reversed(self.pend_sweeps):
                if q.born < bar_index:
                    break
                if q.is_high == is_high and abs(q.level - lvl) <= cfg.mintick:
                    self.pend_deduped += 1
                    return
            self.pend_sweeps.append(PendSweep(
                level=lvl, ext=(l if is_high else h), is_high=is_high,
                src=src, born=bar_index))
            self.pend_armed += 1

        for p in reversed(self.pools):
            if p.dead:
                continue
            if p.is_high:
                if h > p.level and c < p.level:
                    p.swept, p.dead = True, True
                    _arm(p.level, True, SRC_POOL)
                    sweep_hi_evt = True
                elif c > p.level:
                    p.dead = True
            else:
                if l < p.level and c > p.level:
                    p.swept, p.dead = True, True
                    _arm(p.level, False, SRC_POOL)
                    sweep_lo_evt = True
                elif c < p.level:
                    p.dead = True

        if self._is_first_bar_of_session:
            self.pdh_swept = False
            self.pdl_swept = False

        def _sweep_hi(lvl):
            return lvl is not None and h > lvl and c < lvl

        def _sweep_lo(lvl):
            return lvl is not None and l < lvl and c > lvl

        if cfg.show_pdhl and not self.pdh_swept and _sweep_hi(self.pdh):
            self.pdh_swept = True
            sweep_hi_evt = True
            _arm(self.pdh, True, SRC_PDH)
        if cfg.show_pdhl and not self.pdl_swept and _sweep_lo(self.pdl):
            self.pdl_swept = True
            sweep_lo_evt = True
            _arm(self.pdl, False, SRC_PDL)
        # ONH/ONL: structurally inert (RTH-only replay, see module docstring)

        sess_h_prev = self.sess_h
        sess_l_prev = self.sess_l
        if self._is_first_bar_of_session:
            self.sess_h, self.sess_l = h, l
        else:
            self.sess_h = h if self.sess_h is None else max(self.sess_h, h)
            self.sess_l = l if self.sess_l is None else min(self.sess_l, l)

        if cfg.show_sesshl and not self._is_first_bar_of_session:
            if _sweep_hi(sess_h_prev):
                sweep_hi_evt = True
                _arm(sess_h_prev, True, SRC_SESSION)
            if _sweep_lo(sess_l_prev):
                sweep_lo_evt = True
                _arm(sess_l_prev, False, SRC_SESSION)

        # GEX sweeps: gex1/gex2/gex3 forced to 0 for the entire historical
        # replay (no historical log of heff's manual daily GEX entries) --
        # matches the .pine file's own "0 = hidden" convention, so this
        # whole code path is a documented, permanent no-op here.

        # v2.2 resolution loop (Pine 985-1030). Reverse order so removals are safe
        # and the most-recently-armed source is named first in the joined tag.
        sweep_reclaim_short = sweep_reclaim_long = False
        reclaim_src_l = reclaim_src_s = ""
        survivors = []
        for ps in reversed(self.pend_sweeps):
            if bar_index <= ps.born:
                survivors.append(ps)      # minimum lag: sweep bar + 1 confirming close
                continue
            # Confirmation level depends on the opt-in mode. Default
            # ("Close back inside (v2.1)") uses the level itself; the stricter
            # modes require travel past the sweep bar's far extreme.
            confirm_lvl = ps.level if cfg.sweep_confirm == SWEEP_CONFIRM_V21 else ps.ext
            confirmed = (c < confirm_lvl) if ps.is_high else (c > confirm_lvl)
            disp_ok_rec = (cfg.sweep_confirm != SWEEP_CONFIRM_EXT_DISP
                           or bar_range >= cfg.sweep_disp_mult * atr_base)
            # Closing back BEYOND the level means the sweep failed, not reclaimed.
            cancelled = (c > ps.level) if ps.is_high else (c < ps.level)
            expired = bar_index - ps.born > cfg.reclaim_win

            if confirmed and disp_ok_rec:
                # v2.2c: several sources can confirm the SAME direction on one bar
                # (PDH + a pool + GEX all reclaiming short is one event with three
                # causes). Join every contributing source, deduped, rather than
                # naming only the first one reached.
                if ps.is_high:
                    if ps.src not in reclaim_src_s:
                        reclaim_src_s = ps.src if not reclaim_src_s else reclaim_src_s + "+" + ps.src
                    sweep_reclaim_short = True
                else:
                    if ps.src not in reclaim_src_l:
                        reclaim_src_l = ps.src if not reclaim_src_l else reclaim_src_l + "+" + ps.src
                    sweep_reclaim_long = True
                continue                  # consumed
            if cancelled or expired:
                continue                  # dropped
            survivors.append(ps)
        # Restore ascending-born order: the dedupe scan and MAX_PEND reasoning both
        # depend on the array staying sorted by born with current-bar records last.
        self.pend_sweeps = list(reversed(survivors))

        # v2.2b: opposing reclaims could confirm on the SAME bar -- a pending
        # high-sweep at 500 and a pending low-sweep at 490 both confirm on a close
        # of 495, firing trig_long and trig_short together. v2.1 had this hole too
        # with its single slots; independent tracking widens it. Resolved exactly
        # like the Module 2 dual break: the side the bar CLOSED toward wins.
        if sweep_reclaim_long and sweep_reclaim_short:
            self.opposing_reclaims += 1
            if c >= o:
                sweep_reclaim_short = False
                reclaim_src_s = ""
            else:
                sweep_reclaim_long = False
                reclaim_src_l = ""

        self._is_first_bar_of_session = False

        # ---- Module 6: premium / discount -----------------------------------
        deal_hi, deal_lo = self.last_ph, self.last_pl
        pd_valid = deal_hi is not None and deal_lo is not None and deal_hi > deal_lo
        eq_lvl = (deal_hi + deal_lo) / 2 if pd_valid else None
        in_discount = pd_valid and c < eq_lvl
        in_premium = pd_valid and c > eq_lvl

        # ---- Module 7: HTF bias (precomputed, injected) -----------------------
        htf_dir1, htf_dir2 = self._htf_dir_lookup(bar_index)
        htf_bias = (htf_dir1 or 0) + (htf_dir2 or 0)
        htf_long_frac = 1.0 if htf_bias >= 2 else 0.5 if htf_bias == 1 else 0.0
        htf_short_frac = 1.0 if htf_bias <= -2 else 0.5 if htf_bias == -1 else 0.0

        # ---- Module 7.5: pullback / 200MA fade ---------------------------------
        pull_long = pull_short = fade_long = fade_short = False
        if self.trend_dir == 1 and ma_f is not None and l <= ma_f and c > ma_f and c > o:
            if self.last_pull_l_bar is None or (bar_index - self.last_pull_l_bar) > cfg.trig_cooldown:
                pull_long = True
                self.last_pull_l_bar = bar_index
        if self.trend_dir == -1 and ma_f is not None and h >= ma_f and c < ma_f and c < o:
            if self.last_pull_s_bar is None or (bar_index - self.last_pull_s_bar) > cfg.trig_cooldown:
                pull_short = True
                self.last_pull_s_bar = bar_index

        prev_close = prev.c if prev is not None else None
        if (ma_s is not None and prev_close is not None and prev_close > ma_s
                and l <= ma_s + cfg.fade_buf * atr_base and c > ma_s and c > o):
            if self.last_fade_l_bar is None or (bar_index - self.last_fade_l_bar) > cfg.trig_cooldown:
                fade_long = True
                self.last_fade_l_bar = bar_index
        if (ma_s is not None and prev_close is not None and prev_close < ma_s
                and h >= ma_s - cfg.fade_buf * atr_base and c < ma_s and c < o):
            if self.last_fade_s_bar is None or (bar_index - self.last_fade_s_bar) > cfg.trig_cooldown:
                fade_short = True
                self.last_fade_s_bar = bar_index

        # ---- Module 8: weighted confluence engine ------------------------------
        c_struct_l = cfg.w_mss if mss_up else cfg.w_bos if bos_up else 0.0
        c_struct_s = cfg.w_mss if mss_dn else cfg.w_bos if bos_dn else 0.0
        zone_long = fvg_tap_bull or ob_tap_bull
        zone_short = fvg_tap_bear or ob_tap_bear
        c_zone_l = cfg.w_zone if zone_long else 0.0
        c_zone_s = cfg.w_zone if zone_short else 0.0
        c_sweep_l = cfg.w_sweep if sweep_reclaim_long else 0.0
        c_sweep_s = cfg.w_sweep if sweep_reclaim_short else 0.0
        c_pull_l = cfg.w_pull if pull_long else 0.0
        c_pull_s = cfg.w_pull if pull_short else 0.0
        c_fade_l = cfg.w_fade if fade_long else 0.0
        c_fade_s = cfg.w_fade if fade_short else 0.0
        c_pd_l = cfg.w_pd if in_discount else 0.0
        c_pd_s = cfg.w_pd if in_premium else 0.0
        c_ma_l = cfg.w_ma if bull_stack else (cfg.w_ma * 0.5 if (ma_f is not None and ma_m is not None and ma_f > ma_m) else 0.0)
        c_ma_s = cfg.w_ma if bear_stack else (cfg.w_ma * 0.5 if (ma_f is not None and ma_m is not None and ma_f < ma_m) else 0.0)
        c_vol_l = (cfg.w_rvol * 1.5) if vol_spike else (cfg.w_rvol if vol_ok else 0.0)
        c_vol_s = c_vol_l
        c_htf_l = cfg.w_htf * htf_long_frac
        c_htf_s = cfg.w_htf * htf_short_frac
        c_kz_l = cfg.w_kz if kz_on else 0.0
        c_kz_s = c_kz_l

        score_long = c_struct_l + c_zone_l + c_sweep_l + c_pull_l + c_fade_l + c_pd_l + c_ma_l + c_vol_l + c_htf_l + c_kz_l
        score_short = c_struct_s + c_zone_s + c_sweep_s + c_pull_s + c_fade_s + c_pd_s + c_ma_s + c_vol_s + c_htf_s + c_kz_s

        trig_long = mss_up or bos_up or sweep_reclaim_long or pull_long or fade_long
        trig_short = mss_dn or bos_dn or sweep_reclaim_short or pull_short or fade_short

        rev_trig_l = mss_up or sweep_reclaim_long or fade_long
        rev_trig_s = mss_dn or sweep_reclaim_short or fade_short
        if cfg.htf_gate_mode == "Off":
            htf_ok_long = htf_ok_short = True
        elif cfg.htf_gate_mode == "All triggers":
            htf_ok_long = htf_bias >= 0
            htf_ok_short = htf_bias <= 0
        else:  # "Continuation only"
            htf_ok_long = rev_trig_l or htf_bias >= 0
            htf_ok_short = rev_trig_s or htf_bias <= 0

        long_signal = (trig_long and score_long >= cfg.min_score and htf_ok_long
                       and (not cfg.kz_require or kz_on) and (not cfg.gate_ma or bull_stack))
        short_signal = (trig_short and score_short >= cfg.min_score and htf_ok_short
                         and (not cfg.kz_require or kz_on) and (not cfg.gate_ma or bear_stack))

        trig_txt_l = TRIG_MSS if mss_up else TRIG_BOS if bos_up else (
            TRIG_SWEEP_RECLAIM if sweep_reclaim_long else TRIG_PULLBACK if pull_long else TRIG_MA_FADE)
        trig_txt_s = TRIG_MSS if mss_dn else TRIG_BOS if bos_dn else (
            TRIG_SWEEP_RECLAIM if sweep_reclaim_short else TRIG_PULLBACK if pull_short else TRIG_MA_FADE)

        # ---- advance history ----------------------------------------------
        self.history.append(Bar(t=t, o=o, h=h, l=l, c=c, v=v))
        self._prev_close = c

        result = {
            "bar_index": bar_index, "t": t, "o": o, "h": h, "l": l, "c": c, "v": v,
            "trend_dir": self.trend_dir, "atr": atr_now, "atr_base": atr_base,
            "ma_fast": ma_f, "ma_mid": ma_m, "ma_slow": ma_s, "bull_stack": bull_stack, "bear_stack": bear_stack,
            "rvol": rvol, "vol_ok": vol_ok, "vol_spike": vol_spike,
            "bos_up": bos_up, "bos_dn": bos_dn, "mss_up": mss_up, "mss_dn": mss_dn,
            "sweep_reclaim_long": sweep_reclaim_long, "sweep_reclaim_short": sweep_reclaim_short,
            # v2.2: which liquidity was reclaimed, joined and deduped when several
            # sources confirm the same direction on one bar. Feeds the alert
            # payload's `trigger_src`, and is "" unless a reclaim actually won.
            "reclaim_src_long": reclaim_src_l, "reclaim_src_short": reclaim_src_s,
            "pull_long": pull_long, "pull_short": pull_short, "fade_long": fade_long, "fade_short": fade_short,
            "zone_long": zone_long, "zone_short": zone_short,
            "in_discount": in_discount, "in_premium": in_premium,
            "htf_dir1": htf_dir1, "htf_dir2": htf_dir2, "htf_bias": htf_bias,
            "kz_on": kz_on,
            "score_long": score_long, "score_short": score_short,
            "factors_long": {
                "structure": c_struct_l, "zone": c_zone_l, "sweep_reclaim": c_sweep_l, "pullback": c_pull_l,
                "ma_fade": c_fade_l, "premium_discount": c_pd_l, "ma_stack": c_ma_l, "rvol": c_vol_l,
                "htf": c_htf_l, "killzone": c_kz_l,
            },
            "factors_short": {
                "structure": c_struct_s, "zone": c_zone_s, "sweep_reclaim": c_sweep_s, "pullback": c_pull_s,
                "ma_fade": c_fade_s, "premium_discount": c_pd_s, "ma_stack": c_ma_s, "rvol": c_vol_s,
                "htf": c_htf_s, "killzone": c_kz_s,
            },
            "trig_long": trig_long, "trig_short": trig_short,
            "htf_ok_long": htf_ok_long, "htf_ok_short": htf_ok_short,
            "long_signal": long_signal, "short_signal": short_signal,
            "trig_txt_long": trig_txt_l if trig_long else None,
            "trig_txt_short": trig_txt_s if trig_short else None,
        }
        return result
