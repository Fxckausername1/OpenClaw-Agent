#!/usr/bin/env python3
"""
momentum_breakdown.py -- "Momentum Breakdown" volatility-expansion entry system.

Catches high-velocity directional moves AT INCEPTION (volatility-compression breakout) -- the
inverse of a mean-reversion fade, which by construction only fires AFTER a move has already
extended (z-score stretched), i.e. on the exhaustion/pullback, not the impulse.

Pipeline (indicators fully vectorized with pandas/numpy; KAMA and the FSM step bar-by-bar because
both are PATH-DEPENDENT recursions that cannot be vectorized without breaking causality):

  Module 1  Volatility compression + regime filter   BB/KC squeeze, KER/KAMA, RSI-9 (80/20)
  Module 2  Velocity math + event anchoring          rolling z-score + velocity, Anchored VWAP
  Module 3  Microstructure / liquidity validation    CVD_Engine  (free-data workaround, see below)
  Module 4  Portfolio Finite State Machine            Flat / Pending / Active / Cooldown
  Module 5  Volatility-adjusted risk exits            STATIC 3.5x-ATR Chandelier (asymmetric, no time stop)

MODULE 3 -- FREE-DATA CVD WORKAROUND (no paid L2):
  True CVD needs aggressor-tagged trades. We bootstrap two modes in `CVD_Engine`:
   (A) Historical/backfill -> intrabar PRESSURE decomposition from OHLCV bars (vectorized).
   (B) Live -> Lee-Ready TICK TEST on Alpaca's free v2/iex trade websocket (async).
  Both emit the same downstream signal (a running CVD whose slope confirms/denies a breakout).
  (A) is still a bar-level estimate, not true flow -- weight Module 3 accordingly until (B) feeds it.

No-lookahead: indicator value at bar t uses only data <= t; the FSM consumes them causally and
models fills at the NEXT bar's open.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd


# ============================================================================ CONFIG
@dataclass
class Config:
    # --- Module 1: squeeze / regime ---
    bb_period: int = 20
    bb_mult: float = 2.0
    kc_period: int = 20
    kc_mult: float = 1.5
    atr_period: int = 20
    ker_period: int = 10
    ker_min: float = 0.30          # KER below this == chop -> abort all entry logic
    kama_fast: int = 2
    kama_slow: int = 30
    rsi_period: int = 9
    rsi_upper: float = 80.0        # widened from 70/30 for the noisier 9-period RSI
    rsi_lower: float = 20.0
    # --- Module 2: velocity / anchoring ---
    z_period: int = 20
    z_vel_min: float = 0.50        # min |d(z)/dbar| for a genuine high-velocity displacement
    avwap_band_mult: float = 3.5   # ASYMMETRIC: target pushed out 2.0 -> 3.5 SD to capture the fat tail
    avwap_min_bars: int = 5        # AVWAP target time-lock: bars since anchor before it can arm
    avwap_min_band_atr: float = 0.5  # AVWAP target vol-lock: |band - vwap| must exceed this x entry_atr
    disable_avwap_target: bool = False  # ASYMMETRIC: if True, NO hard target -> pure trailing-stop exit
    # --- Module 3: microstructure ---
    cvd_lookback: int = 10         # window for the CVD divergence / confirmation test
    # --- Module 4: cooldown ---
    cooldown_bars: int = 6         # hard re-entry lockout after any liquidation
    # --- Module 5: exits ---
    # ASYMMETRIC-EXIT REWORK: the original mean-reversion-style exits (tight CE that collapses to 1.5x
    # at z-exhaustion + a 5-bar time stop) strangle a momentum payoff -- they liquidate winners before
    # they can clear the 6bp friction. We now (a) lock the Chandelier STATIC at 3.5x ATR so the trade
    # endures normal pullbacks, and (b) remove the time stop entirely, letting the trailing stop and
    # (optionally) a far-out AVWAP band be the only liquidators -> "let winners run".
    static_chandelier: bool = True   # True -> always ce_atr_mult_normal; ignore the z-exhaust collapse
    disable_time_stop: bool = True   # True -> remove Module 5.3 time-based invalidation entirely
    ce_atr_mult_normal: float = 3.5  # raised 3.0 -> 3.5 (static trailing multiplier)
    ce_atr_mult_exhaust: float = 1.5 # legacy; only used if static_chandelier=False
    z_exhaust_lo: float = 1.5      # legacy z-exhaustion window (only if static_chandelier=False)
    z_exhaust_hi: float = 2.0
    time_stop: pd.Timedelta = field(default_factory=lambda: pd.Timedelta(minutes=5))
    min_progress_atr: float = 0.50  # must travel this many entry-ATRs within time_stop (if enabled)
    fill_on_next_open: bool = True
    # --- Strategy 1: Institutional Sweep (block follower) ---
    rvol_period: int = 20
    sweep_rvol_min: float = 3.5     # massive institutional displacement
    sweep_loc_long: float = 0.90    # close in top 10% of range -> no overhead limit resistance
    sweep_loc_short: float = 0.10   # close in bottom 10% of range
    # --- Strategy 2: Macro MA Re-Acceleration (trend continuation) ---
    macro_ker_period: int = 12      # ~1h trend on 5-min bars (12 x 5min = 60min)
    macro_ker_min: float = 0.60     # trend must be highly efficient / uninterrupted


# ============================================================ MODULE 1 + 5 PRIMITIVES (vectorized)
def true_range(df: pd.DataFrame) -> pd.Series:
    pc = df["Close"].shift(1)
    return pd.concat([df["High"] - df["Low"],
                      (df["High"] - pc).abs(),
                      (df["Low"] - pc).abs()], axis=1).max(axis=1)


def atr(df: pd.DataFrame, period: int) -> pd.Series:
    """Wilder ATR via EMA(alpha=1/period) of the true range."""
    return true_range(df).ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()


def rsi(close: pd.Series, period: int) -> pd.Series:
    """Wilder RSI, vectorized via EMA of gains/losses."""
    d = close.diff()
    gain = d.clip(lower=0).ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    loss = (-d.clip(upper=0)).ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    rs = gain / loss.replace(0, np.nan)
    return (100.0 - 100.0 / (1.0 + rs)).fillna(50.0)


def bb_kc_squeeze(df: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    """MODULE 1.1 -- Bollinger(20,2) compressing INSIDE Keltner(20, 1.5*ATR). `squeeze_fired` is the
    release bar (was compressed, now not) -- the actionable volatility-expansion trigger."""
    mid = df["Close"].rolling(cfg.bb_period).mean()
    sd = df["Close"].rolling(cfg.bb_period).std(ddof=0)
    bb_u, bb_l = mid + cfg.bb_mult * sd, mid - cfg.bb_mult * sd
    kc_mid = df["Close"].rolling(cfg.kc_period).mean()
    a = atr(df, cfg.atr_period)
    kc_u, kc_l = kc_mid + cfg.kc_mult * a, kc_mid - cfg.kc_mult * a
    squeeze_on = (bb_u < kc_u) & (bb_l > kc_l)
    squeeze_fired = squeeze_on.shift(1, fill_value=False) & (~squeeze_on)
    return pd.DataFrame({"bb_u": bb_u, "bb_l": bb_l, "kc_u": kc_u, "kc_l": kc_l,
                         "atr": a, "squeeze_on": squeeze_on, "squeeze_fired": squeeze_fired})


def kaufman_er(close: pd.Series, period: int) -> pd.Series:
    """MODULE 1.2 -- Kaufman Efficiency Ratio = |net change| / sum(|bar changes|). 1=trending, 0=chop."""
    change = (close - close.shift(period)).abs()
    volatility = close.diff().abs().rolling(period).sum()
    return (change / volatility.replace(0, np.nan)).clip(0, 1).fillna(0.0)


def kama(close: pd.Series, ker: pd.Series, fast: int, slow: int) -> pd.Series:
    """MODULE 1.2 -- Kaufman Adaptive MA. RECURSIVE (each value depends on the previous KAMA via a
    per-bar smoothing constant) -> O(n) loop is the correct implementation, not a vectorization miss."""
    fast_sc, slow_sc = 2.0 / (fast + 1), 2.0 / (slow + 1)
    sc = ((ker * (fast_sc - slow_sc) + slow_sc) ** 2).to_numpy(dtype=float)   # vectorized per-bar alpha
    c = close.to_numpy(dtype=float)
    out = np.full(len(c), np.nan)
    seed = next((j for j in range(len(c)) if not np.isnan(c[j])), None)
    if seed is None:
        return pd.Series(out, index=close.index)
    out[seed] = c[seed]
    for i in range(seed + 1, len(c)):
        prev = out[i - 1]
        out[i] = prev + (sc[i] if not np.isnan(sc[i]) else 0.0) * (c[i] - prev)
    return pd.Series(out, index=close.index)


# ================================================================ MODULE 2 (velocity + anchoring)
def zscore_velocity(close: pd.Series, period: int) -> tuple[pd.Series, pd.Series]:
    """MODULE 2.1 -- rolling z-score of price and its first derivative (per-bar velocity)."""
    mean = close.rolling(period).mean()
    sd = close.rolling(period).std(ddof=0)
    z = (close - mean) / sd.replace(0, np.nan)
    return z.fillna(0.0), z.diff().fillna(0.0)


class AnchoredVWAP:
    """MODULE 2.2 -- VWAP + volume-weighted std-dev bands anchored at a specific bar index (the
    squeeze-fire bar). Computed from the anchor forward only, so it never sees pre-anchor data."""

    def __init__(self, df: pd.DataFrame, anchor_pos: int, band_mult: float = 2.0):
        seg = df.iloc[anchor_pos:]
        tp = (seg["High"] + seg["Low"] + seg["Close"]) / 3.0
        vol = seg["Volume"].clip(lower=0)
        cum_v = vol.cumsum().replace(0, np.nan)
        self.anchor_pos = anchor_pos
        self.vwap = (tp * vol).cumsum() / cum_v
        var = ((tp - self.vwap) ** 2 * vol).cumsum() / cum_v
        sd = np.sqrt(var.clip(lower=0))
        self.upper = self.vwap + band_mult * sd
        self.lower = self.vwap - band_mult * sd

    def at(self, pos: int) -> dict:
        if pos < self.anchor_pos:
            return {"vwap": np.nan, "upper": np.nan, "lower": np.nan}
        k = pos - self.anchor_pos
        return {"vwap": float(self.vwap.iloc[k]),
                "upper": float(self.upper.iloc[k]),
                "lower": float(self.lower.iloc[k])}


# ============================================================================ MODULE 3 (CVD_Engine)
class CVD_Engine:
    """Cumulative Volume Delta with a zero-cost workaround for the absence of paid L2 aggressor data.

    (A) Historical / backfill  -> `from_bars()`: intrabar PRESSURE decomposition (vectorized).
    (B) Live                    -> `on_trade()` / `stream()`: Lee-Ready TICK TEST on the IEX feed.

    Both yield a running CVD; `cvd_slope()` turns it into the confirm/deny signal the FSM consumes.
    NOTE (A) is a bar-level estimate, not true order flow -- weight Module 3 accordingly until the
    live tick test (B) is the source.
    """

    # ---- (A) HISTORICAL: intrabar pressure ----
    @staticmethod
    def from_bars(df: pd.DataFrame) -> pd.Series:
        """Buy = V*(C-L)/(H-L);  Sell = V*(H-C)/(H-L);  BarDelta = Buy - Sell;  CVD = cumsum(BarDelta).
        Vectorized. A doji (C mid-range) nets ~0; close-on-high -> +V (buying), close-on-low -> -V."""
        rng = (df["High"] - df["Low"]).replace(0, np.nan)
        buy = df["Volume"] * (df["Close"] - df["Low"]) / rng
        sell = df["Volume"] * (df["High"] - df["Close"]) / rng
        return (buy - sell).fillna(0.0).cumsum()

    # ---- (B) LIVE: Lee-Ready tick test ----
    def __init__(self):
        self.cvd: float = 0.0
        self._last_price: Optional[float] = None
        self._last_polarity: int = 1            # zero-tick before any trade defaults to buy

    def on_trade(self, price: float, size: float) -> float:
        """Classify one live trade by the tick test and update CVD:
        price > prev -> buying (+size); price < prev -> selling (-size); unchanged -> inherit prior."""
        if self._last_price is None or price == self._last_price:
            polarity = self._last_polarity      # first trade or zero-tick inherits prior polarity
        else:
            polarity = 1 if price > self._last_price else -1
        self.cvd += polarity * size
        self._last_price, self._last_polarity = price, polarity
        return self.cvd

    async def stream(self, symbols, key: str, secret: str, on_update=None):  # pragma: no cover (live)
        """Async ingest of Alpaca's free IEX trade stream, applying the tick test per trade. Requires
        the `websockets` package + Alpaca data creds. Live path -- not exercised by the backtest.
        `on_update(symbol, cvd, trade_msg)` is called after each classified trade."""
        import json
        import websockets
        url = "wss://stream.data.alpaca.markets/v2/iex"
        engines = {s: CVD_Engine() for s in symbols}
        async with websockets.connect(url, ping_interval=20) as ws:
            await ws.recv()                                                   # {"T":"success","msg":"connected"}
            await ws.send(json.dumps({"action": "auth", "key": key, "secret": secret}))
            await ws.recv()                                                   # auth ack
            await ws.send(json.dumps({"action": "subscribe", "trades": list(symbols)}))
            async for raw in ws:
                for msg in json.loads(raw):
                    if msg.get("T") == "t":                                   # trade message
                        eng = engines.get(msg["S"])
                        if eng is None:
                            continue
                        cvd = eng.on_trade(float(msg["p"]), float(msg["s"]))
                        if on_update is not None:
                            on_update(msg["S"], cvd, msg)


def cvd_slope(cvd: pd.Series, lookback: int) -> pd.Series:
    """Signed CVD change over `lookback` bars. >0 = net aggressive buying, <0 = net aggressive
    selling. Confirms a breakout's direction; an opposite slope == absorption/divergence (head-fake)."""
    return cvd.diff(lookback).fillna(0.0)


# ============================================================ CATALYST-STRATEGY INDICATORS (vectorized)
def rvol(volume: pd.Series, period: int) -> pd.Series:
    """STRATEGY 1 -- relative volume = current bar volume / trailing N-bar average. The average is
    shift(1)'d so it uses ONLY prior bars (current spike excluded from its own denominator, strictly
    causal). At the open the trailing window spans into the prior session -- a recent-volume baseline."""
    avg = volume.rolling(period).mean().shift(1)
    return (volume / avg.replace(0, np.nan)).fillna(0.0)


def close_location(df: pd.DataFrame) -> pd.Series:
    """STRATEGY 1 -- Close Location Value in [0, 1]: (Close - Low) / (High - Low). 1.0 = close on the
    high (aggressive buying, no overhead resistance), 0.0 = close on the low. A zero-range bar -> 0.5."""
    rng = (df["High"] - df["Low"]).replace(0, np.nan)
    return ((df["Close"] - df["Low"]) / rng).fillna(0.5)


def macro_trend(close: pd.Series, period: int) -> tuple[pd.Series, pd.Series]:
    """STRATEGY 2 -- macro Kaufman Efficiency Ratio over a longer (~1h) lookback + the signed macro
    displacement. KER in [0,1] (1 = clean trend); macro_dir = sign of the net move over the window."""
    ker = kaufman_er(close, period)
    macro_dir = np.sign(close - close.shift(period)).fillna(0.0)
    return ker, macro_dir


# ============================================================ INDICATOR ASSEMBLY (one vectorized pass)
def build_indicators(df: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    out = df[["Open", "High", "Low", "Close", "Volume"]].copy()
    out = out.join(bb_kc_squeeze(df, cfg))
    out["ker"] = kaufman_er(df["Close"], cfg.ker_period)
    out["kama"] = kama(df["Close"], out["ker"], cfg.kama_fast, cfg.kama_slow)
    out["rsi"] = rsi(df["Close"], cfg.rsi_period)
    out["z"], out["z_vel"] = zscore_velocity(df["Close"], cfg.z_period)
    out["cvd"] = CVD_Engine.from_bars(df)                       # (A) historical intrabar pressure
    out["cvd_slope"] = cvd_slope(out["cvd"], cfg.cvd_lookback)
    # --- catalyst-strategy features (Strategy 1 + Strategy 2) ---
    out["rvol"] = rvol(df["Volume"], cfg.rvol_period)           # S1
    out["cloc"] = close_location(df)                            # S1
    out["macro_ker"], out["macro_dir"] = macro_trend(df["Close"], cfg.macro_ker_period)  # S2
    return out


# ============================================================================ MODULE 4 (FSM)
@dataclass
class Context:
    cfg: Config
    df: pd.DataFrame
    ind: pd.DataFrame
    position: int = 0
    entry_pos: Optional[int] = None
    entry_price: float = float("nan")
    entry_time: Optional[pd.Timestamp] = None
    entry_atr: float = float("nan")
    extreme: float = float("nan")
    stop: float = float("nan")
    avwap: Optional[AnchoredVWAP] = None
    pending_dir: int = 0
    cooldown_until: int = -1
    trades: list = field(default_factory=list)
    flat_state_cls: type = None    # which Flat state to return to after a cooldown (set by the engine)


class State:
    name = "BASE"
    def handle(self, ctx: Context, i: int) -> "State":
        raise NotImplementedError


class FlatPositionState(State):
    """Monitors regime/indicators for a valid macro breakout: KER above the chop floor, squeeze just
    FIRED, high z-velocity, price on the trend side of KAMA, RSI not already exhausted that way."""
    name = "FLAT"

    def handle(self, ctx: Context, i: int) -> State:
        r = ctx.ind.iloc[i]
        if i <= ctx.cooldown_until or not bool(r.squeeze_fired):
            return self
        if r.ker < ctx.cfg.ker_min:                          # MODULE 1.2: chop -> abort
            return self
        if abs(r.z_vel) < ctx.cfg.z_vel_min:                 # MODULE 2.1: demand high velocity
            return self
        direction = 0
        if r.z_vel > 0 and r.Close > r.kama and r.rsi < ctx.cfg.rsi_upper:
            direction = 1
        elif r.z_vel < 0 and r.Close < r.kama and r.rsi > ctx.cfg.rsi_lower:
            direction = -1
        if direction == 0:
            return self
        ctx.pending_dir = direction
        ctx.avwap = AnchoredVWAP(ctx.df, i, ctx.cfg.avwap_band_mult)   # anchor at the fire bar
        return PendingState()


class PendingState(State):
    """Macro hit; awaiting CVD microstructure validation. REJECT the breakout as a head-fake if CVD
    diverges from price (e.g. breakdown but CVD shows buying/absorption). Validated -> fill next open."""
    name = "PENDING"

    def handle(self, ctx: Context, i: int) -> State:
        r = ctx.ind.iloc[i]
        d = ctx.pending_dir
        confirms = (r.cvd_slope > 0) if d == 1 else (r.cvd_slope < 0)   # MODULE 3.2
        still_valid = (r.ker >= ctx.cfg.ker_min) and not bool(r.squeeze_on)
        if not (confirms and still_valid):
            ctx.pending_dir, ctx.avwap = 0, None
            return FlatPositionState()
        fill_pos = i + 1 if (ctx.cfg.fill_on_next_open and i + 1 < len(ctx.df)) else i
        px = float(ctx.df["Open"].iloc[fill_pos]) if fill_pos != i else float(r.Close)
        ctx.position, ctx.entry_pos, ctx.entry_price = d, fill_pos, px
        ctx.entry_time = ctx.df.index[fill_pos]
        ctx.entry_atr = float(r.atr)
        ctx.extreme = px
        ctx.stop = _chandelier(ctx, abs(r.z), float(r.atr))
        return ActivePositionState()


class ActivePositionState(State):
    """ASYMMETRIC-EXIT version: ratchets a STATIC 3.5x-ATR Chandelier stop (one-way; no z-exhaustion
    collapse), optionally takes profit at a far-out (3.5 SD, artifact-locked) AVWAP band, and -- by
    design -- does NOT enforce a time stop. The trailing stop is the primary liquidator so a winner
    can run into the fat tail instead of being scratched early."""
    name = "ACTIVE"

    def handle(self, ctx: Context, i: int) -> State:
        r = ctx.ind.iloc[i]
        hi, lo, close = float(r.High), float(r.Low), float(r.Close)
        d = ctx.position

        ctx.extreme = max(ctx.extreme, hi) if d == 1 else min(ctx.extreme, lo)
        new_stop = _chandelier(ctx, abs(r.z), float(r.atr))
        ctx.stop = max(ctx.stop, new_stop) if d == 1 else min(ctx.stop, new_stop)  # never loosen

        # MODULE 5.1: Chandelier stop hit.
        if (d == 1 and lo <= ctx.stop) or (d == -1 and hi >= ctx.stop):
            return self._exit(ctx, i, ctx.stop, "chandelier")

        # MODULE 2.2 (artifact-FIXED): AVWAP outer band as a target, armed ONLY when statistically
        # meaningful -- the band std-dev is ~0 at the anchor, so without these two locks it tags
        # instantly for fake same-bar wins. Lock 1 = >= avwap_min_bars since the anchor; Lock 2 =
        # |band - vwap| > avwap_min_band_atr * entry_atr (the band has genuinely expanded).
        if (not ctx.cfg.disable_avwap_target and ctx.avwap is not None
                and (i - ctx.avwap.anchor_pos) >= ctx.cfg.avwap_min_bars):
            band = ctx.avwap.at(i)
            tgt = band["upper"] if d == 1 else band["lower"]
            if not np.isnan(tgt) and abs(tgt - band["vwap"]) > ctx.cfg.avwap_min_band_atr * ctx.entry_atr:
                if (d == 1 and hi >= tgt) or (d == -1 and lo <= tgt):
                    return self._exit(ctx, i, tgt, "avwap_target")

        # MODULE 5.3: time-based invalidation -- no baseline progress within the window. ASYMMETRIC
        # REWORK: disabled by default (disable_time_stop) so a winner is never cut for being "slow".
        if not ctx.cfg.disable_time_stop:
            elapsed = ctx.df.index[i] - ctx.entry_time
            progress = (close - ctx.entry_price) * d
            if elapsed >= ctx.cfg.time_stop and progress < ctx.cfg.min_progress_atr * ctx.entry_atr:
                return self._exit(ctx, i, close, "time_stop")
        return self

    def _exit(self, ctx: Context, i: int, price: float, reason: str) -> State:
        pnl_r = ((price - ctx.entry_price) * ctx.position) / max(ctx.entry_atr, 1e-9)
        ctx.trades.append({"entry_time": ctx.entry_time, "entry": ctx.entry_price,
                           "entry_atr": ctx.entry_atr, "exit_time": ctx.df.index[i], "exit": price,
                           "dir": ctx.position, "reason": reason, "r_atr": round(pnl_r, 4)})
        ctx.position, ctx.entry_pos, ctx.avwap, ctx.pending_dir = 0, None, None, 0
        ctx.cooldown_until = i + ctx.cfg.cooldown_bars       # MODULE 4: hard re-entry lockout
        return CooldownState()


class CooldownState(State):
    """Post-liquidation lockout: blocks ALL re-entry logic for cooldown_bars to neutralize whipsaws.
    Returns to the ACTIVE strategy's Flat state (ctx.flat_state_cls), not a hardcoded one -- so a swap
    can't silently bounce back to the squeeze strategy mid-run."""
    name = "COOLDOWN"

    def handle(self, ctx: Context, i: int) -> State:
        return self if i < ctx.cooldown_until else (ctx.flat_state_cls or FlatPositionState)()


def _chandelier(ctx: Context, z_abs: float, atr_val: float) -> float:
    """MODULE 5.1/5.2 -- Chandelier trailing stop. ASYMMETRIC REWORK: when static_chandelier is set
    (default) the multiplier is LOCKED at ce_atr_mult_normal (3.5x) so the stop trails wide enough to
    survive normal pullbacks and let the winner run. The legacy regime-scaled collapse (3.0x -> 1.5x
    at z-exhaustion |z| in [1.5, 2.0]) is mean-reversion behavior that liquidates momentum early -- it
    is retained only for static_chandelier=False ablation."""
    if ctx.cfg.static_chandelier:
        mult = ctx.cfg.ce_atr_mult_normal
    else:
        mult = (ctx.cfg.ce_atr_mult_exhaust
                if ctx.cfg.z_exhaust_lo <= z_abs <= ctx.cfg.z_exhaust_hi
                else ctx.cfg.ce_atr_mult_normal)
    return ctx.extreme - mult * atr_val if ctx.position == 1 else ctx.extreme + mult * atr_val


# ====================================================== CATALYST FSM (shared entry + Flat states)
def _open_position(ctx: Context, i: int, direction: int) -> State:
    """Generic no-lookahead entry for the catalyst strategies: signal at bar i, fill at the NEXT bar's
    open, seed entry state + the initial static-3.5x Chandelier. Deliberately SKIPS the squeeze-only
    PendingState (its CVD/squeeze gates) so each new strategy is judged purely on its own entry edge."""
    r = ctx.ind.iloc[i]
    fill_pos = i + 1 if (ctx.cfg.fill_on_next_open and i + 1 < len(ctx.df)) else i
    px = float(ctx.df["Open"].iloc[fill_pos]) if fill_pos != i else float(r.Close)
    ctx.position, ctx.entry_pos, ctx.entry_price = direction, fill_pos, px
    ctx.entry_time = ctx.df.index[fill_pos]
    ctx.entry_atr = float(r.atr)
    ctx.extreme = px
    ctx.avwap = AnchoredVWAP(ctx.df, i, ctx.cfg.avwap_band_mult)   # harmless when disable_avwap_target
    ctx.stop = _chandelier(ctx, abs(float(r.z)), float(r.atr))
    return ActivePositionState()


class SweepFlatState(State):
    """STRATEGY 1 -- Institutional Sweep / block follower. A single bar of massive relative volume
    (RVOL > 3.5) that closes at the extreme of its range (CLOC > 0.90 long / < 0.10 short = zero
    opposing limit resistance) AND is confirmed by the CVD pressure slope on that bar = aggressive
    institutional displacement to follow. No mean-reversion gating -- raw entry edge only."""
    name = "FLAT_SWEEP"

    def handle(self, ctx: Context, i: int) -> State:
        r = ctx.ind.iloc[i]
        if i <= ctx.cooldown_until or r.rvol < ctx.cfg.sweep_rvol_min:
            return self
        direction = 0
        if r.cloc > ctx.cfg.sweep_loc_long and r.cvd_slope > 0:        # buying sweep + flow confirms
            direction = 1
        elif r.cloc < ctx.cfg.sweep_loc_short and r.cvd_slope < 0:     # selling sweep + flow confirms
            direction = -1
        if direction == 0:
            return self
        return _open_position(ctx, i, direction)


class ReAccelFlatState(State):
    """STRATEGY 2 -- Macro MA Re-Acceleration / trend continuation. In a highly efficient macro trend
    (macro_ker > 0.6), price pulls back to TOUCH/PIERCE the KAMA then REJECTS and closes back on the
    trend side, with a high-speed z-velocity rejection (|z_vel| > z_vel_min). Buy the dip in an uptrend,
    sell the rip in a downtrend."""
    name = "FLAT_REACCEL"

    def handle(self, ctx: Context, i: int) -> State:
        r = ctx.ind.iloc[i]
        if i <= ctx.cooldown_until or r.macro_ker < ctx.cfg.macro_ker_min:
            return self
        lo, hi, close, kama_v = float(r.Low), float(r.High), float(r.Close), float(r.kama)
        if np.isnan(kama_v):
            return self
        direction = 0
        if r.macro_dir > 0 and lo <= kama_v and close > kama_v and r.z_vel > ctx.cfg.z_vel_min:
            direction = 1                                              # uptrend: dip to KAMA, reject up
        elif r.macro_dir < 0 and hi >= kama_v and close < kama_v and r.z_vel < -ctx.cfg.z_vel_min:
            direction = -1                                             # downtrend: rip to KAMA, reject down
        if direction == 0:
            return self
        return _open_position(ctx, i, direction)


# ============================================================================ ENGINE
class MomentumBreakdownEngine:
    """Drives the FSM over a precomputed indicator frame. Indicators vectorized up front; the FSM is
    a single O(n) causal pass (a path-dependent state machine cannot be vectorized)."""

    def __init__(self, cfg: Config = Config(), flat_state_cls: type = FlatPositionState):
        self.cfg = cfg
        self.flat_state_cls = flat_state_cls

    def run(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.sort_index()
        ind = build_indicators(df, self.cfg)
        ctx = Context(self.cfg, df, ind, flat_state_cls=self.flat_state_cls)
        state: State = self.flat_state_cls()
        warmup = max(self.cfg.bb_period, self.cfg.kc_period, self.cfg.atr_period,
                     self.cfg.z_period, self.cfg.kama_slow)
        for i in range(warmup, len(df)):
            state = state.handle(ctx, i)
        if ctx.position != 0:
            ActivePositionState()._exit(ctx, len(df) - 1, float(df["Close"].iloc[-1]), "eod")
        return pd.DataFrame(ctx.trades)


# ============================================================================ MODULE 3-ADJACENT: HARNESS
COST_BPS = 6.0   # round-trip transaction cost (slippage + spread), basis points


def apply_cost(trades: pd.DataFrame, cost_bps: float = COST_BPS) -> pd.DataFrame:
    """Net P&L after a round-trip cost. Cost in price = (bps/1e4)*entry; converted to the trade's
    ATR-R unit via /entry_atr so it nets directly against r_atr. This strips microscopic noise-wins."""
    if trades.empty:
        return trades
    t = trades.copy()
    t["cost_r"] = (cost_bps / 1e4) * t["entry"] / t["entry_atr"].clip(lower=1e-9)
    t["net_r"] = t["r_atr"] - t["cost_r"]
    return t


def _summary(trades: pd.DataFrame, label: str) -> None:
    if trades.empty:
        print(f"  {label:<11} 0 trades")
        return
    n = len(trades)
    gross, net, avg = trades["r_atr"].sum(), trades["net_r"].sum(), trades["net_r"].mean()
    win = 100.0 * (trades["net_r"] > 0).mean()
    wins = trades.loc[trades.net_r > 0, "net_r"].sum()
    losses = -trades.loc[trades.net_r < 0, "net_r"].sum()
    pf = wins / max(1e-9, losses)
    print(f"  {label:<11} {n:>4} tr | gross {gross:+7.1f}R | net {net:+7.1f}R | "
          f"avg {avg:+.3f}R | win {win:4.0f}% | PF {pf:.2f}")


def holdout_test(df: pd.DataFrame, cfg: Config = Config(), split: float = 0.75,
                 cost_bps: float = COST_BPS, flat_state_cls: type = FlatPositionState) -> dict:
    """Chronological 75/25 holdout. The FSM runs INDEPENDENTLY on each split (no cross-boundary
    leakage), a round-trip cost is applied to every trade, and in-sample vs out-of-sample metrics
    are printed so we can see whether the momentum edge actually CARRIES forward (vs. an in-sample
    mirage). Same discipline as the mean-rev / ORB harness. `flat_state_cls` selects the strategy."""
    df = df.sort_index()
    cut = int(len(df) * split)
    train, test = df.iloc[:cut], df.iloc[cut:]
    eng = MomentumBreakdownEngine(cfg, flat_state_cls=flat_state_cls)
    tr = apply_cost(eng.run(train), cost_bps)
    te = apply_cost(eng.run(test), cost_bps)
    print(f"HOLDOUT {int(split*100)}/{int((1-split)*100)} chronological | {cost_bps:.0f}bp round-trip cost")
    print(f"  train bars {len(train):>6}  ({train.index[0].date()} -> {train.index[-1].date()})")
    print(f"  test  bars {len(test):>6}  ({test.index[0].date()} -> {test.index[-1].date()})")
    _summary(tr, "IN-SAMPLE")
    _summary(te, "OUT-SAMPLE")
    carries = (not te.empty) and te["net_r"].sum() > 0 and te["net_r"].mean() > 0
    print(f"  VERDICT: edge {'CARRIES' if carries else 'does NOT carry'} out-of-sample after {cost_bps:.0f}bp")
    return {"in_sample": tr, "out_sample": te, "carries": carries}


# ============================================================================ EXAMPLE
if __name__ == "__main__":
    import os
    import sys

    # Usage:
    #   python momentum_breakdown.py                 -> synthetic demo
    #   python momentum_breakdown.py NVDA            -> wf_cache ticker (data/wf_cache/NVDA.parquet)
    #   python momentum_breakdown.py path/to.parquet -> explicit file
    #   ... --no-target                              -> ablation: disable the AVWAP hard target
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    no_target = "--no-target" in sys.argv

    if args:
        arg = args[0]
        if os.path.exists(arg):
            path = arg
        elif os.path.exists(f"data/wf_cache/{arg}.parquet"):   # bare ticker -> wf_cache
            path = f"data/wf_cache/{arg}.parquet"
        else:
            raise SystemExit(f"no such file or wf_cache ticker: {arg}")
        data = pd.read_parquet(path) if path.endswith(".parquet") else pd.read_csv(path, index_col=0, parse_dates=True)
        data = data.rename(columns=str.capitalize) if "close" in data.columns else data
        print(f"TICKER {arg}  ({path})")
    else:
        idx = pd.date_range("2026-01-02 09:30", periods=600, freq="5min")
        rng = np.random.default_rng(0)
        base = np.r_[np.cumsum(rng.normal(0, 0.02, 250)),
                     np.cumsum(rng.normal(0.12, 0.05, 100)),
                     np.cumsum(rng.normal(0, 0.05, 250))] + 100
        data = pd.DataFrame({"Open": base, "High": base + 0.12, "Low": base - 0.12,
                             "Close": base + rng.normal(0, 0.02, 600),
                             "Volume": rng.integers(1e4, 1e5, 600)}, index=idx)

    cfg = Config(disable_avwap_target=no_target)
    print(f"CONFIG  static_chandelier={cfg.static_chandelier} ce_mult={cfg.ce_atr_mult_normal} "
          f"time_stop_disabled={cfg.disable_time_stop} avwap_target={'OFF' if cfg.disable_avwap_target else f'{cfg.avwap_band_mult}SD'}")
    holdout_test(data, cfg)
