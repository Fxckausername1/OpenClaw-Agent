"""BT-2 historical contract selector -- roadmap Section 6.

Point-in-time only: given a book of contract-quote observations already
known as of decision_ts (built by build_point_in_time_book from BT-1-shaped
classified trade_quote history), applies a configurable rule set and
returns either the single best candidate or NO CONTRACT. Never forces a
worse candidate to produce a fill -- Section 16's "No preferred-premium
contract exists" scenario requires returning NO TRADE, not walking further
OTM.

No lookahead is possible from inside this module: build_point_in_time_book
filters to trade_timestamp <= decision_ts BEFORE any row reaches
evaluate_candidate/select_contract. When the caller supplies the underlying
price observed at decision_ts, delta is derived from that spot and the latest
eligible option midpoint; headline B1 never loads the EOD Greek snapshot.


Deliberately a SEPARATE implementation from contract_quote.py's live CB-V4
Section 8 screen, not a reuse of its _evaluate_candidate: that screen
treats premium band as "preference only, never a hard gate" (its own
docstring), while roadmap Section 6 lists "ask in preferred premium band"
as one of the selector's own pass/fail rules, and Section 16 requires NO
TRADE (not OTM-walking) when nothing in-band exists. These are genuinely
different gate semantics for a genuinely different purpose (backtest
selection vs live shadow screening) -- keeping them as separate modules
avoids one silently drifting to match the other's behavior as either
evolves. Both stay in the same CB-V4 PASS/WAIT/FAIL vocabulary and the same
general shape (evaluate one candidate -> reasons list -> rank the passing
set), so a reader who already knows contract_quote.py can follow this
module without re-learning the pattern.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import math
from typing import Optional
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import greeks as bs

from .schemas import normalize_right

ET = ZoneInfo("America/New_York")
SECONDS_PER_YEAR = 365.0 * 24.0 * 60.0 * 60.0


def time_to_expiry_years(expiration, decision_ts) -> float:
    """Calendar time from decision_ts to the 16:00 ET option expiry."""
    expiration = expiration if isinstance(expiration, dt.date) else dt.date.fromisoformat(str(expiration)[:10])
    decision = pd.Timestamp(decision_ts)
    decision = decision.tz_localize(ET) if decision.tzinfo is None else decision.tz_convert(ET)
    expiry_close = pd.Timestamp(dt.datetime.combine(expiration, dt.time(16, 0), tzinfo=ET))
    return max((expiry_close - decision).total_seconds(), 1.0) / SECONDS_PER_YEAR


def point_in_time_delta(mid, underlying_price, strike, expiration, decision_ts, right, rate=0.05):
    """Derive delta using only values observable at decision_ts.

    Returns None when the quote/spot cannot support a finite Black-Scholes
    implied-volatility solution. It never substitutes an EOD Greek.
    """
    try:
        price = float(mid)
        spot = float(underlying_price)
        strike = float(strike)
        if not all(math.isfinite(v) and v > 0 for v in (price, spot, strike)):
            return None
        is_call = normalize_right(right) == "C"
        t_years = time_to_expiry_years(expiration, decision_ts)
        iv = float(bs.implied_vol(price, spot, strike, t_years, float(rate), is_call))
        if not math.isfinite(iv):
            return None
        delta = float(bs.bs_greeks(spot, strike, t_years, float(rate), iv, is_call)["delta"])
        return delta if math.isfinite(delta) else None
    except (TypeError, ValueError, OverflowError):
        return None


@dataclasses.dataclass(frozen=True)
class SelectorConfig:
    """Every threshold here is an initial research judgment call, not yet
    calibrated against any real BT-2 outcome data -- same honesty
    convention as contract_quote.py's own CB-V4 constants and
    bt1_manifest.py's grading thresholds. Revisit once real trades exist to
    study (roadmap Section 10 / BT-3 explicitly wants these swept as a
    parameter grid; BT-2 itself only needs this one reasonable default).

    Deliberately WIDER than contract_quote.py's live CB-V4 constants in a
    few places (spread ceiling, quote-age ceiling): this reads from BT-1's
    trade-associated NBBO history, which is sparser than a live continuous
    snapshot, so an identically tight live ceiling would starve the
    backtest of any candidates at all on quieter strikes. Flagged to heff
    as a judgment call, not a proven-correct number."""
    allowed_dte: frozenset = frozenset({0, 1, 2})
    premium_low: float = 0.20
    premium_high: float = 0.30
    max_spread_dollars: float = 0.05
    max_spread_pct_mid: float = 0.15
    min_abs_delta: float = 0.15
    max_quote_age_seconds: float = 10.0
    min_ask_size: float = 5.0
    target_delta: float = 0.35


NO_CONTRACT_REASON_NO_CANDIDATES = "no_candidates_in_book"
NO_CONTRACT_REASON_NONE_PASS = "no_candidate_passed_all_rules"


@dataclasses.dataclass
class SelectionResult:
    contract: Optional[dict]
    candidates_checked: int
    reason: Optional[str]

    @property
    def found(self) -> bool:
        return self.contract is not None


def build_point_in_time_book(
    trades: pd.DataFrame, decision_ts, greeks: Optional[dict] = None,
    underlying_price: Optional[float] = None, rate: float = 0.05,
) -> pd.DataFrame:
    """Reconstructs a per-contract 'as of decision_ts' quote book from
    BT-1-shaped classified trade_quote history: the most recent trade-
    associated NBBO (bid/ask/bid_size/ask_size) at or before decision_ts
    for each contract_id, with quote_age_seconds computed against that same
    timestamp. `trades` must already carry contract_id/strike/right/
    expiration (i.e. already run through normalize.classify_trades) --
    this function only reads those columns, it never derives them.

    STRICTLY point-in-time: filters to trade_timestamp <= decision_ts
    BEFORE grouping, so no future row can influence a contract's book entry
    regardless of what the caller passes in.

    Known limitation, documented rather than papered over: this proxies
    quote history from trade-associated NBBO only -- this codebase has
    never ingested a continuous quote-tick stream, live or historical. A
    contract with no recent trades will correctly show a stale
    quote_age_seconds here, which is exactly what the selector's
    max_quote_age_seconds gate exists to catch, not a bug to work around."""
    if trades is None or trades.empty:
        return pd.DataFrame()
    decision_ts = pd.Timestamp(decision_ts)
    eligible = trades[trades["trade_timestamp"] <= decision_ts]
    if eligible.empty:
        return pd.DataFrame()
    latest = (
        eligible.sort_values("trade_timestamp")
        .groupby("contract_id", as_index=False)
        .last()
    )
    latest = latest.copy()
    latest["quote_age_seconds"] = (decision_ts - latest["trade_timestamp"]).dt.total_seconds()
    if underlying_price is not None:
        try:
            spot = float(underlying_price)
        except (TypeError, ValueError):
            spot = float("nan")
        mids = (
            pd.to_numeric(latest["bid"], errors="coerce")
            + pd.to_numeric(latest["ask"], errors="coerce")
        ).to_numpy(dtype=float) / 2.0
        strikes = pd.to_numeric(latest["strike"], errors="coerce").to_numpy(dtype=float)
        t_years = np.asarray([
            time_to_expiry_years(exp, decision_ts) for exp in latest["expiration"]
        ], dtype=float)
        is_calls = np.asarray([normalize_right(right) == "C" for right in latest["right"]])
        deltas = np.full(len(latest), np.nan)
        valid = np.isfinite(mids) & (mids > 0) & np.isfinite(strikes) & (strikes > 0) & math.isfinite(spot) & (spot > 0)
        if valid.any():
            with np.errstate(all="ignore"):
                iv = np.asarray(bs.implied_vol(mids, spot, strikes, t_years, float(rate), is_calls), dtype=float)
                solved = valid & np.isfinite(iv)
                if solved.any():
                    deltas[solved] = bs.bs_greeks(spot, strikes[solved], t_years[solved], float(rate), iv[solved], is_calls[solved])["delta"]
        latest["delta"] = [float(value) if math.isfinite(value) else None for value in deltas]
    elif greeks:
        # Compatibility only for older research callers. Headline B1 passes
        # an intraday underlying_price and never supplies EOD Greeks.
        latest["delta"] = latest["contract_id"].map(lambda cid: (greeks.get(cid) or {}).get("delta"))
    elif "delta" not in latest.columns:
        latest["delta"] = None
    return latest


def _dte(expiration: dt.date, decision_date: dt.date) -> int:
    return (expiration - decision_date).days


def evaluate_candidate(row, decision_date: dt.date, config: SelectorConfig) -> dict:
    """Evaluates one point-in-time book row against every Section 6 rule.
    Always returns every reason a rule failed (never short-circuits after
    the first), so a NO TRADE outcome can be audited for exactly why, same
    diagnostic-completeness convention as contract_quote._evaluate_candidate."""
    right = normalize_right(row["right"])
    expiration = row["expiration"]
    if not isinstance(expiration, dt.date):
        expiration = dt.date.fromisoformat(str(expiration)[:10])
    bid, ask = row.get("bid"), row.get("ask")

    if bid is None or ask is None or pd.isna(bid) or pd.isna(ask) or bid <= 0 or ask <= 0 or bid >= ask:
        return {
            "passed": False, "reasons": ["no valid two-sided quote (missing/zero/crossed)"],
            "strike": float(row["strike"]), "right": right, "expiration": expiration.isoformat(),
            "_delta_distance": float("inf"), "spread_pct_mid": float("inf"),
        }

    mid = (bid + ask) / 2.0
    spread = ask - bid
    spread_pct = spread / mid if mid else None
    dte = _dte(expiration, decision_date)
    reasons = []

    if dte not in config.allowed_dte:
        reasons.append(f"DTE {dte} not in allowed set {sorted(config.allowed_dte)}")
    if not (config.premium_low <= ask <= config.premium_high):
        reasons.append(f"ask ${ask:.2f} outside preferred premium band ${config.premium_low:.2f}-${config.premium_high:.2f}")
    if spread > config.max_spread_dollars + 1e-9:
        reasons.append(f"spread ${spread:.2f} exceeds ${config.max_spread_dollars:.2f} limit")
    if spread_pct is None or spread_pct > config.max_spread_pct_mid:
        reasons.append(f"spread {spread_pct:.1%} exceeds {config.max_spread_pct_mid:.0%} of mid" if spread_pct is not None else "spread pct unavailable")
    delta = row.get("delta")
    if delta is None or (isinstance(delta, float) and pd.isna(delta)):
        reasons.append("delta unavailable")
        delta = None
    elif abs(delta) < config.min_abs_delta:
        reasons.append(f"|delta| {abs(delta):.2f} below {config.min_abs_delta} floor")
    quote_age = row.get("quote_age_seconds")
    if quote_age is None or pd.isna(quote_age) or quote_age > config.max_quote_age_seconds:
        reasons.append(f"quote age {quote_age} exceeds {config.max_quote_age_seconds}s ceiling")
    ask_size = row.get("ask_size")
    if ask_size is None or pd.isna(ask_size) or ask_size < config.min_ask_size:
        reasons.append(f"displayed ask size {ask_size} below {config.min_ask_size} liquidity floor")

    return {
        "passed": not reasons, "reasons": reasons,
        "strike": float(row["strike"]), "right": right, "expiration": expiration.isoformat(),
        "bid": round(float(bid), 4), "ask": round(float(ask), 4), "mid": round(mid, 4),
        "spread": round(spread, 4), "spread_pct_mid": round(spread_pct, 4) if spread_pct is not None else float("inf"),
        "delta": round(float(delta), 4) if delta is not None else None,
        "quote_age_seconds": None if quote_age is None or pd.isna(quote_age) else round(float(quote_age), 2),
        "ask_size": None if ask_size is None or pd.isna(ask_size) else float(ask_size),
        "dte": dte,
        "_delta_distance": abs(abs(delta) - config.target_delta) if delta is not None else float("inf"),
    }


def _quality_key(row: dict) -> tuple:
    """Lower is better. Spread-pct primary ('highest quality' per Section
    6), then closeness to target_delta as the documented tie-break
    ('choose highest quality, then closest to target delta')."""
    return (row["spread_pct_mid"], row["_delta_distance"])


def select_contract(
    book: pd.DataFrame, right: str, decision_ts, config: SelectorConfig = SelectorConfig(),
) -> SelectionResult:
    """`book` must already be point-in-time safe (build_point_in_time_book's
    output) -- this function has no timestamp column of its own to filter
    against and trusts that invariant, exactly like
    contract_quote.screen_candidates trusts its own snapshot input."""
    if book is None or book.empty:
        return SelectionResult(contract=None, candidates_checked=0, reason=NO_CONTRACT_REASON_NO_CANDIDATES)

    right = normalize_right(right)
    subset = book[book["right"].map(normalize_right) == right]
    if subset.empty:
        return SelectionResult(contract=None, candidates_checked=0, reason=NO_CONTRACT_REASON_NO_CANDIDATES)

    decision_date = pd.Timestamp(decision_ts).date()
    evaluated = [evaluate_candidate(row, decision_date, config) for _, row in subset.iterrows()]
    passing = [c for c in evaluated if c["passed"]]
    if not passing:
        return SelectionResult(contract=None, candidates_checked=len(evaluated), reason=NO_CONTRACT_REASON_NONE_PASS)

    best = dict(min(passing, key=_quality_key))
    best.pop("_delta_distance", None)
    return SelectionResult(contract=best, candidates_checked=len(evaluated), reason=None)


def select_cheapest_passing_contract(
    book: pd.DataFrame, right: str, decision_ts, config: SelectorConfig = SelectorConfig(),
) -> SelectionResult:
    """Adversarial-test helper ONLY (roadmap Section 16: 'Force the
    selector to pick the cheapest contract available and compare simulated
    damage/friction vs the real selector's choice'). Deliberately ranks
    passing candidates by lowest ask instead of select_contract's
    quality/delta-closeness ranking. Never called by bt2_simulator.py's
    real trade path -- exists so tests can quantify how much worse a
    cheapest-contract-first policy would perform."""
    if book is None or book.empty:
        return SelectionResult(contract=None, candidates_checked=0, reason=NO_CONTRACT_REASON_NO_CANDIDATES)

    right = normalize_right(right)
    subset = book[book["right"].map(normalize_right) == right]
    if subset.empty:
        return SelectionResult(contract=None, candidates_checked=0, reason=NO_CONTRACT_REASON_NO_CANDIDATES)

    decision_date = pd.Timestamp(decision_ts).date()
    evaluated = [evaluate_candidate(row, decision_date, config) for _, row in subset.iterrows()]
    passing = [c for c in evaluated if c["passed"]]
    if not passing:
        return SelectionResult(contract=None, candidates_checked=len(evaluated), reason=NO_CONTRACT_REASON_NONE_PASS)

    best = dict(min(passing, key=lambda c: c["ask"]))
    best.pop("_delta_distance", None)
    return SelectionResult(contract=best, candidates_checked=len(evaluated), reason=None)
