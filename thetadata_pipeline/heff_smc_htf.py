"""HEFF SMC v2 Module 7 port -- HTF bias (5m/15m structure direction).

Mirrors the .pine file's
    htfDir1 = request.security(tickerid, htfTf1, f_structDir()[1], gaps_off, lookahead_on)
(and same for htfTf2). f_structDir() is a SEPARATE, simpler structure state
machine than Module 2's own trendDir engine -- no BOS/MSS distinction, no
displacement gate, just "closed above the last confirmed pivot high -> +1,
closed below the last confirmed pivot low -> -1."

The [1]+lookahead_on idiom means: for any given 1-min chart bar, read the
HTF structure-direction value AS OF THE LAST FULLY COMPLETED higher-
timeframe bar -- never the one still forming under the current 1-min bar.
This module implements that with a plain resample (label=bin CLOSE time)
+ backward asof-merge: a still-forming HTF bin's close timestamp is always
in the future relative to any 1-min bar inside it, so a backward merge on
"HTF close <= this 1-min bar's time" naturally lands on the last COMPLETED
bar with no separate shift(1) required (shifting on top of this would
double-lag it -- verified by construction, not by comparison against a live
Pine run, since no live/replay ground truth exists for this indicator; see
the B1 report's confidence notes).
"""

from __future__ import annotations

import logging
from typing import Optional

import pandas as pd

from .heff_smc_engine import PivotTracker

logger = logging.getLogger("thetadata_pkg.heff_smc_htf")


class HtfStructDirTracker:
    """Mirrors f_structDir(): pivot-based structure direction only, run on
    a HIGHER-timeframe bar sequence (own bar-index space, not the 1-min
    chart's). `var`-scoped forever, so instantiate once and feed every HTF
    bar across the whole continuous multi-session replay in order."""

    def __init__(self, piv_len: int):
        self.piv = PivotTracker(piv_len, piv_len)
        self.active_high = None
        self.active_low = None
        self.dir = 0
        self._bar_index = -1
        # Diagnostics for the v2.2 dual-break fix: how often a single HTF bar broke
        # BOTH levels (the case v2.1 silently resolved bearish), and how many of
        # those could not be resolved because no open was supplied.
        self.ambiguous_bars = 0
        self.unresolved_ambiguous = 0

    def update(self, high: float, low: float, close: float,
               open_: Optional[float] = None) -> int:
        """v2.2 AUDIT FIX -- dual-break resolution.

        v2.1 ran the up-break and down-break as two independent `if`s. An outside
        HTF bar through BOTH active levels executed both branches, and the bearish
        one ran second, so it always won: **every two-sided HTF bar silently
        reported dir = -1**. Because htfBias = htfDir1 + htfDir2 feeds the HTF
        confluence factor and the HTF gate, that biased the whole model bearish on
        exactly the widest-range (most informative) HTF bars.

        v2.2 evaluates both tests against PRE-UPDATE state and resolves ties by
        the bar's own close direction (`close >= open ? 1 : -1`), consuming both
        levels. Ported operand-for-operand from v2.2 Pine lines 1093-1108.

        `open_` is newly required to resolve the tie. It is optional only so an
        existing caller that never hits a two-sided bar keeps working; when it is
        None a tie falls back to the v2.1 outcome (-1) and is COUNTED, so a silent
        regression is impossible -- see `ambiguous_bars`."""
        self._bar_index += 1
        ph, pl, _ = self.piv.update(self._bar_index, high, low)
        if ph is not None:
            self.active_high = ph
        if pl is not None:
            self.active_low = pl

        breaks_up = self.active_high is not None and close > self.active_high
        breaks_dn = self.active_low is not None and close < self.active_low

        if breaks_up and not breaks_dn:
            self.dir = 1
            self.active_high = None
        elif breaks_dn and not breaks_up:
            self.dir = -1
            self.active_low = None
        elif breaks_up and breaks_dn:
            self.ambiguous_bars += 1
            if open_ is None:
                # Cannot resolve without the open. Keep v2.1's outcome rather than
                # invent one, but the counter makes this visible instead of silent.
                self.unresolved_ambiguous += 1
                self.dir = -1
            else:
                self.dir = 1 if close >= open_ else -1
            self.active_high = None
            self.active_low = None
        return self.dir


def resample_htf_bars(one_min: pd.DataFrame, minutes: int) -> pd.DataFrame:
    """OHLC resample of ET-localized 1-min bars (t,o,h,l,c,v columns) into
    `minutes`-length bins, labeled/closed so the resulting row index IS
    each bin's real CLOSE timestamp (bin [T, T+minutes) -> labeled T+minutes).
    Empty bins (the overnight/weekend gaps between sessions, since these are
    RTH-only 1-min bars) are dropped, never forward-filled -- an HTF bar
    with zero real 1-min bars inside it does not exist on the real chart
    either. 09:30 ET aligns exactly to both the 5-min and 15-min clock grid
    (570 minutes after midnight, divisible by both), so pandas's default
    midnight-origin resample needs no explicit origin override to avoid
    drift session-to-session."""
    df = one_min.set_index("t").sort_index()
    agg = df.resample(f"{minutes}min", label="right", closed="left").agg(
        {"o": "first", "h": "max", "l": "min", "c": "last", "v": "sum"}
    )
    agg = agg.dropna(subset=["o", "h", "l", "c"])
    return agg.reset_index()


def compute_htf_dir_series(one_min: pd.DataFrame, minutes: int, piv_len: int) -> pd.DataFrame:
    """Runs HtfStructDirTracker bar-by-bar across every resampled HTF bar,
    continuous across all sessions (no reset at day boundaries -- matches
    the live indicator's own forever-`var` HTF state). Returns [t, dir]
    where t = the HTF bar's own close timestamp."""
    htf_bars = resample_htf_bars(one_min, minutes)
    tracker = HtfStructDirTracker(piv_len)
    # `o` is now passed so the v2.2 dual-break tie can be resolved by close
    # direction. Without it, every two-sided HTF bar would keep reporting -1.
    dirs = [tracker.update(row.h, row.l, row.c, row.o)
            for row in htf_bars.itertuples(index=False)]
    htf_bars = htf_bars.copy()
    htf_bars["dir"] = dirs
    if tracker.ambiguous_bars:
        logger.info(
            "%dmin HTF: %d two-sided bar(s) resolved by close direction "
            "(v2.1 would have reported -1 on every one of them)",
            minutes, tracker.ambiguous_bars)
    return htf_bars[["t", "dir"]]


def align_htf_dir_to_1min(one_min: pd.DataFrame, htf_dir: pd.DataFrame) -> pd.Series:
    """Backward asof-merge on HTF-bar-close-time <= 1-min-bar-time (see
    module docstring for why this alone implements the [1]+lookahead_on
    "last completed HTF bar" idiom with no extra shift). Bars before the
    very first HTF bar has even completed (start of the whole 62-session
    history only) get 0, matching Pine's own nz(htfDir, 0)."""
    left = one_min[["t"]].sort_values("t").reset_index(drop=True)
    right = htf_dir.sort_values("t").reset_index(drop=True)
    merged = pd.merge_asof(left, right, on="t", direction="backward")
    return merged["dir"].fillna(0).astype(int)
