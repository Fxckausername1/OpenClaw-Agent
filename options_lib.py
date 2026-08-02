#!/usr/bin/env python3
"""
options_lib.py — primitives for the autonomous options agent (OPTIONS_AGENT_BLUEPRINT.md).
Pure, light, unit-testable math. No live API, no heavy compute → safe to build/test anytime.

Implements:
  §1 Translation Engine : breeden_litzenberger_rnd, optimize_spread (discrete grid), skew_slope
  §2 Friction Layer     : slippage_penalty, pacing_ladder
  §3 Tournament         : thompson_rank, expected_max_sharpe (DSR SR*), probabilistic_sharpe
  §4 Portfolio Risk     : select_portfolio_milp, expected_shortfall_atr
  §5 Reconciliation     : slippage_drag, ema_update

Run:  ./venv/bin/python options_lib.py --selftest    ($0, instant)
"""
import argparse
import sys
from dataclasses import dataclass, field
from typing import List, Literal, Optional

import numpy as np
from scipy.stats import norm
from scipy.interpolate import UnivariateSpline
from scipy.optimize import milp, LinearConstraint, Bounds, brentq

from greeks import bs_price  # reuse the BS core already unit-tested in greeks.py

# ----------------------------------------------------------------- §0 constants
MAX_CONCURRENT_POSITIONS = 3
MAX_POSITIONS_PER_SIDE = 2
EXECUTION_CYCLE_SECONDS = 120
EXECUTION_POLL_TICKS = 4
NAV_ES_LIMIT = 0.05
MAB_SUCCESS_THRESHOLD = 0.5
PSR_PROMOTE_THRESH = 0.95
PSR_KILL_THRESH = 0.50
MIN_TRADES_FOR_KILL = 20
SLIPPAGE_EMA_ALPHA = 0.10
GAMMA_EULER = 0.5772156649  # Euler-Mascheroni
MAX_RISK_PER_TRADE = 150.0  # HARD cap: a single spread's max loss (×100/contract) must be <= this
# updated 2026-06-28 (heff's call): 100->500, 50% of the $1000 book per trade (was 10%).
# updated 2026-07-02 (heff's call, alongside the tournament reset): 500->150, 15% of the book --
# the 6/30 friction root-cause showed $500 let one bad session burn ~4x the real book.
# NOTE the interaction with MAX_SPREAD_WIDTH below: at width=5 a single $5-wide contract's
# worst-case loss is ~$500 (5pts x 100 multiplier), so width -- not the dollar cap -- is now
# the binding constraint for wide spreads; a single bad spread can now consume up to half
# the book in one trade where it couldn't before.
MAX_SPREAD_WIDTH = 5.0      # HARD cap on strike width (points) — defined-risk, $1000 book


# ----------------------------------------------------------------- DTOs
@dataclass
class SpreadLeg:
    strike: float
    option_type: Literal["C", "P"]
    side: Literal["BUY", "SELL"]
    iv: float
    bid: float
    ask: float


@dataclass
class ProposedSpread:
    ticker: str
    direction: Literal[1, -1]          # 1 bullish, -1 bearish
    strategy_id: str
    kind: str                          # 'bull_put' | 'bear_call' | ...
    legs: List[SpreadLeg]
    net_credit: float                  # >0 = credit received
    max_loss: float
    ev: float
    pop: float
    r_multiple: float
    p_mid: float
    p_nat: float
    utility: float = 0.0


@dataclass
class StrategyState:
    strategy_id: str
    alpha: float = 1.0                 # Beta(1,1) flat prior
    beta: float = 1.0
    returns: List[float] = field(default_factory=list)
    status: Literal["LIVE", "PAPER", "KILLED"] = "PAPER"


# ===================================================== §1 TRANSLATION ENGINE
def breeden_litzenberger_rnd(strikes, ivs, S, T, r, n_grid=400, spline_s=None):
    """Risk-neutral density f(S_T) from the IV smile: f(K)=e^{rT} ∂²C/∂K².
    Returns (grid, density). Invariant: ∫f=1 and ∫x·f dx = S·e^{rT} (forward)."""
    strikes = np.asarray(strikes, float)
    ivs = np.asarray(ivs, float)
    order = np.argsort(strikes)
    strikes, ivs = strikes[order], ivs[order]
    # smooth the smile (4th-deg spline; default light smoothing) then resample dense+uniform
    k = min(4, len(strikes) - 1)
    s = spline_s if spline_s is not None else len(strikes) * 1e-6
    smile = UnivariateSpline(strikes, ivs, k=k, s=s, ext=3)  # ext=3 -> clamp at ends
    grid = np.linspace(strikes[0], strikes[-1], n_grid)
    h = grid[1] - grid[0]
    sig = np.clip(smile(grid), 1e-3, 5.0)
    C = bs_price(S, grid, T, r, sig, True)
    d2 = np.full_like(grid, np.nan)
    d2[1:-1] = (C[2:] - 2 * C[1:-1] + C[:-2]) / (h * h)
    dens = np.exp(r * T) * d2
    dens = np.clip(np.nan_to_num(dens, nan=0.0), 0.0, None)
    area = np.trapezoid(dens, grid)
    if area > 0:
        dens = dens / area
    return grid, dens


def _cdf_upto(grid, dens, x):
    """∫ dens dK from grid[0] up to x (trapezoid)."""
    mask = grid <= x
    if mask.sum() < 2:
        return 0.0
    return float(np.trapezoid(dens[mask], grid[mask]))


def skew_slope(iv_10delta, iv_50delta):
    return (iv_10delta - iv_50delta) / iv_50delta


def esscher_tilt(grid, dens, S0, mu_signal):
    """Signal→density tilt via Entropy Pooling (KL-min to the RND s.t. one expected-return view),
    whose closed form IS the Esscher transform: p*_i ∝ p_i·e^{θ·S_i}. Solve the single monotonic
    root g(θ)=Σp_i S_i e^{θS_i}/Σp_i e^{θS_i} − S0(1+μ)=0 (Brent). Returns (tilted_dens, theta).
    μ_signal = α·z (the equity signal's projected return). θ>0 shifts mass up, θ<0 down."""
    grid = np.asarray(grid, float)
    h = grid[1] - grid[0]
    p = np.clip(dens, 0, None) * h
    p = p / p.sum()
    # View anchored to the PRIOR mean, not S0: our grid is a truncated NTM band whose renormalized
    # mean drifts off S0, so S0·(1+μ) can invert the tilt direction. prior_mean·(1+μ) guarantees a
    # bullish (μ>0) signal always shifts mass UP (θ>0) relative to where the density currently sits.
    prior_mean = float(np.sum(p * grid))
    target = prior_mean * (1 + mu_signal)
    target = float(np.clip(target, grid.min() + 1e-6, grid.max() - 1e-6))  # view must be reachable

    def g(theta):
        w = p * np.exp(theta * (grid - S0))      # center exponent at S0 for numerical stability
        return float(np.sum(w * grid) / np.sum(w) - target)

    lo, hi = -1.0, 1.0
    for _ in range(60):
        if g(lo) < 0:
            break
        lo *= 2
    for _ in range(60):
        if g(hi) > 0:
            break
        hi *= 2
    theta = brentq(g, lo, hi, maxiter=200)
    w = p * np.exp(theta * (grid - S0))
    post = w / w.sum()
    return post / h, float(theta)             # back to a density


def optimize_spread(ticker, direction, strategy_id, chain, S, T, r,
                    grid, dens, slippage=0.0, max_width=None,
                    pop_min=0.65, r_min=0.33, w=(1.0, 0.5, 1.0),
                    max_risk=MAX_RISK_PER_TRADE):
    """Discrete vectorized strike optimizer over the RND. `chain` = list of dicts per strike:
    {strike, type 'C'/'P', bid, ask, iv}. Returns best ProposedSpread or None.
    kind from direction: bullish→bull_put (sell higher put / buy lower put);
    bearish→bear_call (sell lower call / buy higher call). Credit spreads.
    HARD RISK CAP: rejects any spread whose single-contract max loss (max_loss*100) exceeds
    `max_risk`, and whose strike width exceeds `max_width` (default MAX_SPREAD_WIDTH)."""
    kind = "bull_put" if direction == 1 else "bear_call"
    opt = "P" if direction == 1 else "C"
    if max_width is None:
        max_width = MAX_SPREAD_WIDTH
    legs = [c for c in chain if c["type"] == opt and c["bid"] > 0 and c["ask"] > 0]
    if len(legs) < 2:
        return None
    K = np.array([c["strike"] for c in legs], float)
    bid = np.array([c["bid"] for c in legs], float)
    ask = np.array([c["ask"] for c in legs], float)
    iv = np.array([c["iv"] for c in legs], float)
    width_cap = max_width if max_width else (K.max() - K.min())
    best = None
    for si in range(len(legs)):
        for li in range(len(legs)):
            if direction == 1:                      # bull put: short higher, long lower, both OTM
                if not (K[si] > K[li] and K[si] < S):
                    continue
            else:                                   # bear call: short lower, long higher, both OTM
                if not (K[si] < K[li] and K[si] > S):
                    continue
            width = abs(K[si] - K[li])
            if width <= 0 or width > width_cap:
                continue
            credit = bid[si] - ask[li]              # pessimistic: sell at bid, buy at ask
            if credit <= 0:
                continue
            max_loss = width - credit
            if max_loss <= 0:
                continue
            if max_loss * 100.0 > max_risk:        # HARD per-trade risk cap (reject, e.g., BAC $136)
                continue
            # breakeven & PoP from the RND
            if direction == 1:
                breakeven = K[si] - credit          # bull put: profit if S_T >= breakeven
                pop = 1.0 - _cdf_upto(grid, dens, breakeven)
            else:
                breakeven = K[si] + credit          # bear call: profit if S_T <= breakeven
                pop = _cdf_upto(grid, dens, breakeven)
            # EV = ∫ payoff·f − 0 (cost already in credit/max_loss);  payoff at expiry over grid
            if direction == 1:
                payoff = np.clip(credit - np.clip(K[si] - grid, 0, None) + np.clip(K[li] - grid, 0, None),
                                 -max_loss, credit)
            else:
                payoff = np.clip(credit - np.clip(grid - K[si], 0, None) + np.clip(grid - K[li], 0, None),
                                 -max_loss, credit)
            ev = float(np.trapezoid(payoff * dens, grid)) - slippage
            rr = credit / max_loss
            if pop < pop_min or rr < r_min or ev <= 0:
                continue
            util = w[0] * (ev / max_loss) + w[1] * np.log(rr) + w[2] * pop
            p_mid = credit
            p_nat = bid[si] - ask[li]               # natural credit (already pessimistic here)
            cand = ProposedSpread(
                ticker=ticker, direction=direction, strategy_id=strategy_id, kind=kind,
                legs=[SpreadLeg(K[si], opt, "SELL", iv[si], bid[si], ask[si]),
                      SpreadLeg(K[li], opt, "BUY", iv[li], bid[li], ask[li])],
                net_credit=credit, max_loss=max_loss, ev=ev, pop=pop, r_multiple=rr,
                p_mid=p_mid, p_nat=p_nat, utility=util)
            if best is None or util > best.utility:
                best = cand
    return best


# ===================================================== §2 FRICTION LAYER
def slippage_penalty(spread_width, lam, sigma, Q, V):
    """½·Δspread + λ·σ·√(Q/V) — half-spread + square-root market impact."""
    return 0.5 * spread_width + lam * sigma * np.sqrt(Q / max(V, 1e-9))


def pacing_ladder(p_mid, p_nat, kappa=2.0, n=EXECUTION_POLL_TICKS):
    """Exponential urgency ladder L(t), t=0..n. L(0)=p_mid (passive), L(n)=p_nat (cross)."""
    ts = np.arange(n + 1)
    frac = (np.exp(kappa * (ts / n)) - 1.0) / (np.exp(kappa) - 1.0)
    return p_mid - (p_mid - p_nat) * frac


# ===================================================== §3 TOURNAMENT
def thompson_rank(states: List[StrategyState], rng=None):
    """Draw θ̂~Beta(α,β) per strategy; return indices ranked by sample desc."""
    rng = rng or np.random.default_rng()
    samples = np.array([rng.beta(s.alpha, s.beta) for s in states])
    return list(np.argsort(-samples)), samples


def expected_max_sharpe(sharpes):
    """DSR hurdle SR* = expected max Sharpe across N strategies under the null (pure noise)."""
    sharpes = np.asarray(sharpes, float)
    N = len(sharpes)
    if N < 2:
        return 0.0
    v = np.var(sharpes, ddof=1)
    g = GAMMA_EULER
    return float(np.sqrt(v) * ((1 - g) * norm.ppf(1 - 1.0 / N) + g * norm.ppf(1 - 1.0 / (N * np.e))))


def probabilistic_sharpe(returns, sr_star=0.0):
    """PSR(SR*) = Φ((SR−SR*)√(T−1)/√(1−skew·SR+(kurt−1)/4·SR²)). SR is per-trade."""
    x = np.asarray(returns, float)
    T = len(x)
    if T < 2 or x.std(ddof=1) == 0:
        return 0.0
    sr = x.mean() / x.std(ddof=1)
    m = x - x.mean()
    sd = x.std(ddof=0)
    skew = np.mean(m ** 3) / sd ** 3
    kurt = np.mean(m ** 4) / sd ** 4
    denom = np.sqrt(max(1 - skew * sr + (kurt - 1) / 4.0 * sr * sr, 1e-9))
    return float(norm.cdf((sr - sr_star) * np.sqrt(T - 1) / denom))


# ===================================================== §4 PORTFOLIO RISK
def select_portfolio_milp(signals: List[ProposedSpread],
                          cur_bull=0, cur_bear=0,
                          max_total=MAX_CONCURRENT_POSITIONS,
                          max_side=MAX_POSITIONS_PER_SIDE):
    """Binary knapsack: max Σ x_i·EV_i s.t. total/per-side caps (accounting for open positions)."""
    n = len(signals)
    if n == 0:
        return []
    c = -np.array([s.ev for s in signals])          # milp minimizes
    bull = np.array([1.0 if s.direction == 1 else 0.0 for s in signals])
    bear = np.array([1.0 if s.direction == -1 else 0.0 for s in signals])
    A = np.vstack([np.ones(n), bull, bear])
    b_up = np.array([max_total - (cur_bull + cur_bear),
                     max_side - cur_bull, max_side - cur_bear], float)
    b_up = np.clip(b_up, 0, None)
    res = milp(c=c, constraints=LinearConstraint(A, -np.inf, b_up),
               integrality=np.ones(n), bounds=Bounds(0, 1))
    if not res.success:
        return []
    return [signals[i] for i in np.where(np.round(res.x) == 1)[0]]


def expected_shortfall_atr(positions, atr_mult=3.0, alpha=0.99):
    """Stress each defined-risk position by ±atr_mult×ATR; ES = mean of worst (1−alpha) tail losses.
    positions: list of dicts {max_loss, atr_loss_frac} where atr_loss_frac∈[0,1] = fraction of
    max_loss realized under the shock (caller computes from spread mark at the shocked node)."""
    losses = np.array([p["max_loss"] * p.get("atr_loss_frac", 1.0) for p in positions], float)
    if len(losses) == 0:
        return 0.0
    # portfolio-level: simultaneous shock => sum; ES over a small MC of independent gap signs
    rng = np.random.default_rng(0)
    sims = np.array([np.sum(losses * rng.choice([0.0, 1.0], size=len(losses), p=[1 - 0.5, 0.5]))
                     for _ in range(2000)])
    var = np.quantile(sims, alpha)
    tail = sims[sims >= var]
    return float(tail.mean()) if len(tail) else float(var)


# ===================================================== §5 RECONCILIATION
def slippage_drag(p_realized, p_mid, is_credit):
    """δ = (P_realized − P_mid)·D, D=−1 credit / +1 debit. Positive δ = cost (negative slippage)."""
    D = -1.0 if is_credit else 1.0
    return (p_realized - p_mid) * D


def ema_update(new, prev, eta=SLIPPAGE_EMA_ALPHA):
    return eta * new + (1 - eta) * prev


# ===================================================== §6 ORCHESTRATOR PRIMITIVES
class TokenBucket:
    """Alpaca 200 req/min guard (orchestrator spec §2). Capacity 190, refill 190/60 ≈ 3.16/s,
    buffered below the hard limit for jitter/retries. Injectable clock → unit-testable without
    real sleeping. take() returns the seconds a caller must wait (0 if a token is available now)."""
    def __init__(self, capacity=190, refill_per_sec=190 / 60.0, clock=None):
        self.capacity = float(capacity)
        self.refill = float(refill_per_sec)
        self.tokens = float(capacity)
        self.clock = clock or __import__("time").perf_counter
        self.t = self.clock()

    def _refill(self):
        now = self.clock()
        self.tokens = min(self.capacity, self.tokens + (now - self.t) * self.refill)
        self.t = now

    def take(self, n=1):
        self._refill()
        if self.tokens >= n:
            self.tokens -= n
            return 0.0
        return (n - self.tokens) / self.refill      # wait this long, then a token is ready


# API-call priority (orchestrator spec §2 PriorityQueue): lower number = higher priority.
API_PRIORITY = {"cancel_replace": 1, "mleg_submit": 2, "reconcile": 3, "chain_poll": 4}

# multi-leg order FSM states (orchestrator spec §4)
FSM_STATES = ["INIT", "PENDING_NEW", "WORKING", "REPLACING", "PENDING_REPLACE",
              "FILLED", "CANCELED", "RECOVERY_HEDGE"]


# ===================================================== SELFTEST
def selftest():
    ok = True

    def check(name, good):
        nonlocal ok
        ok &= bool(good)
        print(f"  [{'OK' if good else 'FAIL'}] {name}")

    S, T, r = 100.0, 30 / 365, 0.05
    # §1 RND: flat smile → risk-neutral mean must equal forward S·e^{rT}
    strikes = np.arange(70, 131, 2.5)
    ivs = np.full_like(strikes, 0.25)
    grid, dens = breeden_litzenberger_rnd(strikes, ivs, S, T, r, n_grid=600)
    integral = np.trapezoid(dens, grid)
    mean = np.trapezoid(grid * dens, grid)
    fwd = S * np.exp(r * T)
    check("RND integrates to 1", abs(integral - 1) < 0.02)
    check(f"RND mean≈forward ({mean:.2f} vs {fwd:.2f})", abs(mean - fwd) / fwd < 0.01)
    check("RND non-negative", float(dens.min()) >= -1e-9)

    # §1 optimizer: synthetic put chain, bull put credit spread
    chain = []
    for K in np.arange(80, 121, 5.0):
        iv = 0.25
        px = float(bs_price(S, K, T, r, iv, False))   # put mid
        chain.append({"strike": K, "type": "P", "bid": max(px - 0.05, 0.01),
                      "ask": px + 0.05, "iv": iv})
        cx = float(bs_price(S, K, T, r, iv, True))
        chain.append({"strike": K, "type": "C", "bid": max(cx - 0.05, 0.01),
                      "ask": cx + 0.05, "iv": iv})
    # A bullish equity signal (z=1.5) tilts the density UP via the Esscher transform (Entropy Pooling
    # to a +μ view). Under the pure RND a fair spread has EV≈0 by no-arb; the signal creates the edge.
    mu_signal = 0.01 * 1.5                     # α·z, α=1% per z (calibratable)
    prior_mean = np.trapezoid(grid * dens, grid)
    dens_bull, theta = esscher_tilt(grid, dens, S, mu_signal)
    tilted_mean = np.trapezoid(grid * dens_bull, grid)
    check("esscher tilt integrates to 1", abs(np.trapezoid(dens_bull, grid) - 1) < 0.02)
    check(f"esscher hits target mean ({tilted_mean:.3f} vs {prior_mean*(1+mu_signal):.3f})",
          abs(tilted_mean - prior_mean * (1 + mu_signal)) / S < 0.005)
    check("bullish view => mean shifts UP vs prior", tilted_mean > prior_mean)
    check("bullish view => theta>0", theta > 0)
    # synthetic S=100 / $5-wide strikes -> spreads are $500+/contract, so use a high max_risk here
    # (this is an abstract test, not the $1000 book); the cap is exercised separately below.
    sp = optimize_spread("TEST", 1, "MR_bullput", chain, S, T, r, grid, dens_bull,
                         slippage=0.0, max_width=10, pop_min=0.55, r_min=0.10, max_risk=1e9)
    check("optimizer returns a spread (signal-tilted density)", sp is not None)
    # control: under the PURE risk-neutral density, no positive-EV spread should exist
    check("pure RND yields no positive-EV edge (no-arb)",
          optimize_spread("TEST", 1, "x", chain, S, T, r, grid, dens, max_width=10,
                          pop_min=0.55, r_min=0.10, max_risk=1e9) is None)
    check("hard risk cap rejects spreads above max_risk",
          optimize_spread("TEST", 1, "x", chain, S, T, r, grid, dens_bull, max_width=10,
                          pop_min=0.55, r_min=0.10, max_risk=10.0) is None)
    if sp:
        check("credit > 0", sp.net_credit > 0)
        check("0<PoP<1", 0 < sp.pop < 1)
        check("short put > long put (bull put)", sp.legs[0].strike > sp.legs[1].strike)
        check("max_loss = width - credit", abs(sp.max_loss - (abs(sp.legs[0].strike - sp.legs[1].strike) - sp.net_credit)) < 1e-6)

    # §2 friction
    pen = slippage_penalty(0.10, lam=0.1, sigma=0.2, Q=5, V=500)
    check("slippage penalty > half-spread", pen > 0.05)
    ladder = pacing_ladder(1.00, 0.80, kappa=2.0, n=4)
    check("pacing L(0)=mid", abs(ladder[0] - 1.00) < 1e-9)
    check("pacing L(N)=nat", abs(ladder[-1] - 0.80) < 1e-9)
    check("pacing monotone down", bool(np.all(np.diff(ladder) <= 1e-9)))

    # §3 tournament
    states = [StrategyState(f"s{i}", alpha=1 + i, beta=10 - i) for i in range(5)]
    rank, samp = thompson_rank(states, rng=np.random.default_rng(1))
    check("thompson returns full ranking", len(rank) == 5)
    srs = [2.0, 1.0, 0.5, 0.2, -0.3]
    srstar = expected_max_sharpe(srs)
    check(f"DSR SR* positive ({srstar:.3f})", srstar > 0)
    good_returns = np.random.default_rng(2).normal(0.3, 1.0, 60)   # SR≈0.3, 60 trades
    psr = probabilistic_sharpe(good_returns, sr_star=0.0)
    psr_short = probabilistic_sharpe(good_returns[:10], sr_star=0.0)
    check(f"PSR in [0,1] ({psr:.3f})", 0 <= psr <= 1)
    check("PSR grows with sample length", psr >= psr_short)

    # §4 portfolio MILP: 5 signals (3 bull, 2 bear), pick best ≤3 with ≤2/side
    sigs = []
    for i, (d, ev) in enumerate([(1, 5), (1, 4), (1, 3), (-1, 6), (-1, 1)]):
        sigs.append(ProposedSpread("T", d, f"s{i}", "k", [], 1, 1, ev, 0.7, 0.5, 1, 0.9))
    sel = select_portfolio_milp(sigs, cur_bull=0, cur_bear=0)
    nb = sum(1 for s in sel if s.direction == 1)
    nr = sum(1 for s in sel if s.direction == -1)
    check("MILP picks <=3", len(sel) <= 3)
    check("MILP <=2 per side", nb <= 2 and nr <= 2)
    check("MILP took the best bear (ev=6)", any(abs(s.ev - 6) < 1e-9 for s in sel))

    # §5 reconciliation
    drag = slippage_drag(p_realized=0.95, p_mid=1.00, is_credit=True)  # filled for less credit = cost
    check("credit fill below mid = positive drag (cost)", drag > 0)
    e = ema_update(0.10, 0.02, eta=0.1)
    check("EMA update between prev and new", 0.02 < e < 0.10)

    # §6 token bucket (injectable clock — no real sleeping)
    clk = {"t": 0.0}
    tb = TokenBucket(capacity=5, refill_per_sec=1.0, clock=lambda: clk["t"])
    waits = [tb.take(1) for _ in range(5)]
    check("token bucket: first 5 takes free", all(w == 0 for w in waits))
    w6 = tb.take(1)
    check("token bucket: 6th take must wait ~1s", abs(w6 - 1.0) < 1e-6)
    clk["t"] = 3.0
    check("token bucket: refills after time passes", tb.take(1) == 0)
    check("priority: cancel_replace beats chain_poll", API_PRIORITY["cancel_replace"] < API_PRIORITY["chain_poll"])

    print("SELFTEST", "PASS" if ok else "FAIL")
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        sys.exit(0 if selftest() else 1)
    ap.print_help()


if __name__ == "__main__":
    main()
