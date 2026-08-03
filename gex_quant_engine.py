#!/usr/bin/env python3
"""
gex_quant_engine.py — Phase 1 real-time options microstructure engine.

Implements, per "Gamma Exposure Quantitative Modeling.pdf" (uploaded reference,
hereafter "the paper"):
  1. Live ingestion + Effective Open Interest (EOI) blending  (paper pg. 3)
  2. Algorithmic trade flow classification (Lee-Ready / EMO / BVC)  (paper pg. 4-5)
  3. DynamicGammaFlipEngine: vectorized Newton-Raphson gamma-flip solver with
     analytical Speed-based Jacobian, brentq fallback  (paper pg. 5-8)
  4. SessionClock: live intraday time decay feeding the Color-driven steepening
     of the Gamma profile near the close  (paper pg. 8)

CORRECTIONS APPLIED VS. THE SOURCE PDF (verified independently before shipping
this into anything that trades real money — see notes at each site below):

  (A) Speed formula, page 6. The paper states:
          Speed = -(Gamma/S) * (d1/(sigma*sqrt(T)) + 2)
      Differentiating the paper's own Gamma formula (pg. 2) analytically, and
      cross-checked against a central-difference numerical derivative of
      Gamma w.r.t. S, the correct coefficient is +1, not +2:
          Speed = -(Gamma/S) * (d1/(sigma*sqrt(T)) + 1)
      Numerical check (S=100,K=105,r=4%,q=1%,sigma=22%,T=0.15y):
          finite-difference dGamma/dS = 0.00191935
          formula with +1              = 0.00191935   (matches to 1e-9)
          formula with +2 (as printed) = 0.00150215   (off by ~22%)
      Using the paper's literal +2 would silently corrupt the analytical
      Jacobian passed to Newton-Raphson, degrading it from quadratic
      convergence to something worse, and would give a wrong dealer-flow
      reading at exactly the moments (near a Call Wall) the paper itself
      flags as the highest-stakes edge case (Speed sign flips, pg. 9-10).

  (B) Dealer gamma sign convention, page 4 (Quote Rule bullets). The paper's
      prose states:
          "P_t > M_t -> Customer Buy -> dealer sells the option -> dealer
           becomes LONG Gamma"
          "P_t < M_t -> Customer Sell -> dealer buys the option -> dealer
           becomes SHORT Gamma"
      This is backwards under standard option theory: a SHORT option position
      (dealer wrote/sold it to the customer) carries NEGATIVE (short) gamma;
      a LONG position (dealer bought it) carries POSITIVE (long) gamma — for
      calls and puts alike. Note the paper's own page-2 static convention
      (w_C=+1 for calls, w_P=-1 for puts, under "dealers short calls -> long
      dealer gamma") has the identical inconsistency baked in.
      This implementation uses the theoretically correct mapping:
          Customer Buy  -> dealer short -> dealer weight contribution -1
          Customer Sell -> dealer long  -> dealer weight contribution +1
      Shipping the paper's literal mapping would flip the sign of every
      dynamically-classified strike's contribution to Net $GEX.

Both corrections are narrow (one constant, one sign) and are called out again
inline at the exact line they apply, so a reviewer can find and re-derive them
without re-reading this header.

Everything else (the EOI exponential-decay blend, the $GEX formula, the
Lee-Ready / EMO / BVC classification hierarchy, the Newton-Raphson + brentq
architecture, and the live-clock Color/time-decay mechanism) follows the paper
as specified.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time as dt_time
from typing import Optional, Tuple, Union

import numpy as np
import pandas as pd
from numpy.typing import NDArray
from scipy.interpolate import splev, splrep
from scipy.optimize import brentq, newton
from scipy.special import expit
from scipy.stats import norm

FloatArray = NDArray[np.float64]

# Minimum time-to-expiration floor (years), per the paper's edge-case rule:
# "enforcing the small epsilon threshold (1e-5) outlined in the paper."
EPS_T: float = 1e-5
SECONDS_PER_YEAR: float = 365.0 * 86400.0


# =============================================================================
# COMPONENT 1 — Live Ingestion & Effective Open Interest (EOI) Blending Model
#   EOI_t = OI_{T-1} * exp(-lambda * (V_cumulative / OI_{T-1})) + NetIntradayFlow_t
#   (the paper, pg. 3, "The Effective Open Interest (EOI) Blending Model")
# =============================================================================
@dataclass
class ChainState:
    """Vectorized, tick-by-tick option-chain state for one snapshot in time.

    Holds one row per contract in the chain. `apply_ticks` ingests a whole
    batch of trades (a "tick") in one vectorized call via np.bincount scatter-
    aggregation — no Python loop over individual trades, matching the paper's
    call for "a vectorized pipeline that updates the option chain state on a
    tick-by-tick basis."

    Attributes:
        prior_oi: OCC T-1 (legacy, overnight-settled) Open Interest per contract.
        lam: configurable decay constant lambda, calibrated empirically per
            the paper ("lambda is an empirically derived decay constant
            calibrated to historical OCC settlement data").
    """

    prior_oi: FloatArray
    lam: float = 1.5
    _cum_volume: FloatArray = field(init=False, repr=False)
    _net_flow: FloatArray = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.prior_oi = np.asarray(self.prior_oi, dtype=np.float64)
        n = self.prior_oi.shape[0]
        self._cum_volume = np.zeros(n, dtype=np.float64)
        self._net_flow = np.zeros(n, dtype=np.float64)

    def apply_ticks(
        self,
        contract_idx: NDArray[np.intp],
        volume: FloatArray,
        initiator: FloatArray,
    ) -> None:
        """Ingest a batch of classified trades in one vectorized update.

        Args:
            contract_idx: index into the chain (0..n_contracts-1) per trade.
            volume: contract volume traded, per trade.
            initiator: I_k in {-1, +1} (Sell=-1, Buy=+1) from the trade flow
                classification engine (Component 2).
        """
        n = self.prior_oi.shape[0]
        volume = np.asarray(volume, dtype=np.float64)
        signed_volume = volume * np.asarray(initiator, dtype=np.float64)
        self._cum_volume += np.bincount(contract_idx, weights=volume, minlength=n)
        self._net_flow += np.bincount(contract_idx, weights=signed_volume, minlength=n)

    @property
    def effective_oi(self) -> FloatArray:
        """EOI_t per contract — the paper's exponential decay blend.

        Contracts with prior_oi == 0 (new/illiquid strikes, e.g. a freshly
        listed 0DTE strike) have no legacy OI to decay; EOI there is just the
        net intraday flow. This is an explicit division-by-zero guard, not
        part of the cited formula.
        """
        has_legacy = self.prior_oi > 0
        safe_prior = np.where(has_legacy, self.prior_oi, 1.0)  # avoid /0; masked out below
        decay_term = self.prior_oi * np.exp(-self.lam * (self._cum_volume / safe_prior))
        decay_term = np.where(has_legacy, decay_term, 0.0)
        eoi = decay_term + self._net_flow
        return np.maximum(eoi, 0.0)  # EOI is a position count; cannot be negative

    @property
    def v_oi_ratio(self) -> FloatArray:
        """Volume-to-Open-Interest ratio per contract (Phase 3, "Options
        Market Microstructure Research.pdf", pg. 2-3): cumulative intraday
        volume relative to the STATIC prior_oi (T-1 settlement), not the
        already-decayed EOI above -- V/OI is specifically a measure of how
        much turnover has occurred against the lagging baseline; dividing by
        EOI would be circular, since EOI already decays *because of* this
        same turnover.

        Zero-prior-OI strikes: volume traded against a zero base is infinite
        turnover (inf); NO volume against a zero base is no turnover at all
        (0.0, not inf -- an untouched, freshly listed strike must not read as
        maximal turnover, or F_state downstream would slam to 1.0 on a strike
        where literally nothing happened).
        """
        no_base = self.prior_oi <= 0
        safe_prior = np.where(no_base, np.nan, self.prior_oi)
        ratio = self._cum_volume / safe_prior
        return np.where(no_base, np.where(self._cum_volume > 0, np.inf, 0.0), ratio)


# =============================================================================
# COMPONENT 2 — Algorithmic Trade Flow Classification Engine
#   Lee-Ready (1991): Quote Rule + Tick Test fallback  (the paper, pg. 4)
#   EMO: Quote Rule + Quote Transition Test fallback   (the paper, pg. 4-5)
#   BVC: bucketed, standardized-price-change classification (pg. 5)
# =============================================================================
class TradeFlowClassifier:
    """Classifies option trade aggressor side and rolls it up into the
    dynamic dealer positioning weight vector w.

    See the CORRECTION (B) note in the module docstring: this class uses the
    theoretically-correct Buy->dealer-short / Sell->dealer-long mapping, not
    the paper's literal (backwards) page-4 prose.
    """

    @staticmethod
    def classify_quote_rule(
        trade_price: FloatArray, bid: FloatArray, ask: FloatArray
    ) -> FloatArray:
        """Quote Rule: sign(P_t - M_t), M_t = (Bid_t + Ask_t)/2. +1 buy, -1 sell, 0 = at midpoint."""
        mid = 0.5 * (np.asarray(bid, dtype=np.float64) + np.asarray(ask, dtype=np.float64))
        return np.sign(np.asarray(trade_price, dtype=np.float64) - mid)

    @staticmethod
    def classify_tick_test(trade_price: FloatArray) -> FloatArray:
        """Tick Test fallback with recursive zero-tick lookback, vectorized via
        forward-fill of the last nonzero tick (pandas ffill is a vectorized
        C-level scan, not a Python per-tick loop)."""
        price = np.asarray(trade_price, dtype=np.float64)
        diffs = np.diff(price, prepend=price[0])
        tick = np.sign(diffs)
        tick[0] = np.nan  # no prior trade to compare against
        tick = np.where(tick == 0, np.nan, tick)
        filled = pd.Series(tick).ffill().fillna(1.0).to_numpy()
        return filled

    def classify_lee_ready(
        self, trade_price: FloatArray, bid: FloatArray, ask: FloatArray
    ) -> FloatArray:
        """Lee-Ready (1991): Quote Rule, falling back to the Tick Test at the midpoint."""
        direction = self.classify_quote_rule(trade_price, bid, ask)
        at_mid = direction == 0
        if np.any(at_mid):
            tick_direction = self.classify_tick_test(trade_price)
            direction = np.where(at_mid, tick_direction, direction)
        return direction

    def classify_emo(
        self, trade_price: FloatArray, bid: FloatArray, ask: FloatArray
    ) -> FloatArray:
        """Ellis-Michaely-O'Hara: Quote Rule, falling back to the Quote
        Transition Test (current quote midpoint vs. prior quote midpoint) at
        the midpoint, instead of the noisier prior-trade-price Tick Test.

        Unlike classify_lee_ready (which forward-fills), this can return 0
        (= unclassified) for midpoint trades under static quotes; a 0 simply
        contributes no signed volume downstream, which is the honest reading
        of an unclassifiable trade rather than a forced coin-flip."""
        bid = np.asarray(bid, dtype=np.float64)
        ask = np.asarray(ask, dtype=np.float64)
        direction = self.classify_quote_rule(trade_price, bid, ask)
        at_mid = direction == 0
        if np.any(at_mid):
            mid = 0.5 * (bid + ask)
            prev_mid = np.roll(mid, 1)
            prev_mid[0] = mid[0]
            transition = np.sign(mid - prev_mid)
            direction = np.where(at_mid, transition, direction)
        return direction

    @staticmethod
    def classify_bvc(
        trade_price: FloatArray, volume: FloatArray, bucket_size: int = 50
    ) -> FloatArray:
        """Bulk Volume Classification (Easley/Lopez de Prado/O'Hara): buckets
        volume and infers the buy proportion from the standardized price
        change across each bucket via the Normal CDF, ignoring individual
        ticks entirely (mitigates timestamp misalignment / fragmentation, per
        the paper's Table, pg. 4-5).

        Returns the buy fraction in [0, 1] per original tick (broadcast back
        from its bucket), 0.5 == balanced.
        """
        price = np.asarray(trade_price, dtype=np.float64)
        n = price.shape[0]
        bucket_id = np.arange(n) // bucket_size
        price_changes = np.diff(price, prepend=price[0])
        sigma = np.std(price_changes)
        sigma = sigma if sigma > 0 else 1e-8

        df = pd.DataFrame({"price": price, "bucket": bucket_id})
        agg = df.groupby("bucket")["price"].agg(["first", "last", "size"])
        delta_p = agg["last"] - agg["first"]
        z = delta_p / (sigma * np.sqrt(agg["size"]))
        buy_fraction_per_bucket = pd.Series(norm.cdf(z), index=agg.index)
        return buy_fraction_per_bucket.reindex(bucket_id).to_numpy()

    @staticmethod
    def dealer_weight_from_flow(
        contract_idx: NDArray[np.intp],
        direction: FloatArray,
        volume: FloatArray,
        is_call: NDArray[np.bool_],
        n_contracts: int,
    ) -> FloatArray:
        """Rolls up classified per-trade direction into the dynamic dealer
        positioning vector w, one entry per contract.

        CORRECTED sign convention (see module docstring, correction B):
        net customer BUYING at a strike means the dealer is net SHORT that
        strike (dealer wrote it) -> w < 0. Net customer SELLING means the
        dealer is net LONG -> w > 0. Strikes with no classified flow this
        tick fall back to the paper's static page-2 convention (calls +1,
        puts -1) rather than an undefined value.
        """
        volume = np.asarray(volume, dtype=np.float64)
        signed_volume = volume * np.asarray(direction, dtype=np.float64)
        net_signed = np.bincount(contract_idx, weights=signed_volume, minlength=n_contracts)
        total_volume = np.bincount(contract_idx, weights=volume, minlength=n_contracts)

        with np.errstate(invalid="ignore", divide="ignore"):
            w_dynamic = -net_signed / total_volume  # note the minus sign: correction (B)
        no_flow = total_volume == 0
        static_fallback = np.where(is_call, 1.0, -1.0)
        w_dynamic = np.where(no_flow, static_fallback, w_dynamic)
        return np.clip(w_dynamic, -1.0, 1.0)


# =============================================================================
# COMPONENT 3 — Microsecond Newton-Raphson Gamma Flip Solver
#   F(S)  = sum_k Gamma_k(S) * EOI_k * 100 * S^2 * 0.01 * w_k
#   F'(S) = sum_k [S^2 * Speed_k(S) + 2*S * Gamma_k(S)] * EOI_k * 0.01 * w_k
#   (the paper, pg. 5-8)
# =============================================================================
class DynamicGammaFlipEngine:
    """Vectorized Black-Scholes Gamma/Speed engine with a Newton-Raphson
    gamma-flip (zero net $GEX) root solver, falling back to brentq.

    T is passed per-call (not fixed at construction) so the same engine
    instance can be re-solved every tick as SessionClock advances the live
    time-to-expiry (Component 4) without reallocating the contract arrays.
    """

    CONTRACT_MULTIPLIER: float = 100.0

    def __init__(
        self,
        strikes: FloatArray,
        eoi: FloatArray,
        iv: FloatArray,
        r: float,
        q: float,
        w: FloatArray,
        is_call: NDArray[np.bool_],
    ) -> None:
        """
        Args:
            strikes: strike price per contract.
            eoi: Effective Open Interest per contract (Component 1 output).
            iv: implied volatility per contract.
            r: risk-free rate.
            q: continuous dividend yield.
            w: dealer positioning weight per contract (Component 2 output).
            is_call: True for calls, False for puts, per contract. Added in
                Phase 2 — Gamma/Speed are identical for calls and puts so
                Phase 1 never needed this, but Charm is not (see _charm), so
                it's now a required field. No Phase 1 method's math changes.
        """
        self.K: FloatArray = np.asarray(strikes, dtype=np.float64)
        self.EOI: FloatArray = np.asarray(eoi, dtype=np.float64)
        self.IV: FloatArray = np.asarray(iv, dtype=np.float64)
        self.r: float = float(r)
        self.q: float = float(q)
        self.w: FloatArray = np.asarray(w, dtype=np.float64)
        self.is_call: NDArray[np.bool_] = np.asarray(is_call, dtype=bool)

    def _d1(self, S: float, T: FloatArray) -> FloatArray:
        T = np.maximum(T, EPS_T)  # edge case: T -> 0 near expiration
        return (
            np.log(S / self.K) + (self.r - self.q + 0.5 * self.IV**2) * T
        ) / (self.IV * np.sqrt(T))

    def _gamma(self, S: float, T: FloatArray) -> FloatArray:
        """Black-Scholes Gamma with continuous dividend yield q (the paper, pg. 2)."""
        d1 = self._d1(S, T)
        T = np.maximum(T, EPS_T)
        return np.exp(-self.q * T) * norm.pdf(d1) / (S * self.IV * np.sqrt(T))

    def _speed(self, S: float, T: FloatArray) -> FloatArray:
        """Speed = dGamma/dS.

        CORRECTED coefficient vs. the paper (correction A, see module
        docstring): the correct closed form is
            Speed = -(Gamma/S) * (d1/(sigma*sqrt(T)) + 1)
        not "+2" as printed on pg. 6. Verified by differentiating the Gamma
        formula above directly and by central-difference numerical check.
        """
        gamma = self._gamma(S, T)
        d1 = self._d1(S, T)
        T = np.maximum(T, EPS_T)
        return -(gamma / S) * (d1 / (self.IV * np.sqrt(T)) + 1.0)

    def objective_function(self, S: float, T: FloatArray) -> float:
        """Aggregate Net $GEX at hypothetical spot S: F(S) = sum_k Gamma_k * EOI_k * 100 * S^2 * 0.01 * w_k."""
        gamma = self._gamma(S, T)
        gex = gamma * self.EOI * self.CONTRACT_MULTIPLIER * (S**2) * 0.01 * self.w
        return float(np.sum(gex))

    def jacobian(self, S: float, T: FloatArray) -> float:
        """Analytical F'(S), via the product rule d(Gamma*S^2)/dS = S^2*Speed + 2*S*Gamma."""
        gamma = self._gamma(S, T)
        speed = self._speed(S, T)
        deriv = (
            ((S**2) * speed + 2.0 * S * gamma)
            * self.EOI
            * self.CONTRACT_MULTIPLIER
            * 0.01
            * self.w
        )
        return float(np.sum(deriv))

    def calculate_gamma_flip(
        self,
        current_spot: float,
        T: FloatArray,
        search_band: float = 0.20,
        tol: float = 1e-3,
        maxiter: int = 50,
    ) -> Optional[float]:
        """Locates S* where Net $GEX crosses zero.

        Newton-Raphson with the exact analytical Jacobian for quadratic
        convergence; falls back to bracketed Brent's method within
        +/- search_band of current_spot if Newton fails to converge or lands
        outside a sane range (extreme local convexity near expiry / a Speed
        sign-flip past a Call Wall, per the paper's Edge Cases section, pg. 9-10).
        """
        T = np.asarray(T, dtype=np.float64)
        # Gamma-dead chain guard (audit fix 2026-07-04): when every contract's
        # gamma has numerically vanished (e.g. all strikes far OTM minutes
        # from expiry, n(d1) underflows to 0), F(x0) == 0.0 EXACTLY and
        # scipy's newton returns x0 itself as a "root" -- reporting the flip
        # at the current spot when in truth there is no flip to find. No
        # gamma mass, no flip.
        gamma_mass = float(np.sum(np.abs(
            self._gamma(current_spot, T) * self.EOI * self.w
        ))) * self.CONTRACT_MULTIPLIER * current_spot**2 * 0.01
        if gamma_mass < 1e-9:
            return None
        try:
            flip = newton(
                func=self.objective_function,
                x0=current_spot,
                fprime=self.jacobian,
                args=(T,),
                tol=tol,
                maxiter=maxiter,
            )
            if not np.isfinite(flip) or flip <= 0:
                raise RuntimeError("Newton-Raphson returned a non-physical root")
            return float(flip)
        except RuntimeError:
            lo, hi = current_spot * (1 - search_band), current_spot * (1 + search_band)
            try:
                return float(brentq(self.objective_function, lo, hi, args=(T,)))
            except ValueError:
                # objective doesn't change sign inside the search band -> no
                # flip resolvable at this snapshot (e.g. deep one-sided regime)
                return None

    # -- Phase 2 additions: Net Vanna (VEX) / Net Charm (CHEX) --------------
    # Reference: "Second-Order Greeks Stability Analysis.pdf" ("the Phase 2
    # paper"). See the PHASE 2 banner comment below (above _selftest_phase2)
    # for the full verification writeup and the two corrections (C, D)
    # applied here. In short: Vanna_C == Vanna_P and the Charm PDEs below were
    # independently re-derived term-by-term and check out exactly as printed
    # in the paper; what needed fixing was the dealer-exposure SIGN
    # convention and CHEX's time units, not these PDEs themselves.

    def _d2(self, S: float, T: FloatArray) -> FloatArray:
        d1 = self._d1(S, T)
        T = np.maximum(T, EPS_T)
        return d1 - self.IV * np.sqrt(T)

    def _vanna(self, S: float, T: FloatArray) -> FloatArray:
        """Vanna = dDelta/dsigma = -exp(-qT) * n(d1) * d2 / sigma.

        Identical for calls and puts (put-call parity: Delta_P = Delta_C -
        exp(-qT), and the parity gap doesn't depend on sigma). Verified by
        direct term-by-term differentiation of d1 (d(d1)/dsigma = -d2/sigma)
        and cross-checked against this project's greeks.py vanna formula at
        q=0, where the two are identical.
        """
        d1 = self._d1(S, T)
        d2 = self._d2(S, T)
        T = np.maximum(T, EPS_T)
        return -np.exp(-self.q * T) * norm.pdf(d1) * d2 / self.IV

    def _charm(self, S: float, T: FloatArray) -> FloatArray:
        """Charm = dDelta/dt = -dDelta/dtau. Differs for calls vs. puts only
        through the leading q*exp(-qT)*N(d1) term (puts use N(d1)-1).

        Verified by direct differentiation (d(d1)/dtau = (r-q)/(sigma*sqrt(T))
        - d2/(2T)) and cross-checked against greeks.py's charm formula at
        q=0, where calls and puts collapse to the same expression.
        """
        d1 = self._d1(S, T)
        d2 = self._d2(S, T)
        T = np.maximum(T, EPS_T)
        disc = np.exp(-self.q * T)
        drift = disc * norm.pdf(d1) * (
            2.0 * (self.r - self.q) * T - d2 * self.IV * np.sqrt(T)
        ) / (2.0 * T * self.IV * np.sqrt(T))
        leading = disc * np.where(self.is_call, norm.cdf(d1), norm.cdf(d1) - 1.0)
        return self.q * leading - drift

    def net_vex(self, S: float, T: FloatArray) -> float:
        """Net dollar Vanna Exposure (VEX): dollar-flow required per 1% IV shift.

        VEX = Vanna * EOI * contract_multiplier * S * 0.01 * w — reuses the
        exact same corrected dealer-weight vector w as Net $GEX (Correction C:
        the Phase 2 paper's own VEX_K formula, page 6, has calls negative and
        puts positive, i.e. backwards from this project's established
        convention; see the PHASE 2 banner below for the full verification).
        """
        vanna = self._vanna(S, T)
        vex = vanna * self.EOI * self.CONTRACT_MULTIPLIER * S * 0.01 * self.w
        return float(np.sum(vex))

    def net_chex(self, S: float, T: FloatArray) -> float:
        """Net dollar Charm Exposure (CHEX): dollar-flow required per
        1-calendar-day passage of time.

        CHEX = Charm * EOI * contract_multiplier * S * w / 365 — the /365 is
        Correction D (see PHASE 2 banner below): Charm as derived is a
        per-YEAR rate (tau is in years throughout), but the paper's own
        schema calls CHEX a per-DAY dollar flow; its literal formula (page 7)
        has no such conversion and would overstate daily CHEX ~365x.
        """
        charm = self._charm(S, T)
        chex = charm * self.EOI * self.CONTRACT_MULTIPLIER * S * self.w / 365.0
        return float(np.sum(chex))


# =============================================================================
# COMPONENT 4 — Dynamic Temporal Intraday Decay (Color Greek Adjustment)
#   (the paper, pg. 8, "Managing Intraday Decay via the Color Greek")
# =============================================================================
@dataclass
class SessionClock:
    """Tracks live session progression and converts it into a continuous
    time-to-expiry-in-years array, so Color (dGamma/dT) naturally steepens the
    Gamma profile as the close approaches — no manual "decrement DTE" step
    needed, since real datetime subtraction already captures the live
    floating-point fraction of the day remaining.
    """

    session_close_time: dt_time = dt_time(16, 0)

    def time_to_expiry_years(
        self, now: datetime, expiry_dates: Union[date, NDArray[np.datetime64]]
    ) -> FloatArray:
        """Continuous T (years) per contract from the live clock `now` to each
        contract's expiration at `session_close_time`.

        Args:
            now: current wall-clock timestamp (tz-naive, exchange-local).
            expiry_dates: a single date or a per-contract array of expiry dates.
        """
        if isinstance(expiry_dates, date):
            expiry_dt = np.datetime64(datetime.combine(expiry_dates, self.session_close_time))
        else:
            dates = np.asarray(expiry_dates, dtype="datetime64[D]")
            close_offset = np.timedelta64(
                self.session_close_time.hour * 3600 + self.session_close_time.minute * 60, "s"
            )
            expiry_dt = dates.astype("datetime64[s]") + close_offset

        now64 = np.datetime64(now.replace(microsecond=0))
        seconds_remaining = (expiry_dt - now64) / np.timedelta64(1, "s")
        T = seconds_remaining.astype(np.float64) / SECONDS_PER_YEAR
        return np.maximum(T, EPS_T)

    def charm_weight_multiplier(
        self, now: datetime, session_close: datetime, growth: float = 2.0
    ) -> float:
        """Dynamic Clock-Weighting helper (Phase 2, "Second-Order Greeks
        Stability Analysis.pdf", pg. 8-9): returns >=1.0, scaling
        exponentially once the session enters its final 60 minutes, to
        account for the inverse-sigmoid acceleration of 0DTE Charm decay.
        Used by WallStabilityEngine.dynamic_weights to boost omega_3.
        """
        minutes_to_close = max((session_close - now).total_seconds() / 60.0, 0.0)
        if minutes_to_close >= 60.0:
            return 1.0
        return float(np.exp(growth * (60.0 - minutes_to_close) / 60.0))


# =============================================================================
# PHASE 2 — Volatility Surface Calibration, Second-Order Greeks (Vanna/Charm),
# and the Wall Stability Score (WSS). Builds directly on Phase 1 above; no
# Phase 1 formula or method was changed except DynamicGammaFlipEngine's
# constructor, which gained one required field (is_call — Charm needs it,
# Gamma/Speed never did).
#
# Reference: "Second-Order Greeks Stability Analysis.pdf" (uploaded,
# hereafter "the Phase 2 paper").
#
# VERIFIED CORRECT, NO CHANGE NEEDED (re-derived term-by-term, not just
# transcribed):
#   - Vanna_C = Vanna_P = -exp(-qT) n(d1) d2/sigma. Confirmed
#     d(d1)/dsigma = -d2/sigma by direct differentiation, and confirmed
#     Vanna_P = Vanna_C via put-call parity (Delta_P = Delta_C - exp(-qT), and
#     the gap term doesn't depend on sigma). Matches greeks.py's existing
#     vanna formula at q=0.
#   - Charm_C / Charm_P (pg. 4-5), including the paper's own algebraic
#     simplification d(d1)/dtau = (r-q)/(sigma*sqrt(T)) - d2/(2T) -- confirmed
#     by direct differentiation of d1 w.r.t. tau. Matches greeks.py's existing
#     charm formula at q=0 (where calls and puts collapse to one expression).
#   - F_VEX = tanh(VEX_net * (-delta_sigma) / N) (pg. 8): the (-delta_sigma)
#     sign is correct, verified against the paper's own "Vanna Bid" (IV crush
#     -> dealer buying) and "Vanna Ask" (IV expansion -> dealer selling)
#     walkthroughs (pg. 3-4) -- PROVIDED it is fed the corrected Net VEX
#     (Correction C below). Reproduced in _selftest_phase2.
#
# CORRECTIONS APPLIED VS. THE PHASE 2 PAPER:
#
#   (C) Dealer-exposure sign convention, pg. 6-7 (GEX_K / VEX_K / CHEX_K).
#       The paper's formulas are all of the form:
#           X_K = ( -LiveOI_C * X_C + LiveOI_P * X_P ) * scale
#       i.e. CALLS negative, PUTS positive. This is backwards:
#         - It contradicts Phase 1's own established w_C=+1/w_P=-1 convention
#           (Correction B in the Phase 1 banner above), which in turn matches
#           this project's live box (gex.py: `sign = +1 for calls, -1 for
#           puts`).
#         - It contradicts the Phase 2 paper's OWN worked example. Pages 3-4
#           spend two sections deriving that a dealer short a large OTM-put
#           book (the canonical Put Wall book) must show NET POSITIVE Vanna
#           exposure, so that an IV crush triggers the "Vanna Bid" (dealers
#           forced to buy). Plugging a pure short-put book (LiveOI_C=0) into
#           the paper's own literal VEX_K formula gives
#           VEX = +LiveOI_P * Vanna_P * S, and for OTM puts Vanna_P < 0 (see
#           _vanna docstring / the sign of d2 for K<S), so VEX_K comes out
#           NEGATIVE — the opposite of what the paper's own Vanna Bid
#           narrative requires. With the corrected sign (+Call, -Put) the
#           same book gives VEX_K = -LiveOI_P * Vanna_P * S > 0, matching.
#       This module reuses the exact same per-contract dealer weight vector w
#       from Component 2 for Vanna and Charm, exactly as Phase 1 already does
#       for Gamma (net_vex / net_chex above both multiply by self.w). Verified
#       in _selftest_phase2 by confirming a synthetic short-put book produces
#       positive Net VEX, and that an IV expansion then flips F_VEX negative
#       (Vanna Ask / bearish) while an IV crush keeps it positive (Vanna Bid
#       / bullish), matching the paper's pg. 3-4 walkthroughs exactly.
#
#   (D) CHEX time units, pg. 7. The paper's CHEX_K formula has no time-unit
#       conversion, but Charm (as derived, both here and in the paper) is a
#       PER-YEAR rate (tau is in years throughout the PDE). The paper's own
#       schema explicitly defines CHEX as dollar-flow "for a 1-day temporal
#       passage" -- taking the formula literally overstates daily CHEX by
#       ~365x. This implementation divides by 365.0 (see net_chex above),
#       matching both the stated "per day" spec and greeks.py's existing
#       `charm / 365.0` convention.
# =============================================================================
def _lower_convex_hull(x: FloatArray, y: FloatArray) -> Tuple[FloatArray, FloatArray]:
    """Greatest convex minorant (lower convex hull) of the points (x, y),
    x assumed sorted ascending. Monotone-chain, O(n).

    Used to enforce d^2(price)/dK^2 >= 0 (no butterfly arbitrage) on a
    smoothed call-price curve: any point strictly above the returned hull is
    an interior (non-convex-boundary) point and gets replaced, on
    reinterpolation, by the hull's connecting line -- the hull is convex by
    construction, so this is a hard geometric guarantee, not a statistical
    one. n here is the strike count for one expiry slice (tens, not
    thousands), so this single small sequential pass is not held to the
    per-tick vectorization bar the rest of the engine is.
    """
    n = x.shape[0]
    hull_idx: list[int] = []
    for i in range(n):
        while len(hull_idx) >= 2:
            o, a = hull_idx[-2], hull_idx[-1]
            cross = (x[a] - x[o]) * (y[i] - y[o]) - (y[a] - y[o]) * (x[i] - x[o])
            if cross <= 0:  # a is not on the convex boundary from below -> drop it
                hull_idx.pop()
            else:
                break
        hull_idx.append(i)
    idx = np.array(hull_idx, dtype=np.intp)
    return x[idx], y[idx]


@dataclass
class VolatilitySurfaceCalibrator:
    """Component 5 — Arbitrage-Free Implied Volatility Surface Calibration
    (the Phase 2 paper, pg. 2-3), for one expiry slice at a time.

    Pipeline, exactly as described in the paper: (1) vectorized bisection IV
    solve from live mid-quotes, (2) convert to total variance w(K) = iv^2*T,
    (3) cubic-spline smoothing across w(K) to remove bid-ask microstructure
    noise, (4) convert the smoothed surface to call prices and project onto
    the lower convex hull in K -- a hard, by-construction guarantee that
    d^2(price)/dK^2 >= 0 (no butterfly arbitrage), (5) invert back to a
    smoothed IV per input strike.

    Scope note: this calibrates the smile within a single expiry (butterfly-
    arbitrage-free in K). Cross-expiry calendar-spread arbitrage is handled
    by the separate CalendarArbitrageEngine below, which consumes this
    class's per-expiry output.
    """

    r: float
    q: float

    def _bs_call_price(self, S: float, K: FloatArray, T: FloatArray, iv: FloatArray) -> FloatArray:
        T = np.maximum(np.asarray(T, dtype=np.float64), EPS_T)
        iv = np.maximum(np.asarray(iv, dtype=np.float64), EPS_T)
        d1 = (np.log(S / K) + (self.r - self.q + 0.5 * iv**2) * T) / (iv * np.sqrt(T))
        d2 = d1 - iv * np.sqrt(T)
        return S * np.exp(-self.q * T) * norm.cdf(d1) - K * np.exp(-self.r * T) * norm.cdf(d2)

    def _bs_price(
        self, S: float, K: FloatArray, T: FloatArray, iv: FloatArray, is_call: NDArray[np.bool_]
    ) -> FloatArray:
        call = self._bs_call_price(S, K, T, iv)
        put = call - S * np.exp(-self.q * T) + K * np.exp(-self.r * T)  # put-call parity
        return np.where(is_call, call, put)

    def _implied_vol(
        self,
        price: FloatArray,
        S: float,
        K: FloatArray,
        T: FloatArray,
        is_call: NDArray[np.bool_],
        lo: float = 1e-4,
        hi: float = 5.0,
        iters: int = 60,
    ) -> FloatArray:
        """Vectorized bisection IV solve from mid-prices (the paper's step 1),
        mirroring this project's existing greeks.py implied_vol solver --
        including its validity rules (audit fix 2026-07-04; the first version
        skipped them): non-positive or below-intrinsic quotes, and solutions
        pinned to a bisection bound, return NaN instead of silently pinning
        to iv=1e-4 and poisoning the surface. Callers interpolate NaNs from
        neighboring strikes.
        """
        price = np.asarray(price, dtype=np.float64)
        K = np.asarray(K, dtype=np.float64)
        T = np.asarray(T, dtype=np.float64)
        disc_r = np.exp(-self.r * T)
        disc_q = np.exp(-self.q * T)
        intrinsic = np.where(
            is_call,
            np.maximum(S * disc_q - K * disc_r, 0.0),
            np.maximum(K * disc_r - S * disc_q, 0.0),
        )
        valid = (price > 0.0) & (price >= intrinsic - 1e-6) & (T > 0)
        a = np.full_like(price, lo)
        b = np.full_like(price, hi)
        for _ in range(iters):
            m = 0.5 * (a + b)
            pm = self._bs_price(S, K, T, m, is_call)
            too_low = pm < price
            a = np.where(too_low, m, a)
            b = np.where(too_low, b, m)
        iv = 0.5 * (a + b)
        # pinned-to-bound solutions carry no real signal (greeks.py rule)
        iv = np.where((iv <= lo * 1.5) | (iv >= hi * 0.999), np.nan, iv)
        return np.where(valid, iv, np.nan)

    def calibrate(
        self,
        strikes: FloatArray,
        mid_price: FloatArray,
        T: float,
        S: float,
        is_call: NDArray[np.bool_],
    ) -> FloatArray:
        """Runs the full pipeline; returns smoothed, arbitrage-free IV per
        input row (same order/length as `strikes` -- a call and a put quoted
        at the same K both receive that strike's calibrated IV).

        AUDIT FIX (2026-07-04) -- three defects in the first version:
          1. It called scipy's CubicSpline (an INTERPOLATOR) and evaluated it
             at its own knots, returning the inputs bit-for-bit -- the
             claimed "smoothing" was a no-op, so the convex hull threaded the
             lower envelope of the raw noise (measured IV bias vs truth:
             about -0.07 on a synthetic smile with 3-cent mid noise). Now
             uses a genuine least-squares FITPACK smoothing spline on total
             variance, with the smoothing factor s = n * sigma_hat^2
             estimated from second differences (Var(d2 w) = 6 sigma^2 for
             iid noise) -- degenerates gracefully toward interpolation on
             clean data.
          2. It crashed (ValueError: x must be strictly increasing) on any
             real chain, because live chains quote a call AND a put at the
             same strike. Duplicate strikes are now collapsed to one variance
             point each (mean of the solvable quotes at that K).
          3. Unsolvable quotes pinned to iv=1e-4 instead of being masked
             (see _implied_vol); NaNs are now interpolated across strikes.

        KNOWN LIMIT (measured, not fixable at this layer): deep-wing strikes
        whose option price is comparable to the quote noise are statistically
        unidentifiable -- masking kills the downward outliers, so the
        surviving quotes bias wing IV HIGH (about +0.07 on a synthetic smile
        whose 3-cent noise exceeds the far wings' entire premium). The ATM
        band is accurate (regression below: bias ~+0.01 under 2-cent noise).
        When wing fidelity matters, restrict inputs to a near-the-money band
        upstream -- exactly greeks.py's existing --band 0.12 NTM design.
        """
        strikes = np.asarray(strikes, dtype=np.float64)
        mid_price = np.asarray(mid_price, dtype=np.float64)
        is_call = np.asarray(is_call, dtype=bool)
        T_arr = np.full(strikes.shape[0], T, dtype=np.float64)

        raw_iv = self._implied_vol(mid_price, S, strikes, T_arr, is_call)
        w_raw = raw_iv**2 * T

        # one variance point per unique strike (call+put at the same K)
        K_u, inv = np.unique(strikes, return_inverse=True)
        solvable = ~np.isnan(w_raw)
        if not np.any(solvable):
            raise ValueError("surface calibration: no solvable quotes in this expiry slice")
        sums = np.bincount(inv[solvable], weights=w_raw[solvable], minlength=K_u.size)
        cnts = np.bincount(inv[solvable], minlength=K_u.size)
        with np.errstate(invalid="ignore"):
            w_u = sums / cnts
        w_u = pd.Series(w_u).interpolate(limit_direction="both").to_numpy()

        # genuine least-squares smoothing across total variance
        if K_u.size >= 4:
            d2w = np.diff(w_u, 2)
            noise_var = float(np.var(d2w)) / 6.0 if d2w.size >= 2 else 0.0
            tck = splrep(K_u, w_u, k=3, s=K_u.size * noise_var)
            w_smooth = splev(K_u, tck)
        else:
            w_smooth = w_u
        w_smooth = np.maximum(w_smooth, EPS_T)
        iv_smooth = np.sqrt(w_smooth / max(T, EPS_T))

        T_u = np.full(K_u.size, T, dtype=np.float64)
        call_smooth = self._bs_call_price(S, K_u, T_u, iv_smooth)
        Kx, Cy = _lower_convex_hull(K_u, call_smooth)
        call_hull = np.interp(K_u, Kx, Cy)

        iv_hull = self._implied_vol(call_hull, S, K_u, T_u, np.ones(K_u.size, dtype=bool))
        iv_hull = pd.Series(iv_hull).interpolate(limit_direction="both").to_numpy()
        return iv_hull[inv]


# =============================================================================
# Cross-expiry calendar-spread arbitrage checking/enforcement -- closes the
# scope gap flagged in VolatilitySurfaceCalibrator above. Not from either
# uploaded PDF; this is standard quant literature (Gatheral, "The Volatility
# Surface": total variance must be non-decreasing in tau at fixed forward
# log-moneyness), so there's no "corrected against a flawed source" story
# here -- it's verified directly against its own no-arbitrage definition in
# _selftest_phase2 (inject a known violation, confirm detection, confirm the
# fix restores monotonicity).
# =============================================================================
@dataclass
class CalendarArbitrageEngine:
    """Checks/enforces that total variance w(k, tau) = sigma_IV(k,tau)^2 * tau
    is non-decreasing in tau at fixed forward log-moneyness k = ln(K/F_tau),
    F_tau = S*exp((r-q)*tau) -- the standard no-calendar-arbitrage condition.

    Comparing at fixed log-moneyness (not fixed absolute strike) matters
    because the same strike K corresponds to different moneyness at
    different expiries once the forward has drifted; comparing raw strikes
    directly would flag false violations (or miss real ones) whenever
    (r-q)*tau is non-negligible between the two expiries.

    A violation -- w falling as tau increases, at some k -- means a calendar
    spread (long the far leg, short the near leg, matched moneyness) is a
    risk-free arbitrage: the near leg is worth more variance than the far
    leg it's supposed to be a fraction of.
    """

    r: float
    q: float
    n_moneyness_grid: int = 41

    def _forward(self, S: float, T: float) -> float:
        return S * np.exp((self.r - self.q) * T)

    def _w_on_grid(
        self, strikes: FloatArray, iv: FloatArray, T: float, S: float, k_grid: FloatArray
    ) -> FloatArray:
        """Interpolates one expiry's smoothed IV, re-expressed as total
        variance, onto a common log-moneyness grid (flat-extrapolated
        outside its own observed range, per np.interp's default)."""
        F = self._forward(S, T)
        k_obs = np.log(np.asarray(strikes, dtype=np.float64) / F)
        order = np.argsort(k_obs)
        w_obs = np.asarray(iv, dtype=np.float64)[order] ** 2 * T
        return np.interp(k_grid, k_obs[order], w_obs)

    def _common_grid(self, expiries: list, S: float) -> FloatArray:
        # intersection of each expiry's own observed moneyness range, so no
        # expiry needs extrapolation at the grid points used for comparison
        k_lo = max(np.log(strikes.min() / self._forward(S, T)) for T, strikes, _ in expiries)
        k_hi = min(np.log(strikes.max() / self._forward(S, T)) for T, strikes, _ in expiries)
        return np.linspace(k_lo, k_hi, self.n_moneyness_grid)

    def check(
        self, expiries: list, S: float
    ) -> list:
        """expiries: list of (T, strikes, smoothed_iv) tuples, any order.
        Returns [(T_near, T_far, k), ...] for each adjacent expiry pair /
        moneyness grid point where total variance decreases; empty = clean.
        """
        ordered = sorted(expiries, key=lambda e: e[0])
        Ts = [e[0] for e in ordered]
        k_grid = self._common_grid(ordered, S)
        w_grid = np.stack([self._w_on_grid(strikes, iv, T, S, k_grid) for T, strikes, iv in ordered])

        violations = []
        for i in range(1, len(ordered)):
            bad = w_grid[i] < w_grid[i - 1] - 1e-12
            for j in np.where(bad)[0]:
                violations.append((Ts[i - 1], Ts[i], float(k_grid[j])))
        return violations

    def enforce(self, expiries: list, S: float) -> dict:
        """Projects the term structure onto the calendar-arbitrage-free cone:
        a pointwise running max of total variance along tau, per moneyness
        grid point (the direct isotonic-in-tau analogue of Component 5's
        convex-hull projection in strike). Returns {T: corrected_iv_array},
        corrected IV expressed back on each expiry's ORIGINAL strikes.
        """
        ordered = sorted(expiries, key=lambda e: e[0])
        k_grid = self._common_grid(ordered, S)
        w_grid = np.stack([self._w_on_grid(strikes, iv, T, S, k_grid) for T, strikes, iv in ordered])
        w_fixed = np.maximum.accumulate(w_grid, axis=0)  # running max as tau increases

        out = {}
        for row, (T, strikes, _iv) in zip(w_fixed, ordered):
            F = self._forward(S, T)
            k_obs = np.log(np.asarray(strikes, dtype=np.float64) / F)
            w_at_strikes = np.interp(k_obs, k_grid, row)  # flat outside the common range
            out[T] = np.sqrt(np.maximum(w_at_strikes, EPS_T) / max(T, EPS_T))
        return out


@dataclass
class WallStabilityEngine:
    """Component: Wall Stability Score (WSS) Normalization Module (the Phase
    2 paper, pg. 8-9). Combines I_GEX (logistic distance-to-flip), F_VEX, and
    F_CHEX (tanh-normalized dollar flows, from DynamicGammaFlipEngine.net_vex
    / net_chex above) into a single, dynamically-weighted score.

    Note on the +/-100 bound: the paper specifies WSS in [-100, 100] and also
    asks for omega_3 to scale exponentially in the final hour. Those two
    requirements only stay compatible if the weights are renormalized to sum
    to 100 after the exponential boost (dynamic_weights does this) --
    otherwise the boosted omega_3 alone would push WSS past the paper's own
    stated range. This renormalization isn't in the paper; it's required for
    the paper's own bound to hold given its own weighting rule.
    """

    base_weights: Tuple[float, float, float] = (40.0, 30.0, 30.0)  # (w1, w2, w3), sums to 100
    logistic_k: float = 6.0
    final_hour_growth: float = 2.0

    def i_gex(self, spot: float, gamma_flip: Optional[float]) -> float:
        """Logistic normalization of spot's distance from the zero-gamma flip
        line, in [-1, 1]. Positive => spot above the flip (positive-gamma,
        stabilizing regime).

        (Uses scipy.special.expit per the paper's explicit "logistic
        normalization" wording, even though 2*logistic(x)-1 == tanh(x/2) --
        i.e. this is the same function family as F_VEX/F_CHEX below,
        reparametrized, not a materially different normalization.)
        """
        if gamma_flip is None or gamma_flip <= 0:
            return 0.0
        x = self.logistic_k * (spot - gamma_flip) / gamma_flip
        return float(2.0 * expit(x) - 1.0)

    @staticmethod
    def f_vex(net_vex: float, delta_iv: float, standardizing_scalar: float) -> float:
        """See Correction C above: sign matches the paper's own Vanna
        Bid / Vanna Ask walkthroughs once net_vex uses the corrected dealer
        sign convention (DynamicGammaFlipEngine.net_vex already applies it)."""
        n = standardizing_scalar if standardizing_scalar > 0 else 1e-8
        return float(np.tanh(net_vex * (-delta_iv) / n))

    @staticmethod
    def f_chex(net_chex_at_wall: float, delta_t_days: float, standardizing_scalar: float) -> float:
        n = standardizing_scalar if standardizing_scalar > 0 else 1e-8
        return float(np.tanh(net_chex_at_wall * delta_t_days / n))

    def dynamic_weights(self, now: datetime, session_close: datetime) -> Tuple[float, float, float]:
        """Dynamic Clock-Weighting: omega_3 (Charm) scales exponentially in
        the final 60 minutes (via SessionClock.charm_weight_multiplier), then
        all three weights are renormalized to sum to 100 (see class
        docstring)."""
        w1, w2, w3 = self.base_weights
        w3 = w3 * SessionClock().charm_weight_multiplier(now, session_close, self.final_hour_growth)
        total = w1 + w2 + w3
        scale = 100.0 / total if total > 0 else 1.0
        return w1 * scale, w2 * scale, w3 * scale

    def score(
        self,
        spot: float,
        gamma_flip: Optional[float],
        net_vex: float,
        delta_iv: float,
        net_chex_at_wall: float,
        delta_t_days: float,
        standardizing_scalar: float,
        now: datetime,
        session_close: datetime,
    ) -> Tuple[float, str]:
        """WSS = w1*I_GEX + w2*F_VEX + w3*F_CHEX, mapped to the paper's four
        categorical flags (pg. 4 / 9)."""
        w1, w2, w3 = self.dynamic_weights(now, session_close)
        i = self.i_gex(spot, gamma_flip)
        fv = self.f_vex(net_vex, delta_iv, standardizing_scalar)
        fc = self.f_chex(net_chex_at_wall, delta_t_days, standardizing_scalar)
        wss = w1 * i + w2 * fv + w3 * fc
        return float(wss), self.classify(wss)

    @staticmethod
    def classify(wss: float) -> str:
        """Maps the continuous WSS onto the paper's four bins (pg. 4/9):
        HARD FLOOR [+50,+100], YIELDING [0,+49], FRACTURED [-1,-49],
        TRAPDOOR [-50,-100]. Implemented as contiguous real-valued bins
        (>=50 / >=0 / >=-50 / else) since the paper's integer-style gaps
        (e.g. between -1 and 0) don't partition a continuous score without a
        gap; this is the natural continuous reading of the same four bins."""
        if wss >= 50.0:
            return "HARD FLOOR"
        if wss >= 0.0:
            return "YIELDING"
        if wss >= -50.0:
            return "FRACTURED"
        return "TRAPDOOR"


# =============================================================================
# PHASE 3 — Structural Validation Layer and Cascade Breakdown Probability
# Engine. Builds on Phase 1 (ChainState.v_oi_ratio above is the only existing-
# code touch: one new read-only property, nothing else changed) and Phase 2
# (net_vex/net_chex, gamma_flip). No existing method's behavior changes.
#
# Reference: "Options Market Microstructure Research.pdf" (uploaded,
# hereafter "the Phase 3 paper").
#
# VERIFIED CORRECT, NO CHANGE NEEDED:
#   - G_state, T_state asymptotic/clip formulas (pg. 8-9): both are monotonic,
#     continuous at their branch boundary (spot==flip and VIX/VXV==1.0
#     respectively give 0 from either side), and bounded in [0,1] by
#     construction (1-exp(-x) for x>=0, and min(1.0, ...) for x>=0). Confirmed
#     directly, not just transcribed.
#   - P(C) weights (0.2/0.3/0.3/0.2) sum to exactly 1.0, so with each state
#     bounded in [0,1], P(C) itself is bounded in [0,1] by construction --
#     no extra clip needed at the top level.
#
# GAP FILLED (not a bug -- the paper's own definition is simply incomplete):
#   F_state (pg. 9) only defines two branches: V/OI<1.0 -> 0, and V/OI>1.0
#   WITH bid-side-dominant flow -> the scaled formula. It never says what
#   happens when V/OI>1.0 but flow is ASK-side dominant (accumulation, e.g.
#   the Beta-2/Alpha-1 rows' "Put Accumulation" case) -- left literally
#   undefined. Given the rest of the paper's own logic (high V/OI from
#   accumulation reinforces a wall; only bid-side unwinding erodes it), the
#   sensible completion is F_state=0 whenever flow isn't bid-side-dominant,
#   regardless of V/OI's magnitude. Implemented that way and noted here so
#   it reads as a deliberate closure of a gap, not a silent guess.
#
# NOTED, NOT ACTED ON: a genuine cross-document sign inconsistency. This
# paper defines Charm = -dDelta/dt (pg. 5-6), the opposite sign of Phase 2's
# paper (Charm = dDelta/dt = -dDelta/dtau) and of this project's own,
# already-verified DynamicGammaFlipEngine._charm / greeks.py. Charm/CHEX
# don't appear anywhere in P(C) or its four states, so this doesn't block
# anything Phase 3 actually asks for -- flagged for the record rather than
# silently ignored, and the existing (already cross-verified, in-production)
# Phase 2 convention is left untouched rather than "fixed" against a source
# that itself conflicts with the rest of the codebase.
#
# DESIGN CHOICE: Rule classification (pg. 7-8) is driven by the paper's own
# stated P(C) ranges per rule (<5% / ~30% / ~50% / >85%) crossed with the
# GEX-regime sign (which the table itself uses as the primary split across
# all four rows), rather than requiring an exact simultaneous match on every
# literal table cell (V/OI<0.5 AND VIX/VXV<0.95 AND ...). A live reading will
# essentially never land on all of a row's illustrative thresholds at once;
# threshold-cascaded classification on the derived P(C) (mirroring
# WallStabilityEngine.classify in Phase 2) always produces an answer instead
# of falling through undefined. The >0.75 hard-pivot threshold is used
# exactly as the prompt specifies, not the table's illustrative ">85%".
# =============================================================================
def bid_side_volume_fraction(
    contract_idx: NDArray[np.intp],
    direction: FloatArray,
    volume: FloatArray,
    n_contracts: int,
) -> FloatArray:
    """Fraction of a tick batch's volume executed at/below the bid (seller-
    initiated, direction==-1 from Component 2's classifiers) per contract, in
    [0, 1]. Contracts with no flow this batch return 0.5 (neutral/unknown,
    not a spurious 0 or 1 that would falsely pass or fail the >70% test).
    """
    volume = np.asarray(volume, dtype=np.float64)
    direction = np.asarray(direction, dtype=np.float64)
    bid_volume = np.where(direction < 0, volume, 0.0)
    bid_total = np.bincount(contract_idx, weights=bid_volume, minlength=n_contracts)
    all_total = np.bincount(contract_idx, weights=volume, minlength=n_contracts)
    with np.errstate(invalid="ignore", divide="ignore"):
        frac = bid_total / all_total
    return np.where(all_total > 0, frac, 0.5)


def flag_ghost_wall(
    v_oi: FloatArray,
    bid_fraction: FloatArray,
    v_oi_threshold: float = 1.05,
    bid_fraction_threshold: float = 0.70,
    established_oi: Optional[FloatArray] = None,
    min_established_oi: float = 100.0,
) -> NDArray[np.bool_]:
    """Component 1 — Ghost Wall filter (the Phase 3 paper's validation table,
    pg. 3: "V/OI > 1.05, > 70% at the Bid price -> Ghost Wall
    (Dismantling/Fragile)"). True where V/OI > threshold AND >70% of that
    strike's volume executed at the bid -- large-scale liquidation, not
    defensive accumulation (which would print predominantly at the ask; see
    the paper's own contrasting ">1.05, >70% at Ask -> Reinforced Wall" row).

    audit fix 2026-07-04: real Unusual Whales flow fed through this function
    for the first time showed the raw V/OI rule alone is noise-prone on thin
    strikes -- a handful of contracts trading against near-zero prior OI
    trivially clears 1.05x on any real trade at all (29 of 37 raw hits across
    a 14-ticker/~3,100-contract validation run were exactly this). When
    `established_oi` is supplied, also require prior_oi >= min_established_oi
    before the flag can fire. Left optional (default None -> no floor) so
    existing callers without an OI array on hand keep the old bare-ratio
    behavior.
    """
    raw = (np.asarray(v_oi) > v_oi_threshold) & (np.asarray(bid_fraction) > bid_fraction_threshold)
    if established_oi is None:
        return raw
    return raw & (np.asarray(established_oi) >= min_established_oi)


@dataclass
class VolatilityTermStructureMonitor:
    """Component 2 — VIX/VXV term structure engine (the Phase 3 paper, pg.
    4-5). Tracks the continuous ratio and derives both the qualitative regime
    label and the P(C) T_state severity score from the same underlying
    calculation (see gamma_wall_reliability below for why that matters).
    """

    vix: float
    vxv: float

    @property
    def ratio(self) -> float:
        return self.vix / self.vxv if self.vxv > 0 else float("inf")

    @property
    def regime(self) -> str:
        r = self.ratio
        if r < 0.95:
            return "STEEP_CONTANGO"
        if r <= 1.0:
            return "FLAT_CONTANGO"
        return "BACKWARDATION"

    @staticmethod
    def t_state_from_ratio(ratio: float) -> float:
        """T_state (pg. 8-9): 0 at/below parity, scaling to 1.0 at a 10%
        inversion (VIX/VXV=1.10), per the paper's exact formula. A staticmethod
        so CascadeBreakdownEngine can compute it from a bare ratio without
        constructing a throwaway monitor instance."""
        if ratio <= 1.0:
            return 0.0
        return float(min(1.0, (ratio - 1.0) * 10.0))

    @property
    def t_state(self) -> float:
        return self.t_state_from_ratio(self.ratio)

    @property
    def gamma_wall_reliability(self) -> float:
        """Structural downgrade of traditional Gamma-wall reliability under
        backwardation (the Phase 3 paper, pg. 5, SPAN margin / Vanna feedback
        section -- described qualitatively as "deteriorates rapidly", no
        exact formula given). Implemented as 1.0 - t_state: full reliability
        in contango, decaying linearly to 0 by the same VIX/VXV=1.10 deep-
        inversion point t_state already saturates at. Deliberately reuses
        t_state rather than introducing a second, independent magic number
        for the same underlying severity.
        """
        return 1.0 - self.t_state


@dataclass
class VexExtremeTracker:
    """Rolling-window tracker for V_state's "Max Historical |VEX|" (the Phase
    3 paper, pg. 9): "rolling 90-day maximum" of prior (not including today's
    own) |VEX| readings, so a new all-time-worst reading is capped at exactly
    1.0 by v_state's own min(1.0, ...) clip rather than dividing by itself.
    """

    window: int = 90
    _history: list = field(default_factory=list)

    def v_state(self, vex: float) -> float:
        """V_state: 0 if VEX>=0 (supportive); |VEX| / rolling-max(|prior VEX|)
        if VEX<0, clipped to [0,1]. Bootstraps to 1.0 on the very first
        negative reading (no history yet -> maximally severe by default)."""
        if vex >= 0:
            state = 0.0
        elif not self._history:
            state = 1.0
        else:
            state = min(1.0, abs(vex) / max(self._history))
        self._history.append(abs(vex))
        if len(self._history) > self.window:
            self._history.pop(0)
        return float(state)


@dataclass
class CascadeAssessment:
    """Result of one CascadeBreakdownEngine.evaluate() call."""

    g_state: float
    t_state: float
    f_state: float
    v_state: float
    p_c: float
    rule: str
    hard_pivot: bool
    ghost_wall: bool


@dataclass
class CascadeBreakdownEngine:
    """Component 3 — The Cascade Breakdown Probability Engine (the Phase 3
    paper, pg. 8-9): P(C) = w1*G_state + w2*T_state + w3*F_state + w4*V_state,
    with the paper's exact empirical weights, plus Component 4's rule
    classification and hard-pivot trigger.
    """

    w_gamma: float = 0.2
    w_term: float = 0.3
    w_flow: float = 0.3
    w_vanna: float = 0.2
    # AUDIT FIX (2026-07-04, universe expanded 14 ETFs -> 192 single stocks):
    # the paper's lam=0.05 was calibrated against an ABSOLUTE dollar flip-spot
    # distance -- fine at SPY's ~$750 scale (a real flip-spot gap of ~$8 is a
    # genuinely large relative move there), but the same fixed lam against the
    # same absolute distance on an $80 stock understates a PROPORTIONALLY much
    # larger move, because gap-in-dollars shrinks with price even when
    # gap-as-a-percent doesn't. Confirmed live: this is the actual mechanism
    # behind P(C) reading systematically "too cool" for the newly-added
    # single-name universe vs the original ETF-only build. Fixed by
    # normalizing distance as a FRACTION of gamma_flip (same convention
    # WallStabilityEngine.i_gex already uses for its own analogous distance
    # term) and recalibrating lam so SPY's own historical reading is
    # unchanged: back-solved from spot=744.07/gamma_flip=752.05 (today's real
    # snapshot) so lam=37.603 reproduces the exact G_state the old
    # lam=0.05/absolute-distance formula gave SPY, while now scaling
    # correctly for every other price level too.
    lam: float = 37.603
    pivot_threshold: float = 0.75  # exact threshold specified in the Phase 3 brief

    def __post_init__(self) -> None:
        total = self.w_gamma + self.w_term + self.w_flow + self.w_vanna
        if not np.isclose(total, 1.0):
            raise ValueError(f"P(C) weights must sum to 1.0, got {total}")

    def g_state(self, spot: float, gamma_flip: Optional[float]) -> float:
        """G_state (pg. 8): 0 in positive gamma (spot above the flip); an
        asymptotic exponential in [0,1) deepening as spot falls below it.

        Distance is normalized as a FRACTION of gamma_flip, not an absolute
        dollar amount (see the AUDIT FIX note on `lam` above) -- this is what
        makes the same lam meaningful across a $80 stock and a $750 index
        alike, since a 1% flip-spot gap now reads the same regardless of the
        ticker's own price level."""
        if gamma_flip is None or spot >= gamma_flip:
            return 0.0
        relative_dist = (gamma_flip - spot) / gamma_flip
        return float(1.0 - np.exp(-self.lam * relative_dist))

    @staticmethod
    def f_state(v_oi: float, bid_dominant: bool) -> float:
        """F_state (pg. 9), with the ask-side/accumulation gap closed (see
        the PHASE 3 banner above): 0 unless V/OI>1.0 AND flow is bid-side-
        dominant (unwinding); scales to 1.0 by V/OI=1.5."""
        if v_oi <= 1.0 or not bid_dominant:
            return 0.0
        return float(min(1.0, (v_oi - 1.0) * 2.0))

    def probability(self, g: float, t: float, f: float, v: float, f_state_tracked: bool = True) -> float:
        """P(C) = w1*G + w2*T + w3*F + w4*V -- bounded in [0,1] by
        construction since each input is bounded in [0,1] and the weights
        sum to 1.0 (enforced in __post_init__).

        AUDIT FIX (2026-07-04): when F_state genuinely isn't computable (no
        live tick-level order flow -- advanced_gex.py hard-pins f=0.0 and
        flags this via p_c_flow_state_tracked=False), the OLD behavior just
        multiplied that 0.0 by w_flow=0.3 like a real "no unwind detected"
        reading, permanently capping P(C) at w_gamma+w_term+w_vanna=0.7 --
        meaning hard_pivot (>0.75) could NEVER fire, no matter how extreme
        G/T/V were. f_state_tracked=False now excludes F_state entirely
        (rather than silently treating "untracked" as "measured zero") and
        proportionally redistributes w_gamma/w_term/w_vanna to sum to 1.0
        among themselves, so P(C) can legitimately reach 1.0 from the 3
        components that ARE live. This does not invent a value for F --
        it's the same "exclude what you don't have, don't fake it" principle
        as every other honesty flag in this pipeline. Callers MUST keep
        threading p_c_flow_state_tracked (or equivalent) alongside the
        result, same as today, so a redistributed P(C) is never mistaken for
        a real 4-factor reading -- this fixes P(C)'s ceiling, not the
        existing "partial" caveat, which still applies.
        """
        if not f_state_tracked:
            tracked_total = self.w_gamma + self.w_term + self.w_vanna
            return float((self.w_gamma * g + self.w_term * t + self.w_vanna * v) / tracked_total)
        return float(self.w_gamma * g + self.w_term * t + self.w_flow * f + self.w_vanna * v)

    @staticmethod
    def classify(positive_gamma_regime: bool, p_c: float, pivot_threshold: float = 0.75) -> Tuple[str, bool]:
        """Rule classification (pg. 7-8), driven by P(C) crossed with the GEX
        regime sign -- see the PHASE 3 banner above for why this is a
        threshold cascade on the paper's own stated per-rule P(C) ranges
        rather than a literal, all-conditions-at-once table match. Returns
        (rule_label, hard_pivot_flag); hard_pivot is True exactly when
        p_c > pivot_threshold, per the prompt's explicit instruction.
        """
        hard_pivot = p_c > pivot_threshold
        if positive_gamma_regime:
            return ("Alpha-1 (Fortress Wall)" if p_c < 0.15 else "Beta-2 (Stressed Wall)"), hard_pivot
        return ("Delta-4 (Ghost Wall / Breakdown Imminent)" if hard_pivot
                else "Gamma-3 (Migrating Wall)"), hard_pivot

    def evaluate(
        self,
        spot: float,
        gamma_flip: Optional[float],
        vix_vxv_ratio: float,
        v_oi: float,
        bid_dominant: bool,
        vex: float,
        vex_tracker: VexExtremeTracker,
    ) -> CascadeAssessment:
        """Full Component 3+4 pipeline for one Put Wall strike: computes all
        four states, P(C), the rule classification, and the hard-pivot /
        ghost-wall flags in one call."""
        positive_gamma_regime = gamma_flip is None or spot >= gamma_flip
        g = self.g_state(spot, gamma_flip)
        t = VolatilityTermStructureMonitor.t_state_from_ratio(vix_vxv_ratio)
        f = self.f_state(v_oi, bid_dominant)
        v = vex_tracker.v_state(vex)
        p_c = self.probability(g, t, f, v)
        rule, hard_pivot = self.classify(positive_gamma_regime, p_c, self.pivot_threshold)
        ghost = bool(flag_ghost_wall(np.array([v_oi]), np.array([1.0 if bid_dominant else 0.0]))[0])
        return CascadeAssessment(g, t, f, v, p_c, rule, hard_pivot, ghost)


# =============================================================================
# PHASE 4 — Execution State Machine: order-flow/CVD divergence filters,
# dynamic expiration selection, and the four regime-specific execution gates.
# Builds on Phases 1-3; nothing above this banner is modified.
#
# Reference: "Gamma Trading System Logic.pdf" (uploaded, hereafter "the
# Phase 4 paper").
#
# SCOPE NOTE: AdvancedGammaExecutionEngine.evaluate_* below return a plain
# dict describing a candidate trade (status/strategy/target/stop/...).
# Nothing in this file places an order, calls a broker API, or touches the
# live box -- these are recommendation objects only, consistent with this
# project's existing posture (promote_champion.py is deliberately disabled;
# signals don't auto-promote to live capital without a separate, explicit step).
#
# VERIFIED CORRECT / CONSISTENT, NO CHANGE NEEDED:
#   - GEX_K (pg. 1-2) again prints the same backwards (-Call, +Put) dealer-
#     sign convention flagged in Phase 2/3 -- not re-fixed here, since Phase 4
#     never recomputes GEX from scratch; it only consumes net_gex from the
#     already-corrected DynamicGammaFlipEngine.objective_function.
#   - SVI parametrization (pg. 1) is cited as background for why the surface
#     is arbitrage-free; it's the standard Gatheral raw-SVI form and is
#     correct as printed, but nothing here re-implements it -- Phase 2's
#     VolatilitySurfaceCalibrator (cubic-spline + convex-hull) already
#     delivers the same guarantee via a different, already-tested method.
#   - Mean-Reversion-Short's target = max(gamma_flip, poc) (pg. 8 pseudocode)
#     looks backwards on first read for a SHORT (why max, not min?) -- but
#     the prose (pg. 6: "whichever is closer to the entry price") resolves
#     it: shorting from above toward two candidate downside levels, the one
#     "closer to entry" is the HIGHER of the two, i.e. exactly max(). Traced
#     through and confirmed consistent, not changed.
#
# NOTED, NOT ACTED ON (cross-document Charm sign, again -- see Phase 3's
# banner): this paper also defines Charm = -dDelta/dt (pg. 1), opposite of
# Phase 2's paper / this project's already-verified DynamicGammaFlipEngine.
# _charm. As in Phase 3, tracing the actual USE of CHEX here (Mean-Reversion-
# Short: positive CHEX -> a call-wall book's excess long-stock hedge unwinds
# -> dealer SELLING) against Phase 2's own walkthrough (positive CHEX -> a
# put-wall book's excess short-stock hedge unwinds -> dealer BUYING) shows
# these are the SAME sign convention applied to opposite starting hedges, not
# an actual contradiction in how CHEX is consumed -- only the abstract
# definition line disagrees. net_chex is only ever consumed here, never
# recomputed, so nothing changes.
#
# DISCREPANCIES FOUND AND RESOLVED (source-internal, prose/table vs.
# pseudocode):
#   - Mean-Reversion Long's own VALIDATION TABLE (pg. 4) lists Net_CHEX<=0 as
#     a required condition, but the paper's own pseudocode (pg. 4-5) never
#     checks it -- and the Phase 4 brief's description of gate A also omits
#     it. Followed the pseudocode + brief (majority, and more specific) over
#     the table; net_chex is not gated in evaluate_mean_reversion_long.
#   - Mean-Reversion Long: prose says "1DTE to 3DTE" (pg. 4, and the Phase 4
#     brief says the same); the paper's own pseudocode calls
#     select_optimal_expiration(min_dte=1, max_dte=5). Followed the explicit
#     brief/prose (1-3 DTE) over the unnarrated pseudocode max_dte=5.
#   - Momentum Short's hedging_impact (pg. 9-10 pseudocode) hardcodes
#     `abs(net_gex) * 0.01`, where the paper's own general formula (pg. 2)
#     defines Hedging Impact using a live (delta_S / S) term, not a constant.
#     Read 0.01 as standing in for "a standardized 1% move" -- consistent
#     with how $GEX itself is expressed "per 1% spot move" everywhere else in
#     this codebase -- and implemented hedging_impact() with that as a
#     configurable default (move_pct=0.01), not a hardcoded literal, so a
#     caller can substitute a live observed move for the general form instead.
#
# NOT IMPLEMENTED (explicitly out of scope per the Phase 4 brief's own
# component list, to avoid scope creep beyond what was asked): the
# pseudocode's 4th Mean-Reversion-Long sub-check (state.order_flow.
# volume_oscillator < 0) and the detailed multi-candle "reversal candle
# prints higher volume than the breakdown candle -> hard abort + position
# reversal" logic in Momentum Short's risk section (pg. 10-11). The core
# stop-hunt filter that IS in scope (Classic Bullish Divergence) is
# implemented and gates Momentum Short below.
# =============================================================================
def compute_ofi(volume: FloatArray, direction: FloatArray) -> float:
    """Order Flow Imbalance: OFI = sum(V_i * D_i) (the Phase 4 paper, pg. 2).
    Same signed-volume-sum pattern already used for NetIntradayFlow in
    ChainState (Component 1) and for net-signed volume in TradeFlowClassifier
    (Component 2) -- applied here to the underlying's tape instead of the
    option chain's.
    """
    return float(np.sum(np.asarray(volume, dtype=np.float64) * np.asarray(direction, dtype=np.float64)))


def detect_price_cvd_divergence(
    price: FloatArray, cvd: FloatArray, kind: str, lookback: int = 20
) -> bool:
    """Component 1 -- CVD divergence filter (the Phase 4 paper, pg. 3-4, 6-7).

    kind='BULLISH_CLASSIC': price prints a LOWER low than its prior swing low
    while CVD prints a HIGHER low -- sellers losing momentum (a stop-loss-hunt
    signature; validates Mean-Reversion Long and filters Momentum Short/
    Trapdoor false breakdowns).
    kind='BEARISH_CLASSIC': price prints a HIGHER high than its prior swing
    high while CVD prints a LOWER high -- buyers exhausting (validates
    Mean-Reversion Short).

    Swing points are the extremum of each half of the trailing `lookback`
    window (prior half vs. recent half) -- simpler and more deterministic
    than general peak-finding, and sufficient for the two-point comparison
    the paper's divergence definitions actually require. Returns False (no
    signal fabricated) if the window is too short, consistent with this
    project's existing "a blank read is a valid read" convention (gex.py).
    """
    price = np.asarray(price, dtype=np.float64)[-lookback:]
    cvd = np.asarray(cvd, dtype=np.float64)[-lookback:]
    n = price.shape[0]
    if n < 4:
        return False
    mid = n // 2
    prior_p, recent_p = price[:mid], price[mid:]
    prior_c, recent_c = cvd[:mid], cvd[mid:]
    if kind == "BULLISH_CLASSIC":
        return bool(recent_p.min() < prior_p.min() and recent_c.min() > prior_c.min())
    if kind == "BEARISH_CLASSIC":
        return bool(recent_p.max() > prior_p.max() and recent_c.max() < prior_c.max())
    raise ValueError(f"unknown divergence kind: {kind}")


def confirm_trend_alignment(price: FloatArray, cvd: FloatArray, trend: str, lookback: int = 20) -> bool:
    """Confirms price and CVD are moving together, i.e. no absorption/
    divergence (the Phase 4 paper, pg. 9: "CVD is moving aggressively with
    price (No Divergence/Absorption)") -- validates genuine capitulation in
    Momentum Short/Trapdoor and genuine breakout thrust in Momentum Long/
    Squeeze. trend='DOWN': both price and CVD make a lower low over the
    window. trend='UP': both make a higher high.
    """
    price = np.asarray(price, dtype=np.float64)[-lookback:]
    cvd = np.asarray(cvd, dtype=np.float64)[-lookback:]
    n = price.shape[0]
    if n < 4:
        return False
    mid = n // 2
    if trend == "DOWN":
        return bool(price[mid:].min() < price[:mid].min() and cvd[mid:].min() < cvd[:mid].min())
    if trend == "UP":
        return bool(price[mid:].max() > price[:mid].max() and cvd[mid:].max() > cvd[:mid].max())
    raise ValueError(f"unknown trend: {trend}")


def detect_stacked_imbalances(
    ofi_by_level: FloatArray, min_consecutive: int = 3, ratio_threshold: float = 3.0
) -> bool:
    """Component 1 -- stacked-imbalance filter (the Phase 4 paper, pg. 11):
    a true Gamma Squeeze prints >= min_consecutive CONSECUTIVE price levels
    where buy-side market orders exceed sell-side limit orders by
    ratio_threshold:1 or more. `ofi_by_level` is each level's buy/sell ratio,
    levels ordered by price. Vectorized via convolution over a boolean hit
    mask (a run-length check), no Python loop over levels.
    """
    levels = np.asarray(ofi_by_level, dtype=np.float64)
    if levels.shape[0] < min_consecutive:
        return False
    hits = (levels >= ratio_threshold).astype(np.float64)
    run_counts = np.convolve(hits, np.ones(min_consecutive), mode="valid")
    return bool(np.any(run_counts >= min_consecutive))


def detect_absorption(
    price_change_pct: float,
    cvd_surge: float,
    price_stall_threshold: float = 0.0015,
    cvd_surge_threshold: float = 0.0,
) -> bool:
    """Component 1 -- absorption filter (the Phase 4 paper, pg. 11-12): price
    stalling (|price_change_pct| below price_stall_threshold) despite a
    massive positive cvd_surge (aggressive buyers swallowed by passive/
    iceberg sell orders) -- a deterministic bull-trap signal that aborts
    Momentum Long/Squeeze. cvd_surge_threshold is instrument-scale-dependent
    (shares); callers should pass something like a multiple of average bar
    volume, not a universal constant.
    """
    return bool(abs(price_change_pct) < price_stall_threshold and cvd_surge > cvd_surge_threshold)


def select_optimal_expiration(available_dtes: NDArray[np.int_], min_dte: int, max_dte: int) -> Optional[int]:
    """Component 2 -- Dynamic Expiration Selection (the Phase 4 paper, pg. 4,
    8-9, 13): picks the nearest available DTE within [min_dte, max_dte]; if
    the chain has nothing in that window, falls back to the nearest
    available DTE beyond max_dte (the paper's documented "nearest weekly"
    fallback: pg. 4 "or the nearest weekly expiration"; pg. 8 "nearest-term
    weeklies if 0DTE is unavailable"). Returns None if nothing exists even
    with min_dte runway (fully out of contracts).
    """
    available = np.asarray(available_dtes, dtype=np.int64)
    in_range = available[(available >= min_dte) & (available <= max_dte)]
    if in_range.size > 0:
        return int(in_range.min())
    beyond = available[available > max_dte]
    if beyond.size > 0:
        return int(beyond.min())
    return None


def locate_next_high_oi_node(strikes: FloatArray, oi: FloatArray, spot: float, direction: str) -> Optional[float]:
    """Locates 'the next major downside/upside Open Interest cluster' (the
    Phase 4 paper, pg. 9, 12) -- the strike with the largest OI on the
    requested side of spot."""
    strikes = np.asarray(strikes, dtype=np.float64)
    oi = np.asarray(oi, dtype=np.float64)
    mask = strikes < spot if direction == "DOWN" else strikes > spot
    if not np.any(mask):
        return None
    side_strikes, side_oi = strikes[mask], oi[mask]
    return float(side_strikes[np.argmax(side_oi)])


def calculate_gamma_exhaustion_node(
    strikes: FloatArray, gex_by_strike: FloatArray, spot: float, exhaustion_frac: float = 0.10
) -> Optional[float]:
    """The 'Gamma Exhaustion Node' (the Phase 4 paper, pg. 12): the nearest
    strike above spot where the aggregate |GEX| concentration first decays
    below exhaustion_frac of its running peak, i.e. where the forced dealer-
    buying loop that fuels a squeeze runs out of gamma to burn.
    """
    strikes = np.asarray(strikes, dtype=np.float64)
    gex = np.abs(np.asarray(gex_by_strike, dtype=np.float64))
    order = np.argsort(strikes)
    strikes, gex = strikes[order], gex[order]
    above = strikes > spot
    if not np.any(above):
        return None
    strikes_above, gex_above = strikes[above], gex[above]
    running_peak = np.maximum.accumulate(gex_above)
    if running_peak[-1] <= 0:
        return None
    exhausted = np.where(gex_above < exhaustion_frac * running_peak)[0]
    if exhausted.size == 0:
        return float(strikes_above[-1])  # never exhausts within the chain -> farthest strike available
    return float(strikes_above[exhausted[0]])


def hedging_impact(net_gex: float, adv: float, move_pct: float = 0.01) -> float:
    """Hedging Impact = |Net_GEX| * move_pct / ADV (the Phase 4 paper, pg. 2;
    see the PHASE 4 banner above for the pseudocode-vs-prose discrepancy this
    resolves). Used both as a leading indicator of hedging velocity and,
    directly, as Momentum Short/Trapdoor's position-size multiplier.

    UNITS CAVEAT (audit note): net_gex and adv must be denominated alike --
    dollar GEX over dollar ADV (or share-equivalents over shares) -- for the
    result to be a meaningful dimensionless fraction of daily liquidity.
    Dollar GEX over share ADV (an easy default mistake, since Alpaca bars
    report share volume) makes the multiplier arbitrary. The paper's own
    pseudocode never specifies units; flagged here rather than hidden.
    """
    if not (adv > 0):
        return 0.0
    return float(abs(net_gex) * move_pct / adv)


@dataclass
class ExecutionState:
    """Flattened snapshot feeding AdvancedGammaExecutionEngine -- assembled
    from Phase 1/2 (DynamicGammaFlipEngine), Phase 2 (WallStabilityEngine),
    and Phase 3 (CascadeBreakdownEngine) outputs, plus the Phase 4 order-flow
    inputs. Mirrors the paper's nested state.exposures / state.levels /
    state.order_flow / state.vol_surface access pattern, flattened into one
    object rather than four.
    """

    spot: float
    put_wall: float
    call_wall: float
    gamma_flip: Optional[float]
    net_gex: float
    net_vex: float
    net_chex: float
    vgex_ratio: float             # intraday volume-weighted GEX ratio vs. the overnight GEX ratio
    term_structure: str            # "CONTANGO" or "BACKWARDATION"
    atm_iv_trend: str              # "RISING", "FALLING", or "FLAT"
    price_series: FloatArray
    cvd_series: FloatArray
    volume: float
    avg_volume_20: float
    adv: float
    poc: float                     # volume-profile Point of Control
    strikes: FloatArray
    oi_by_strike: FloatArray
    gex_by_strike: FloatArray
    available_dtes: NDArray[np.int_]
    ofi_by_level: Optional[FloatArray] = None
    is_friday_close: bool = False
    wss_score: Optional[float] = None   # from Phase 2's WallStabilityEngine (informational)
    p_c: Optional[float] = None         # from Phase 3's CascadeBreakdownEngine
    hard_pivot: Optional[bool] = None   # from Phase 3's CascadeBreakdownEngine


@dataclass
class AdvancedGammaExecutionEngine:
    """Component 3 -- The Four Core Execution Logic Gates (the Phase 4
    paper). Each evaluate_* method returns a plain action dict -- see the
    PHASE 4 banner's scope note: these are recommendations, not orders.
    """

    put_wall_proximity: float = 1.003    # Mean-Reversion-Long trigger band (pg. 4 pseudocode)
    call_wall_proximity: float = 0.997   # Mean-Reversion-Short trigger band (pg. 8 pseudocode)
    vgex_stability_max: float = 0.55     # Mean-Reversion-Short "wall is stable" ceiling (pg. 8 pseudocode)
    squeeze_vgex_min: float = 0.6        # Momentum-Long "heavy call volume" floor (pg. 11 pseudocode)
    volume_expansion_mult: float = 1.5   # shared breakout-confirmation multiplier (pg. 4, 10, 13)
    cvd_lookback: int = 20

    def evaluate_mean_reversion_long(self, state: ExecutionState) -> dict:
        """Regime 1 (the paper, pg. 2-5): positive GEX at the Put Wall."""
        if not (state.net_gex > 0 and state.spot <= state.put_wall * self.put_wall_proximity):
            return {"status": "HOLD_STATE", "reason": "not in a positive-GEX Put Wall test"}
        if not (state.net_vex >= 0 and state.term_structure == "CONTANGO"):
            return {"status": "HOLD_STATE", "reason": "Vanna/term-structure not supportive"}
        if not detect_price_cvd_divergence(
            state.price_series, state.cvd_series, kind="BULLISH_CLASSIC", lookback=self.cvd_lookback
        ):
            return {"status": "HOLD_STATE", "reason": "no bullish CVD divergence (no confirmed absorption)"}
        target = state.gamma_flip
        if target is None:
            # paper pg. 4: target "the Zero Gamma level ... or the nearest peak
            # GEX magnet above the current spot price" -- when no flip resolves
            # (deep one-sided regime), fall back to the latter
            target = locate_next_high_oi_node(
                state.strikes, np.abs(state.gex_by_strike), state.spot, direction="UP"
            )
        return {
            "status": "EXECUTE",
            "strategy": "MEAN_REVERSION_LONG",
            "direction": "LONG",
            "entry": state.spot,
            "target": target,
            "stop": state.put_wall * 0.995,
            "expiration_dte": select_optimal_expiration(state.available_dtes, min_dte=1, max_dte=3),
            "size_multiplier": 1.0,
        }

    def evaluate_mean_reversion_short(self, state: ExecutionState) -> dict:
        """Regime 2 (the paper, pg. 6-8): positive GEX at the Call Wall."""
        if not (state.net_gex > 0 and state.spot >= state.call_wall * self.call_wall_proximity):
            return {"status": "HOLD_STATE", "reason": "not in a positive-GEX Call Wall test"}
        if not (state.vgex_ratio <= self.vgex_stability_max):
            return {"status": "HOLD_STATE", "reason": "vGEX_Ratio spiking -- wall may be rolling higher"}
        if not (state.net_chex > 0):
            return {"status": "HOLD_STATE", "reason": "Charm not providing a selling tailwind"}
        if not detect_price_cvd_divergence(
            state.price_series, state.cvd_series, kind="BEARISH_CLASSIC", lookback=self.cvd_lookback
        ):
            return {"status": "HOLD_STATE", "reason": "no bearish CVD divergence (buying not exhausted)"}
        gamma_flip = state.gamma_flip if state.gamma_flip is not None else -np.inf
        return {
            "status": "EXECUTE",
            "strategy": "MEAN_REVERSION_SHORT",
            "direction": "SHORT",
            "entry": state.spot,
            "target": max(gamma_flip, state.poc),
            "stop": state.call_wall * 1.005,
            "expiration_dte": select_optimal_expiration(state.available_dtes, min_dte=1, max_dte=3),
            "size_multiplier": 1.5 if state.is_friday_close else 1.0,
        }

    def evaluate_momentum_short_trapdoor(self, state: ExecutionState) -> dict:
        """Regime 3 (the paper, pg. 9-11): negative GEX breaking below the Put Wall."""
        if not (state.net_gex < 0 and state.spot < state.put_wall):
            return {"status": "HOLD_STATE", "reason": "structural support has not broken"}
        if not (state.net_vex < 0 and state.term_structure == "BACKWARDATION"):
            return {"status": "HOLD_STATE", "reason": "Vanna feedback loop / term structure not confirmed"}
        if not (state.volume > state.avg_volume_20 * self.volume_expansion_mult):
            return {"status": "HOLD_STATE", "reason": "no volume expansion -- may be a thin-liquidity overshoot"}
        if detect_price_cvd_divergence(
            state.price_series, state.cvd_series, kind="BULLISH_CLASSIC", lookback=self.cvd_lookback
        ):
            return {"status": "HOLD_STATE", "reason": "hidden bullish CVD divergence -- likely a stop-loss hunt"}
        if not confirm_trend_alignment(
            state.price_series, state.cvd_series, trend="DOWN", lookback=self.cvd_lookback
        ):
            return {"status": "HOLD_STATE", "reason": "CVD not trend-aligned with price -- no true capitulation"}
        return {
            "status": "EXECUTE",
            "strategy": "MOMENTUM_SHORT_TRAPDOOR",
            "direction": "SHORT",
            "entry": state.spot,
            "target": locate_next_high_oi_node(state.strikes, state.oi_by_strike, state.spot, direction="DOWN"),
            "stop": state.put_wall * 1.002,
            "expiration_dte": select_optimal_expiration(state.available_dtes, min_dte=0, max_dte=1),
            "size_multiplier": hedging_impact(state.net_gex, state.adv),
        }

    def evaluate_momentum_long_squeeze(self, state: ExecutionState) -> dict:
        """Regime 4 (the paper, pg. 11-13): negative GEX breaking above the Call Wall."""
        if not (state.net_gex < 0 and state.spot > state.call_wall):
            return {"status": "HOLD_STATE", "reason": "overhead resistance has not broken"}
        if not (state.vgex_ratio > self.squeeze_vgex_min):
            return {"status": "HOLD_STATE", "reason": "insufficient fresh call buying to fuel a squeeze"}
        if not (state.atm_iv_trend == "RISING" and state.net_vex < 0):
            return {"status": "HOLD_STATE", "reason": "Vanna dual-front amplification not confirmed"}
        if not (state.volume > state.avg_volume_20 * self.volume_expansion_mult):
            return {"status": "HOLD_STATE", "reason": "no volume expansion"}
        if state.ofi_by_level is not None and not detect_stacked_imbalances(state.ofi_by_level):
            return {"status": "HOLD_STATE", "reason": "no stacked imbalances -- squeeze thrust unconfirmed"}
        lb = min(self.cvd_lookback, state.price_series.shape[0] - 1)
        price_change_pct = (state.price_series[-1] - state.price_series[-lb]) / state.price_series[-lb]
        cvd_surge = state.cvd_series[-1] - state.cvd_series[-lb]
        if detect_absorption(price_change_pct, cvd_surge, cvd_surge_threshold=state.avg_volume_20 * 2.0):
            return {"status": "HOLD_STATE", "reason": "absorption detected -- likely bull trap"}
        if not confirm_trend_alignment(
            state.price_series, state.cvd_series, trend="UP", lookback=self.cvd_lookback
        ):
            return {"status": "HOLD_STATE", "reason": "CVD not trend-aligned -- possible absorption/bull trap"}
        return {
            "status": "EXECUTE",
            "strategy": "MOMENTUM_LONG_SQUEEZE",
            "direction": "LONG",
            "entry": state.spot,
            "target": calculate_gamma_exhaustion_node(state.strikes, state.gex_by_strike, state.spot),
            "stop": state.call_wall * 0.998,
            "expiration_dte": select_optimal_expiration(state.available_dtes, min_dte=0, max_dte=0),
            "size_multiplier": 1.0,
        }

    def evaluate(self, state: ExecutionState) -> dict:
        """Dispatches through all four gates (GEX sign already makes Mean-
        Reversion vs. Momentum mutually exclusive); returns the first EXECUTE
        match or a HOLD_STATE summarizing all four gates' reasons.
        """
        gates = (
            self.evaluate_mean_reversion_long,
            self.evaluate_mean_reversion_short,
            self.evaluate_momentum_short_trapdoor,
            self.evaluate_momentum_long_squeeze,
        )
        reasons = []
        for gate in gates:
            result = gate(state)
            if result["status"] == "EXECUTE":
                return result
            reasons.append(f"{gate.__name__}: {result['reason']}")
        return {"status": "HOLD_STATE", "reason": reasons}


# =============================================================================
# Self-test — synthetic end-to-end run of all four components together.
# =============================================================================
def _selftest() -> bool:
    rng = np.random.default_rng(7)
    ok = True

    # --- Component 3 in isolation: verify the corrected Speed formula ---
    strikes = np.array([100.0])
    eng = DynamicGammaFlipEngine(strikes, eoi=np.array([1.0]), iv=np.array([0.22]),
                                  r=0.04, q=0.01, w=np.array([1.0]), is_call=np.array([True]))
    S, T = 100.0, np.array([0.15])
    h = 1e-4
    numerical = (eng._gamma(S + h, T) - eng._gamma(S - h, T)) / (2 * h)
    analytical = eng._speed(S, T)
    good = np.allclose(numerical, analytical, rtol=1e-6)
    ok &= good
    print(f"  [{'OK' if good else 'FAIL'}] Speed formula matches numerical dGamma/dS "
          f"(analytical={float(analytical[0]):.8f}, numerical={float(numerical[0]):.8f})")

    # --- Component 1: EOI blending ---
    n_contracts = 20
    prior_oi = rng.uniform(50, 500, n_contracts)
    chain = ChainState(prior_oi=prior_oi, lam=1.5)
    n_ticks = 2000
    contract_idx = rng.integers(0, n_contracts, n_ticks)
    volume = rng.uniform(1, 10, n_ticks)
    initiator = rng.choice([-1.0, 1.0], n_ticks)
    chain.apply_ticks(contract_idx, volume, initiator)
    eoi = chain.effective_oi
    good = np.all(np.isfinite(eoi)) and np.all(eoi >= 0)
    ok &= good
    print(f"  [{'OK' if good else 'FAIL'}] EOI blend finite & non-negative "
          f"(mean prior_oi={prior_oi.mean():.1f}, mean eoi={eoi.mean():.1f})")

    # --- Component 2: trade classification + dynamic w ---
    clf = TradeFlowClassifier()
    bid = 4.90 + 0.02 * np.sin(np.arange(n_ticks) / 50.0)
    ask = bid + 0.05
    trade_price = rng.choice([bid.mean(), (bid + ask).mean() / 2, ask.mean()], n_ticks)
    direction = clf.classify_lee_ready(trade_price, bid, ask)
    good = set(np.unique(direction)).issubset({-1.0, 1.0})
    ok &= good
    print(f"  [{'OK' if good else 'FAIL'}] Lee-Ready direction fully resolved (no zero/NaN residue)")

    is_call = rng.choice([True, False], n_contracts)
    w_dynamic = clf.dealer_weight_from_flow(contract_idx, direction, volume, is_call, n_contracts)
    good = np.all(np.abs(w_dynamic) <= 1.0) and np.all(np.isfinite(w_dynamic))
    ok &= good
    print(f"  [{'OK' if good else 'FAIL'}] dynamic dealer weight vector w in [-1, 1]")

    bvc_frac = clf.classify_bvc(trade_price, volume, bucket_size=50)
    good = np.all((bvc_frac >= 0) & (bvc_frac <= 1))
    ok &= good
    print(f"  [{'OK' if good else 'FAIL'}] BVC buy-fraction in [0, 1]")

    # --- Component 4: live session clock feeding Component 3 ---
    clock = SessionClock()
    now = datetime(2026, 7, 6, 15, 30)  # 30 min before a 0DTE close
    expiries = np.array(["2026-07-06"] * n_contracts, dtype="datetime64[D]")
    T_live = clock.time_to_expiry_years(now, expiries)
    good = np.all(T_live > 0) and np.allclose(T_live[0], (30 * 60) / SECONDS_PER_YEAR, rtol=1e-3)
    ok &= good
    print(f"  [{'OK' if good else 'FAIL'}] SessionClock T at 30min-to-close "
          f"= {float(T_live[0]):.8f}y")

    # --- End-to-end: full chain gamma flip using live w, EOI, and T ---
    strikes_chain = np.linspace(80, 120, n_contracts)
    iv_chain = np.full(n_contracts, 0.25)
    engine = DynamicGammaFlipEngine(strikes_chain, eoi, iv_chain, r=0.04, q=0.0,
                                     w=w_dynamic, is_call=is_call)
    flip = engine.calculate_gamma_flip(current_spot=100.0, T=T_live)
    good = flip is None or (60.0 < flip < 140.0)
    ok &= good
    print(f"  [{'OK' if good else 'FAIL'}] end-to-end gamma flip resolves to a sane level: {flip}")

    # --- audit regression: gamma-dead chain (far-OTM, minutes to expiry, gamma
    # numerically 0 -> F(x0)==0.0 exactly) must return None, not echo x0 back ---
    dead = DynamicGammaFlipEngine(np.array([50.0, 55.0]), eoi=np.array([100.0, 100.0]),
                                   iv=np.array([0.9, 0.9]), r=0.04, q=0.0,
                                   w=np.array([1.0, 1.0]), is_call=np.array([False, False]))
    flip_dead = dead.calculate_gamma_flip(current_spot=100.0, T=np.array([1e-4, 1e-4]))
    good = flip_dead is None
    ok &= good
    print(f"  [{'OK' if good else 'FAIL'}] gamma-dead chain returns None (got {flip_dead}), "
          f"not the initial guess masquerading as a flip")

    print("SELFTEST", "PASS" if ok else "FAIL")
    return ok


# =============================================================================
# Phase 2 self-test — surface calibration, VEX/CHEX sign verification (the
# Correction C regression test), IV-shock and 0DTE-grind scenarios, WSS.
# =============================================================================
def _selftest_phase2() -> bool:
    rng = np.random.default_rng(11)
    ok = True

    # --- Component 5: arbitrage-free surface calibration ---
    S0, r, q, T = 100.0, 0.04, 0.0, 30 / 365.0
    strikes = np.arange(70.0, 131.0, 2.5)
    true_iv = 0.18 + 0.0009 * (strikes - S0) ** 2 / S0  # a clean smile
    calib = VolatilitySurfaceCalibrator(r=r, q=q)
    true_call = calib._bs_call_price(S0, strikes, np.full_like(strikes, T), true_iv)
    noisy_price = true_call + rng.normal(0, 0.03, strikes.shape[0])  # bid-ask microstructure noise
    is_call_surf = np.ones_like(strikes, dtype=bool)
    smooth_iv = calib.calibrate(strikes, noisy_price, T, S0, is_call_surf)

    good = np.all(np.isfinite(smooth_iv)) and np.all(smooth_iv > 0)
    ok &= good
    print(f"  [{'OK' if good else 'FAIL'}] surface calibration produces finite, positive IV")

    order = np.argsort(strikes)
    smoothed_call = calib._bs_call_price(S0, strikes[order], np.full(strikes.shape[0], T), smooth_iv[order])
    second_diff = np.diff(smoothed_call, 2)
    good = np.all(second_diff >= -1e-6)
    ok &= good
    print(f"  [{'OK' if good else 'FAIL'}] convex hull projection: d^2(price)/dK^2 >= 0 everywhere "
          f"(min second-diff={second_diff.min():.2e})")

    # --- audit regressions (2026-07-04) for the three calibrate() fixes ---
    # (a) a live-chain shape -- a call AND a put at every strike -- must calibrate,
    #     with both rows at a strike receiving the same IV
    K_dup = np.concatenate([strikes, strikes])
    ic_dup = np.concatenate([np.ones(strikes.size, dtype=bool), np.zeros(strikes.size, dtype=bool)])
    px_dup = calib._bs_price(S0, K_dup, np.full(K_dup.size, T), np.concatenate([true_iv, true_iv]), ic_dup)
    px_dup = px_dup + rng.normal(0, 0.02, K_dup.size)
    iv_dup = calib.calibrate(K_dup, px_dup, T, S0, ic_dup)
    good = (iv_dup.shape == K_dup.shape and np.all(np.isfinite(iv_dup)) and np.all(iv_dup > 0)
            and np.allclose(iv_dup[: strikes.size], iv_dup[strikes.size:]))
    ok &= good
    print(f"  [{'OK' if good else 'FAIL'}] call+put duplicate-strike chain calibrates "
          f"(previously crashed on non-monotone x)")

    # (b) accuracy: with genuine smoothing, the calibrated smile tracks the true
    #     smile in the identifiable ATM band instead of threading the noise's
    #     lower envelope (pre-fix mean bias was about -0.07)
    atm = np.abs(K_dup - S0) <= 15.0
    bias = float(np.mean(iv_dup[atm] - np.concatenate([true_iv, true_iv])[atm]))
    mae = float(np.mean(np.abs(iv_dup[atm] - np.concatenate([true_iv, true_iv])[atm])))
    good = abs(bias) < 0.02 and mae < 0.02
    ok &= good
    print(f"  [{'OK' if good else 'FAIL'}] calibrated smile tracks truth in the ATM band "
          f"(bias={bias:+.4f}, MAE={mae:.4f})")

    # (c) a below-intrinsic (unsolvable) quote is masked and bridged from
    #     neighbors, not solved to iv~=0 and propagated into the surface
    px_poison = true_call.copy()
    px_poison[0] = 25.0  # K=70 deep-ITM call, intrinsic ~30.2 -> quote below intrinsic
    iv_poison = calib.calibrate(strikes, px_poison, T, S0, is_call_surf)
    good = np.all(np.isfinite(iv_poison)) and iv_poison[0] > 0.05
    ok &= good
    print(f"  [{'OK' if good else 'FAIL'}] below-intrinsic quote masked, not pinned to iv~0 "
          f"(recovered iv at poisoned strike: {iv_poison[0]:.4f})")

    # --- Cross-expiry calendar-spread arbitrage check/enforce ---
    cal_eng = CalendarArbitrageEngine(r=r, q=q)
    strikes_near = np.arange(85.0, 116.0, 2.5)
    strikes_far = np.arange(80.0, 121.0, 2.5)
    T_near, T_far = 10 / 365.0, 30 / 365.0
    iv_near_flat = np.full(strikes_near.shape[0], 0.35)  # event-vol spike, short-dated
    iv_far_flat = np.full(strikes_far.shape[0], 0.15)    # deliberately too low -> injected violation

    raw_expiries = [(T_near, strikes_near, iv_near_flat), (T_far, strikes_far, iv_far_flat)]
    violations = cal_eng.check(raw_expiries, S=S0)
    good = len(violations) > 0
    ok &= good
    print(f"  [{'OK' if good else 'FAIL'}] calendar-arbitrage check detects the injected violation "
          f"({len(violations)} moneyness points flagged)")

    fixed = cal_eng.enforce(raw_expiries, S=S0)
    post_violations = cal_eng.check(
        [(T_near, strikes_near, fixed[T_near]), (T_far, strikes_far, fixed[T_far])], S=S0
    )
    good = (len(post_violations) == 0 and np.all(fixed[T_near] > 0) and np.all(fixed[T_far] > 0))
    ok &= good
    print(f"  [{'OK' if good else 'FAIL'}] enforcement restores monotonic total variance "
          f"(post-fix violations={len(post_violations)}, far-expiry mean IV "
          f"{iv_far_flat.mean():.3f} -> {float(fixed[T_far].mean()):.3f})")

    # --- Correction C regression test: dealer-short-OTM-put book -> Net VEX > 0 ---
    put_strikes = np.arange(70.0, 100.0, 2.5)   # OTM puts, K < spot
    n_put = put_strikes.shape[0]
    eoi_put_book = np.full(n_put, 500.0)
    iv_put_book = np.full(n_put, 0.28)
    w_short_puts = np.full(n_put, -1.0)  # dealer net short these puts (Customer Buy -> Dealer Short)
    is_call_put_book = np.zeros(n_put, dtype=bool)
    put_engine = DynamicGammaFlipEngine(put_strikes, eoi_put_book, iv_put_book, r=r, q=q,
                                        w=w_short_puts, is_call=is_call_put_book)
    T_book = np.full(n_put, 20 / 365.0)
    vex_short_puts = put_engine.net_vex(S0, T_book)
    good = vex_short_puts > 0
    ok &= good
    print(f"  [{'OK' if good else 'FAIL'}] Correction C: dealer-short-OTM-put book -> Net VEX > 0 "
          f"(got {vex_short_puts:,.0f})")

    # --- Vanna Bid (IV crush -> bullish) vs. Vanna Ask (IV expansion -> bearish) ---
    wss = WallStabilityEngine()
    N_scalar = 1e5  # standardizing scalar (e.g. 30d ADDV); scaled small here so the
                     # demo book's effect size is legible -- a real name's ADDV would
                     # dwarf a 12-strike synthetic book and mute the printed magnitude
                     # without changing the sign check below.
    f_vex_crush = wss.f_vex(vex_short_puts, delta_iv=-0.05, standardizing_scalar=N_scalar)
    f_vex_expand = wss.f_vex(vex_short_puts, delta_iv=+0.05, standardizing_scalar=N_scalar)
    good = f_vex_crush > 0 and f_vex_expand < 0
    ok &= good
    print(f"  [{'OK' if good else 'FAIL'}] Vanna Bid (IV crush) bullish={f_vex_crush:+.4f}, "
          f"Vanna Ask (IV expansion) bearish={f_vex_expand:+.4f}")

    # --- Component: Charm/CHEX blow-up near expiry for an ATM contract ---
    atm_engine = DynamicGammaFlipEngine(
        np.array([S0]), eoi=np.array([1000.0]), iv=np.array([0.20]),
        r=r, q=q, w=np.array([-1.0]), is_call=np.array([False]),
    )
    chex_far = atm_engine.net_chex(S0, T=np.array([30 / 365.0]))
    chex_near = atm_engine.net_chex(S0, T=np.array([(30 * 60) / SECONDS_PER_YEAR]))  # 30 min to close
    good = abs(chex_near) > abs(chex_far) * 5
    ok &= good
    print(f"  [{'OK' if good else 'FAIL'}] ATM Charm/CHEX blows up near expiry "
          f"(30d={chex_far:,.0f}  30min={chex_near:,.0f})")

    # --- Dynamic Clock-Weighting: omega_3 grows in the final 60 minutes ---
    close = datetime(2026, 7, 6, 16, 0)
    w_mid_session = wss.dynamic_weights(datetime(2026, 7, 6, 13, 0), close)
    w_final_hour = wss.dynamic_weights(datetime(2026, 7, 6, 15, 55), close)
    good = (w_final_hour[2] > w_mid_session[2] * 2) and np.isclose(sum(w_final_hour), 100.0)
    ok &= good
    print(f"  [{'OK' if good else 'FAIL'}] omega_3 scales up near the close "
          f"(mid-session w3={w_mid_session[2]:.1f}, final-hour w3={w_final_hour[2]:.1f}, "
          f"still sums to 100={sum(w_final_hour):.1f})")

    # --- Categorical flag mapping ---
    flags = [wss.classify(v) for v in (75.0, 25.0, -25.0, -75.0, 50.0, 0.0, -50.0)]
    good = flags == ["HARD FLOOR", "YIELDING", "FRACTURED", "TRAPDOOR",
                      "HARD FLOOR", "YIELDING", "FRACTURED"]
    ok &= good
    print(f"  [{'OK' if good else 'FAIL'}] categorical flag mapping: {flags}")

    # --- End-to-end WSS, scenario 1: a sudden volatility (IV expansion) shock,
    # isolated by holding the session time / CHEX contribution fixed at a calm
    # mid-day baseline. Per pages 3-4 (Vanna Ask), an IV expansion against a
    # dealer-short-put book is unambiguously bearish -- this is the same,
    # well-verified relationship as the Vanna Bid/Ask check above, now run
    # through the full WSS composite rather than F_VEX in isolation. ---
    N_full = 2e6  # standardizing scalar for this demo book (see the "N_scalar" note
                  # above -- scaled to the synthetic book's size, not a real ADDV, so
                  # the printed swing is legible rather than lost in tanh saturation).
    mid_session = datetime(2026, 7, 6, 11, 0)
    calm_wss, calm_flag = wss.score(
        spot=S0, gamma_flip=95.0, net_vex=vex_short_puts, delta_iv=-0.02,
        net_chex_at_wall=chex_far, delta_t_days=1.0, standardizing_scalar=N_full,
        now=mid_session, session_close=close,
    )
    iv_shock_wss, iv_shock_flag = wss.score(
        spot=S0, gamma_flip=95.0, net_vex=vex_short_puts, delta_iv=+0.15,
        net_chex_at_wall=chex_far, delta_t_days=1.0, standardizing_scalar=N_full,
        now=mid_session, session_close=close,
    )
    good = iv_shock_wss < calm_wss
    ok &= good
    print(f"  [{'OK' if good else 'FAIL'}] IV-expansion shock (VEX isolated) degrades WSS vs. calm "
          f"(calm={calm_wss:+.2f} [{calm_flag}], shock={iv_shock_wss:+.2f} [{iv_shock_flag}])")

    # --- Scenario 2: late-day 0DTE grind, isolated by holding delta_iv fixed
    # at 0 (no vol move) and only advancing the session clock from mid-day to
    # the final hour, letting Charm's near-expiry blow-up (already measured
    # above: chex_far vs. chex_near) and the dynamic omega_3 boost both feed
    # through. This checks MAGNITUDE (the well-established, unambiguous
    # claim: Color/Charm forces a far larger swing away from neutral as T->0
    # and the final-hour clock-weight kicks in), not a hardcoded sign --
    # Charm's sign right at the money as T->0 is itself parameter-dependent
    # (it's driven by sign(r-q) once d1,d2->0 -- verified analytically, not
    # asserted -- so a hardcoded bullish/bearish direction here would be
    # asserting more than the math actually guarantees). ---
    neutral_wss, _ = wss.score(
        spot=S0, gamma_flip=95.0, net_vex=0.0, delta_iv=0.0,
        net_chex_at_wall=0.0, delta_t_days=1.0, standardizing_scalar=N_full,
        now=mid_session, session_close=close,
    )
    midday_grind_wss, _ = wss.score(
        spot=S0, gamma_flip=95.0, net_vex=0.0, delta_iv=0.0,
        net_chex_at_wall=chex_far, delta_t_days=1.0, standardizing_scalar=N_full,
        now=mid_session, session_close=close,
    )
    late_day_grind_wss, late_day_flag = wss.score(
        spot=S0, gamma_flip=95.0, net_vex=0.0, delta_iv=0.0,
        net_chex_at_wall=chex_near, delta_t_days=1.0, standardizing_scalar=N_full,
        now=datetime(2026, 7, 6, 15, 55), session_close=close,
    )
    good = abs(late_day_grind_wss - neutral_wss) > abs(midday_grind_wss - neutral_wss) * 3
    ok &= good
    print(f"  [{'OK' if good else 'FAIL'}] late-day 0DTE grind swings WSS far more than the same book "
          f"mid-session (mid-day deviation={abs(midday_grind_wss - neutral_wss):.1f}, "
          f"final-hour deviation={abs(late_day_grind_wss - neutral_wss):.1f} [{late_day_flag}])")

    print("SELFTEST PHASE 2", "PASS" if ok else "FAIL")
    return ok


# =============================================================================
# Phase 3 self-test — V/OI Ghost Wall detection, term-structure T_state,
# rolling V_state, and the two mandated end-to-end scenarios: a healthy
# range-bound market and a synthetic high-panic breakdown.
# =============================================================================
def _selftest_phase3() -> bool:
    ok = True

    # --- Component 1: V/OI ratio + bid-side fraction + Ghost Wall flag ---
    n_contracts = 6
    prior_oi = np.array([100.0, 100.0, 300.0, 0.0, 500.0, 5.0])
    chain = ChainState(prior_oi=prior_oi, lam=1.5)
    # strike 0: heavy bid-side unwind (V/OI>1.05, mostly sells) -> Ghost Wall
    # strike 1: heavy ask-side accumulation at the SAME V/OI -> NOT a Ghost Wall
    # strike 5: thin strike (prior_oi=5) that clears the same raw V/OI+bid
    # rule off a trivial amount of volume -> should need the OI floor to filter
    contract_idx = np.array([0, 0, 0, 0, 1, 1, 1, 1, 2, 5, 5, 5])
    volume = np.array([50.0, 40.0, 30.0, 30.0, 50.0, 40.0, 30.0, 30.0, 10.0, 3.0, 3.0, 3.0])
    direction = np.array([-1.0, -1.0, -1.0, 1.0, 1.0, 1.0, 1.0, -1.0, 1.0, -1.0, -1.0, -1.0])
    chain.apply_ticks(contract_idx, volume, direction)
    v_oi = chain.v_oi_ratio
    bid_frac = bid_side_volume_fraction(contract_idx, direction, volume, n_contracts)
    ghost = flag_ghost_wall(v_oi, bid_frac)

    good = bool(v_oi[0] > 1.05 and bid_frac[0] > 0.70 and ghost[0])
    ok &= good
    print(f"  [{'OK' if good else 'FAIL'}] bid-side unwind strike flagged as Ghost Wall "
          f"(V/OI={v_oi[0]:.2f}, bid_frac={bid_frac[0]:.2f})")

    good = bool(v_oi[1] > 1.05 and bid_frac[1] < 0.70 and not ghost[1])
    ok &= good
    print(f"  [{'OK' if good else 'FAIL'}] ask-side accumulation at similar V/OI is NOT flagged "
          f"(V/OI={v_oi[1]:.2f}, bid_frac={bid_frac[1]:.2f})")

    ghost_floored = flag_ghost_wall(v_oi, bid_frac, established_oi=prior_oi, min_established_oi=100.0)
    good = bool(v_oi[5] > 1.05 and bid_frac[5] > 0.70 and ghost[5] and not ghost_floored[5])
    ok &= good
    print(f"  [{'OK' if good else 'FAIL'}] thin strike (prior_oi={prior_oi[5]:.0f}) clears the raw rule "
          f"but is suppressed once an established-OI floor is applied (V/OI={v_oi[5]:.2f})")

    good = bool(ghost_floored[0])
    ok &= good
    print(f"  [{'OK' if good else 'FAIL'}] the genuine established-OI hit (prior_oi={prior_oi[0]:.0f}) "
          f"still fires with the floor applied")

    # audit fix 2026-07-04: an UNTOUCHED zero-prior-OI strike is zero turnover
    # (0.0), not infinite turnover -- inf here would let F_state read 1.0 on a
    # strike where nothing traded. Volume against a zero base is still inf.
    good = v_oi[3] == 0.0
    ok &= good
    print(f"  [{'OK' if good else 'FAIL'}] untouched zero-prior-OI strike reads 0.0 turnover "
          f"(v_oi={v_oi[3]})")
    fresh = ChainState(prior_oi=np.array([0.0]))
    fresh.apply_ticks(np.array([0]), np.array([5.0]), np.array([1.0]))
    good = bool(np.isinf(fresh.v_oi_ratio[0]))
    ok &= good
    print(f"  [{'OK' if good else 'FAIL'}] volume against a zero-OI base still reads inf "
          f"(v_oi={fresh.v_oi_ratio[0]})")

    # --- Component 2: VIX/VXV term structure regimes + reliability discount ---
    contango = VolatilityTermStructureMonitor(vix=16.0, vxv=18.0)  # ratio 0.889 -> steep contango
    inverted = VolatilityTermStructureMonitor(vix=32.0, vxv=27.0)  # ratio 1.185 -> deep inversion
    good = (contango.regime == "STEEP_CONTANGO" and contango.t_state == 0.0
            and contango.gamma_wall_reliability == 1.0)
    ok &= good
    print(f"  [{'OK' if good else 'FAIL'}] steep contango: full gamma-wall reliability "
          f"(ratio={contango.ratio:.3f})")

    good = (inverted.regime == "BACKWARDATION" and inverted.t_state == 1.0
            and inverted.gamma_wall_reliability == 0.0)
    ok &= good
    print(f"  [{'OK' if good else 'FAIL'}] deep inversion: gamma-wall reliability downgraded to 0 "
          f"(ratio={inverted.ratio:.3f}, T_state={inverted.t_state:.2f})")

    # --- Component 3: rolling V_state tracker ---
    tracker = VexExtremeTracker(window=90)
    v1 = tracker.v_state(-100.0)   # first negative reading -> bootstraps to 1.0
    v2 = tracker.v_state(-50.0)    # smaller magnitude than the running max -> < 1.0
    v3 = tracker.v_state(-200.0)   # new all-time worst -> back to 1.0 (capped, not >1.0)
    good = (v1 == 1.0 and 0.0 < v2 < 1.0 and v3 == 1.0)
    ok &= good
    print(f"  [{'OK' if good else 'FAIL'}] rolling V_state: bootstrap={v1:.2f}, "
          f"smaller-than-max={v2:.2f}, new-worst-capped={v3:.2f}")

    # --- Scenario 1: healthy, range-bound market -> Alpha-1 or Beta-2 ---
    engine = CascadeBreakdownEngine()
    calm_tracker = VexExtremeTracker()
    calm_tracker.v_state(-5_000.0)  # seed a mild prior-history reading
    calm = engine.evaluate(
        spot=105.0, gamma_flip=95.0,             # positive gamma regime, spot well above flip
        vix_vxv_ratio=16.0 / 18.0,                # steep contango
        v_oi=0.3, bid_dominant=False,             # quiet, balanced flow
        vex=2_000.0,                              # supportive (positive) VEX
        vex_tracker=calm_tracker,
    )
    good = calm.rule.startswith(("Alpha-1", "Beta-2")) and calm.p_c < 0.20 and not calm.hard_pivot
    ok &= good
    print(f"  [{'OK' if good else 'FAIL'}] Scenario 1 (healthy/range-bound): P(C)={calm.p_c:.3f}, "
          f"rule={calm.rule}, hard_pivot={calm.hard_pivot}")

    # --- Scenario 2: synthetic high-panic environment -> Ghost Wall + hard pivot ---
    panic_tracker = VexExtremeTracker()
    panic_tracker.v_state(-20_000.0)  # a rolling-max prior reading to normalize against
    panic = engine.evaluate(
        spot=88.0, gamma_flip=95.0,               # negative gamma regime, spot below flip
        vix_vxv_ratio=1.12,                       # deep VIX/VXV inversion (>1.05)
        v_oi=1.8, bid_dominant=True,              # heavy bid-side put unwinding, V/OI>1.5
        vex=-90_000.0,                            # deeply negative VEX
        vex_tracker=panic_tracker,
    )
    good = (panic.p_c > 0.75 and panic.hard_pivot and panic.ghost_wall
            and panic.rule.startswith("Delta-4"))
    ok &= good
    print(f"  [{'OK' if good else 'FAIL'}] Scenario 2 (high-panic): P(C)={panic.p_c:.3f} "
          f"(G={panic.g_state:.2f} T={panic.t_state:.2f} F={panic.f_state:.2f} V={panic.v_state:.2f}), "
          f"rule={panic.rule}, hard_pivot={panic.hard_pivot}, ghost_wall={panic.ghost_wall}")

    print("SELFTEST PHASE 3", "PASS" if ok else "FAIL")
    return ok


# =============================================================================
# Phase 4 self-test -- CVD/OFI filters, expiration routing, and the two
# mandated end-to-end scenarios: a Put Wall test with bullish CVD divergence,
# and a Ghost Wall breakdown leveraging Phase 3's real P(C) > 0.75 output.
# =============================================================================
def _selftest_phase4() -> bool:
    ok = True

    # --- Component 1: CVD divergence / trend-alignment / stacked imbalances / absorption ---
    bullish_price = np.array([100.0, 99.7, 99.5, 99.6, 99.4, 99.1, 98.8, 99.0])   # lower low
    bullish_cvd = np.array([500.0, 450.0, 400.0, 420.0, 450.0, 470.0, 500.0, 510.0])  # higher low
    good = detect_price_cvd_divergence(bullish_price, bullish_cvd, kind="BULLISH_CLASSIC", lookback=8)
    ok &= good
    print(f"  [{'OK' if good else 'FAIL'}] BULLISH_CLASSIC divergence detected on a stop-hunt pattern")

    trend_price = np.array([100.0, 99.0, 98.0, 97.0, 96.0, 94.0, 92.0, 90.0])     # lower low
    trend_cvd = np.array([500.0, 400.0, 300.0, 200.0, 100.0, 0.0, -100.0, -200.0])  # ALSO lower low
    good = (not detect_price_cvd_divergence(trend_price, trend_cvd, kind="BULLISH_CLASSIC", lookback=8)
            and confirm_trend_alignment(trend_price, trend_cvd, trend="DOWN", lookback=8))
    ok &= good
    print(f"  [{'OK' if good else 'FAIL'}] genuine capitulation (CVD confirms price) is NOT flagged as a stop-hunt")

    good = detect_stacked_imbalances(np.array([1.0, 1.2, 3.5, 4.0, 3.2, 1.1]), min_consecutive=3, ratio_threshold=3.0)
    ok &= good
    print(f"  [{'OK' if good else 'FAIL'}] 3 consecutive >=3:1 levels flagged as stacked imbalances")

    good = not detect_stacked_imbalances(np.array([1.0, 3.5, 1.0, 3.5, 1.0]), min_consecutive=3, ratio_threshold=3.0)
    ok &= good
    print(f"  [{'OK' if good else 'FAIL'}] isolated (non-consecutive) imbalance spikes are NOT flagged")

    good = detect_absorption(price_change_pct=0.0005, cvd_surge=50_000.0, cvd_surge_threshold=10_000.0)
    ok &= good
    print(f"  [{'OK' if good else 'FAIL'}] stalled price + massive CVD surge flagged as absorption (bull trap)")

    # --- Component 2: dynamic expiration selection ---
    dtes = np.array([0, 1, 2, 3, 5, 7, 14, 30])
    good = (select_optimal_expiration(dtes, 1, 3) == 1       # Mean-Reversion -> nearest in [1,3]
            and select_optimal_expiration(dtes, 0, 0) == 0    # Momentum Long -> strict 0DTE
            and select_optimal_expiration(np.array([2, 5, 7]), 0, 1) == 2)  # no 0DTE -> nearest weekly fallback
    ok &= good
    print(f"  [{'OK' if good else 'FAIL'}] expiration routing: in-range nearest, strict 0DTE, and weekly fallback")

    # --- Scenario 1: Put Wall test + bullish CVD divergence -> MEAN_REVERSION_LONG, >0DTE ---
    engine = AdvancedGammaExecutionEngine(cvd_lookback=8)
    mr_long_state = ExecutionState(
        spot=99.8, put_wall=100.0, call_wall=110.0, gamma_flip=104.0,
        net_gex=50_000.0, net_vex=5_000.0, net_chex=-1_000.0,
        vgex_ratio=0.3, term_structure="CONTANGO", atm_iv_trend="FLAT",
        price_series=bullish_price, cvd_series=bullish_cvd,
        volume=500_000.0, avg_volume_20=600_000.0, adv=5_000_000.0, poc=101.0,
        strikes=np.array([90.0, 95.0, 100.0, 105.0, 110.0]),
        oi_by_strike=np.array([1000.0, 2000.0, 3000.0, 2500.0, 1800.0]),
        gex_by_strike=np.array([500.0, 1500.0, 2500.0, 2000.0, 1200.0]),
        available_dtes=dtes,
    )
    result1 = engine.evaluate_mean_reversion_long(mr_long_state)
    good = (result1["status"] == "EXECUTE" and result1["strategy"] == "MEAN_REVERSION_LONG"
            and result1["expiration_dte"] is not None and result1["expiration_dte"] >= 1)
    ok &= good
    print(f"  [{'OK' if good else 'FAIL'}] Scenario 1 (Put Wall + bullish divergence): {result1['status']} "
          f"{result1.get('strategy')}, expiration_dte={result1.get('expiration_dte')} (>0DTE routing)")

    # --- Scenario 2: Ghost Wall breakdown, leveraging Phase 3's real P(C) > 0.75 ---
    cascade = CascadeBreakdownEngine()
    panic_tracker = VexExtremeTracker()
    panic_tracker.v_state(-20_000.0)
    assessment = cascade.evaluate(
        spot=88.0, gamma_flip=95.0, vix_vxv_ratio=1.12,
        v_oi=1.8, bid_dominant=True, vex=-90_000.0, vex_tracker=panic_tracker,
    )
    good = assessment.p_c > 0.75 and assessment.hard_pivot
    ok &= good
    print(f"  [{'OK' if good else 'FAIL'}] Phase 3 CascadeBreakdownEngine confirms the Ghost Wall: "
          f"P(C)={assessment.p_c:.3f}, hard_pivot={assessment.hard_pivot}")

    trapdoor_state = ExecutionState(
        spot=88.0, put_wall=95.0, call_wall=110.0, gamma_flip=95.0,
        net_gex=-80_000.0, net_vex=-90_000.0, net_chex=3_000.0,
        vgex_ratio=0.4, term_structure="BACKWARDATION", atm_iv_trend="RISING",
        price_series=trend_price, cvd_series=trend_cvd,
        volume=2_000_000.0, avg_volume_20=1_000_000.0, adv=5_000_000.0, poc=90.0,
        strikes=np.array([80.0, 85.0, 90.0, 95.0, 100.0]),
        oi_by_strike=np.array([2000.0, 1500.0, 800.0, 3000.0, 1200.0]),
        gex_by_strike=np.array([1000.0, 900.0, 400.0, 3500.0, 1000.0]),
        available_dtes=np.array([0, 1, 2, 3, 5, 7]),
        p_c=assessment.p_c, hard_pivot=assessment.hard_pivot,
    )
    result2 = engine.evaluate_momentum_short_trapdoor(trapdoor_state)
    good = (result2["status"] == "EXECUTE" and result2["strategy"] == "MOMENTUM_SHORT_TRAPDOOR"
            and result2["expiration_dte"] == 0)
    ok &= good
    print(f"  [{'OK' if good else 'FAIL'}] Scenario 2 (Ghost Wall breakdown): {result2['status']} "
          f"{result2.get('strategy')}, expiration_dte={result2.get('expiration_dte')} (0DTE routing), "
          f"target={result2.get('target')}, size_multiplier={result2.get('size_multiplier'):.6f}")

    # --- evaluate() dispatch sanity: the trapdoor state should not fire the MR gates ---
    dispatched = engine.evaluate(trapdoor_state)
    good = dispatched["strategy"] == "MOMENTUM_SHORT_TRAPDOOR"
    ok &= good
    print(f"  [{'OK' if good else 'FAIL'}] evaluate() dispatch picks the correct gate ({dispatched['strategy']})")

    # --- audit coverage additions (2026-07-04): the two previously untested
    # EXECUTE paths (MR-short, squeeze) and the absorption abort branch ---
    bearish_price = np.array([100.0, 101.0, 102.0, 101.5, 102.5, 103.0, 102.8, 102.6])  # higher high
    bearish_cvd = np.array([500.0, 600.0, 700.0, 650.0, 620.0, 640.0, 660.0, 655.0])    # lower high
    mr_short_state = ExecutionState(
        spot=109.8, put_wall=100.0, call_wall=110.0, gamma_flip=104.0,
        net_gex=50_000.0, net_vex=5_000.0, net_chex=2_000.0,
        vgex_ratio=0.4, term_structure="CONTANGO", atm_iv_trend="FLAT",
        price_series=bearish_price, cvd_series=bearish_cvd,
        volume=500_000.0, avg_volume_20=600_000.0, adv=5_000_000.0, poc=106.0,
        strikes=np.array([90.0, 95.0, 100.0, 105.0, 110.0]),
        oi_by_strike=np.array([1000.0, 2000.0, 3000.0, 2500.0, 1800.0]),
        gex_by_strike=np.array([500.0, 1500.0, 2500.0, 2000.0, 1200.0]),
        available_dtes=dtes, is_friday_close=True,
    )
    r3 = engine.evaluate_mean_reversion_short(mr_short_state)
    good = (r3["status"] == "EXECUTE" and r3["strategy"] == "MEAN_REVERSION_SHORT"
            and r3["target"] == 106.0                      # max(flip 104, poc 106) = closer-to-entry level
            and r3["size_multiplier"] == 1.5               # Friday-close weekend-Charm sizing
            and r3["expiration_dte"] is not None and 1 <= r3["expiration_dte"] <= 3)
    ok &= good
    print(f"  [{'OK' if good else 'FAIL'}] MR-short EXECUTE path: target={r3.get('target')}, "
          f"friday sizing={r3.get('size_multiplier')}, dte={r3.get('expiration_dte')}")

    squeeze_price = np.array([108.0, 108.5, 109.0, 109.5, 110.2, 110.8, 111.2, 111.6])
    squeeze_cvd = np.array([100.0, 300.0, 500.0, 700.0, 1000.0, 1400.0, 1900.0, 2500.0])
    squeeze_state = ExecutionState(
        spot=111.6, put_wall=100.0, call_wall=110.0, gamma_flip=105.0,
        net_gex=-60_000.0, net_vex=-40_000.0, net_chex=0.0,
        vgex_ratio=0.7, term_structure="BACKWARDATION", atm_iv_trend="RISING",
        price_series=squeeze_price, cvd_series=squeeze_cvd,
        volume=2_000_000.0, avg_volume_20=1_000_000.0, adv=5_000_000.0, poc=109.0,
        strikes=np.array([100.0, 105.0, 110.0, 115.0, 120.0]),
        oi_by_strike=np.array([1000.0, 2000.0, 4000.0, 900.0, 100.0]),
        gex_by_strike=np.array([1000.0, 3000.0, 4000.0, 800.0, 50.0]),
        available_dtes=np.array([0, 1, 2]),
        ofi_by_level=np.array([3.5, 4.2, 3.1, 2.0]),
    )
    r4 = engine.evaluate_momentum_long_squeeze(squeeze_state)
    good = (r4["status"] == "EXECUTE" and r4["strategy"] == "MOMENTUM_LONG_SQUEEZE"
            and r4["expiration_dte"] == 0
            and r4["target"] == 120.0)   # gamma exhaustion: 50 < 10% of the 800 running peak
    ok &= good
    print(f"  [{'OK' if good else 'FAIL'}] squeeze EXECUTE path: target(exhaustion node)="
          f"{r4.get('target')}, dte={r4.get('expiration_dte')} (strict 0DTE)")

    # absorption abort: marginal higher high on a stalled tape + massive CVD
    # surge (buyers swallowed by icebergs) must HOLD, not chase the breakout
    stall_price = np.array([111.00, 111.02, 111.03, 111.04, 111.045, 111.05, 111.055, 111.06])
    stall_cvd = np.array([0.0, 5e5, 1e6, 1.5e6, 2e6, 2.4e6, 2.8e6, 3.2e6])
    absorption_state = ExecutionState(
        spot=111.06, put_wall=100.0, call_wall=110.0, gamma_flip=105.0,
        net_gex=-60_000.0, net_vex=-40_000.0, net_chex=0.0,
        vgex_ratio=0.7, term_structure="BACKWARDATION", atm_iv_trend="RISING",
        price_series=stall_price, cvd_series=stall_cvd,
        volume=2_000_000.0, avg_volume_20=1_000_000.0, adv=5_000_000.0, poc=109.0,
        strikes=squeeze_state.strikes, oi_by_strike=squeeze_state.oi_by_strike,
        gex_by_strike=squeeze_state.gex_by_strike,
        available_dtes=np.array([0, 1, 2]),
        ofi_by_level=np.array([3.5, 4.2, 3.1, 2.0]),
    )
    r5 = engine.evaluate_momentum_long_squeeze(absorption_state)
    good = r5["status"] == "HOLD_STATE" and "absorption" in r5["reason"]
    ok &= good
    print(f"  [{'OK' if good else 'FAIL'}] absorption abort: {r5['status']} ({r5['reason']})")

    print("SELFTEST PHASE 4", "PASS" if ok else "FAIL")
    return ok


if __name__ == "__main__":
    import sys

    phase1_ok = _selftest()
    phase2_ok = _selftest_phase2()
    phase3_ok = _selftest_phase3()
    phase4_ok = _selftest_phase4()
    sys.exit(0 if (phase1_ok and phase2_ok and phase3_ok and phase4_ok) else 1)
