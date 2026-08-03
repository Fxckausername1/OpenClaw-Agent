"""On-demand contract-viability screen -- CB-V4 Section 8.

Unlike every other module in this package, this one is never cron'd: it runs
once, on demand, at Catalyst Brief build time, against whichever direction
the Control Map's verdict already points to (CALL WATCH -> calls, PUT WATCH
-> puts). Two snapshot calls per (symbol, expiration) -- option_snapshot_quote
for live NBBO/size, option_snapshot_greeks_first_order for delta/IV -- both
already used elsewhere in this codebase (collector.py uses the greeks call;
this is the first caller of the quote snapshot). Both are single on-demand
pulls across a bounded near-money strike window, not the ~1.2M-row windowed
trade+quote stream that required incremental aggregation -- no new
memory-safety work needed here.

heff signed off on building this specific piece (2026-07-26) because it is
the first CB-V4 deliverable that could actually inform a real manual trade,
unlike the diagnostic-only Control Map/Ghost Wall/CVD/IV-skew work that
shipped before it. manual_options_brief.py's Alpaca-sourced screen is
untouched and remains the only thing feeding real contract decisions today;
this module's output is surfaced only as a shadow addendum in
catalyst_brief.py, same isolation contract as the rest of TD-STD/CB-V4.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any, Optional
from zoneinfo import ZoneInfo

import pandas as pd

from .client import ThetaDataUnavailable, bounded_call, get_client
from .collector import _strike_range_for_universe
from .schemas import normalize_right

ET = ZoneInfo("America/New_York")
logger = logging.getLogger("thetadata_pkg.contract_quote")

# CB-V4 Section 8's gate table. Premium is explicitly "preference only", not
# a hard gate -- everything else here is a real pass/fail threshold, carried
# over from the same research-guess status as aggregate.py's wall thresholds
# (initial values from the CB-V4 doc, not yet TD-5-calibrated).
MAX_QUOTE_AGE_LIVE_SECONDS = 5.0
MAX_SPREAD_DOLLARS = 0.02
MAX_SPREAD_PCT_OF_MID = 0.10
MIN_ABS_DELTA = 0.10
MAX_FRICTION_RATIO = 0.35
GROSS_TARGET_RETURN = 0.25
PREFERRED_PREMIUM_LOW = 0.20
PREFERRED_PREMIUM_HIGH = 0.30

GATE_PASS, GATE_WAIT, GATE_FAIL = "PASS", "WAIT", "FAIL"
_GATE_RANK = {GATE_PASS: 0, GATE_WAIT: 1, GATE_FAIL: 2}

# CB-V4 Section 5's solid wall states -- a GHOST_CANDIDATE or FRAGILE wall is
# deliberately not treated as a real obstruction; that distinction is the
# entire point of the ghost-wall state machine.
_SOLID_WALL_STATES = frozenset({"REINFORCED", "STABLE"})


class ContractScreenUnavailable(RuntimeError):
    """Raised when there is no usable universe/expiration to screen at all
    (DATA BLOCKED for the whole card, not a single candidate's FAIL)."""


def fetch_near_money_book(
    symbol: str, expiration: dt.date, universe: dict, now: dt.datetime
) -> pd.DataFrame:
    """One quote snapshot + one greeks snapshot across the whole near-money
    window, merged on (strike, right). Mirrors collector.collect_greeks's
    call convention exactly (strike='*', right='both', same strike_range
    helper) so this never pulls a full chain."""
    client = get_client()
    strike_range = _strike_range_for_universe(universe)
    try:
        quote_df = bounded_call(
            client.option_snapshot_quote,
            symbol=symbol, expiration=expiration, strike="*", right="both",
            strike_range=strike_range,
        )
    except ThetaDataUnavailable:
        raise
    if quote_df is None or quote_df.empty:
        return pd.DataFrame()

    quote_df = quote_df.copy()
    quote_df["right"] = quote_df["right"].map(normalize_right)
    quote_df["strike"] = quote_df["strike"].astype(float)
    quote_df["quote_age_seconds"] = (
        pd.Timestamp(now) - quote_df["timestamp"]
    ).dt.total_seconds()
    book = quote_df[[
        "strike", "right", "bid", "ask", "bid_size", "ask_size",
        "timestamp", "quote_age_seconds",
    ]].copy()

    try:
        greeks_df = bounded_call(
            client.option_snapshot_greeks_first_order,
            symbol=symbol, expiration=expiration, strike="*", right="both",
            strike_range=strike_range,
        )
    except ThetaDataUnavailable as exc:
        logger.warning("greeks snapshot failed for %s %s: %s", symbol, expiration, exc)
        greeks_df = None

    if greeks_df is not None and not greeks_df.empty:
        greeks_df = greeks_df.copy()
        greeks_df["right"] = greeks_df["right"].map(normalize_right)
        greeks_df["strike"] = greeks_df["strike"].astype(float)
        book = book.merge(
            greeks_df[["strike", "right", "delta", "implied_vol"]],
            on=["strike", "right"], how="left",
        )
    else:
        book["delta"] = None
        book["implied_vol"] = None

    return book


def _runway_check(strike: float, spot: float, right: str, wall_records: list[dict]) -> dict[str, Any]:
    """Section 8's 'Runway' gate: is a dealer-defended same-right wall
    sitting between spot and this strike? Initial research heuristic, not
    yet TD-5-calibrated -- same caveat as the wall-state thresholds it
    reads."""
    if not wall_records or spot is None:
        return {"blocked": False, "reason": None}
    lo, hi = (spot, strike) if strike >= spot else (strike, spot)
    obstructions = [
        w for w in wall_records
        if w.get("right") == right
        and w.get("wall_state") in _SOLID_WALL_STATES
        and w.get("strike") is not None
        and lo < float(w["strike"]) < hi
    ]
    if not obstructions:
        return {"blocked": False, "reason": None}
    nearest = min(obstructions, key=lambda w: abs(float(w["strike"]) - spot))
    return {
        "blocked": True,
        "reason": (
            f"{nearest['wall_state']} {right} wall at {nearest['strike']} "
            "sits between spot and this strike"
        ),
    }


def _evaluate_candidate(row: Any, spot: float, right: str, wall_records: list[dict]) -> dict[str, Any]:
    bid, ask = row.bid, row.ask
    if bid is None or ask is None or bid <= 0 or ask <= bid:
        return {
            "gate": GATE_FAIL, "reasons": ["no valid two-sided quote"],
            "strike": float(row.strike), "right": right,
        }

    mid = (bid + ask) / 2.0
    spread = ask - bid
    spread_pct = spread / mid if mid else None
    quote_age = row.quote_age_seconds
    delta = row.delta if row.delta is not None and pd.notna(row.delta) else None
    bid_size, ask_size = row.bid_size, row.ask_size

    reasons: list[str] = []
    freshness_only_issue = False

    if quote_age is None or pd.isna(quote_age) or quote_age > MAX_QUOTE_AGE_LIVE_SECONDS:
        reasons.append(
            f"quote age {quote_age:.1f}s exceeds {MAX_QUOTE_AGE_LIVE_SECONDS}s live-decision ceiling"
            if quote_age is not None and not pd.isna(quote_age) else "quote timestamp unavailable"
        )
        freshness_only_issue = True

    if spread > MAX_SPREAD_DOLLARS + 1e-9:
        reasons.append(f"spread ${spread:.2f} exceeds ${MAX_SPREAD_DOLLARS:.2f} ceiling")
        freshness_only_issue = False
    if spread_pct is None or spread_pct > MAX_SPREAD_PCT_OF_MID:
        reasons.append("spread exceeds 10% of midpoint")
        freshness_only_issue = False
    if delta is None:
        reasons.append("delta unavailable (Standard first-order snapshot)")
        freshness_only_issue = False
    elif abs(delta) < MIN_ABS_DELTA:
        reasons.append(f"|delta| {abs(delta):.2f} below {MIN_ABS_DELTA} research floor")
        freshness_only_issue = False
    if bid_size is None or ask_size is None or pd.isna(bid_size) or pd.isna(ask_size) or min(bid_size, ask_size) < 1:
        reasons.append("no displayed size on one side of the book")
        freshness_only_issue = False

    target_premium = ask * (1.0 + GROSS_TARGET_RETURN)
    gross_target = target_premium - ask
    friction_ratio = spread / gross_target if gross_target > 0 else None
    if friction_ratio is None or friction_ratio > MAX_FRICTION_RATIO:
        reasons.append("estimated spread friction exceeds 35% of gross 25% target")
        freshness_only_issue = False

    runway = _runway_check(float(row.strike), spot, right, wall_records)
    if runway["blocked"]:
        reasons.append(runway["reason"])
        freshness_only_issue = False

    if not reasons:
        gate = GATE_PASS
    elif freshness_only_issue:
        gate = GATE_WAIT
    else:
        gate = GATE_FAIL

    return {
        "gate": gate,
        "reasons": reasons,
        "strike": float(row.strike),
        "right": right,
        "bid": round(bid, 4),
        "ask": round(ask, 4),
        "mid": round(mid, 4),
        "bid_size": None if bid_size is None or pd.isna(bid_size) else float(bid_size),
        "ask_size": None if ask_size is None or pd.isna(ask_size) else float(ask_size),
        "spread": round(spread, 4),
        "spread_pct": round(spread_pct * 100.0, 2) if spread_pct is not None else None,
        "quote_age_seconds": None if quote_age is None or pd.isna(quote_age) else round(float(quote_age), 1),
        "delta": round(delta, 4) if delta is not None else None,
        "implied_vol": (
            round(float(row.implied_vol), 5)
            if row.implied_vol is not None and not pd.isna(row.implied_vol) else None
        ),
        "target_premium": round(target_premium, 2),
        "friction_ratio_pct": round(friction_ratio * 100.0, 1) if friction_ratio is not None else None,
        "premium_in_preferred_band": PREFERRED_PREMIUM_LOW <= ask <= PREFERRED_PREMIUM_HIGH,
        "max_intended_loss_per_contract": round(ask * 100.0, 2),
        "feed": "thetadata_nbbo",
    }


def _candidate_rank_key(candidate: dict[str, Any]) -> tuple:
    return (
        _GATE_RANK.get(candidate["gate"], 9),
        candidate.get("friction_ratio_pct") if candidate.get("friction_ratio_pct") is not None else float("inf"),
        -(abs(candidate.get("delta")) if candidate.get("delta") is not None else 0.0),
        candidate.get("spread", float("inf")),
    )


def screen_candidates(
    symbol: str, right: str, universe: dict, wall_records: list[dict], now: dt.datetime
) -> dict[str, Any]:
    """Applies CB-V4 Section 8's gates to the near-money book for one
    directional lean (right='C' for CALL WATCH, 'P' for PUT WATCH). Returns
    the best-ranked candidate under its own gate (PASS candidates ranked
    first, then WAIT, then FAIL) -- WAIT is a first-class state here, not
    collapsed straight to a reject the way manual_options_brief.py's
    Alpaca-sourced screen does."""
    spot = universe.get("spot")
    expirations = universe.get("expirations") or []
    if spot is None or not expirations:
        raise ContractScreenUnavailable("no active universe/spot for this symbol")

    expiration = dt.date.fromisoformat(expirations[0])
    book = fetch_near_money_book(symbol, expiration, universe, now)
    if book.empty:
        raise ContractScreenUnavailable("no quoted contracts returned for the near-money window")

    book = book[book["right"] == right]
    if book.empty:
        raise ContractScreenUnavailable(f"no quoted {right} contracts in near-money window")

    candidates = [_evaluate_candidate(row, spot, right, wall_records) for row in book.itertuples()]
    best = min(candidates, key=_candidate_rank_key)
    return {
        "gate": best["gate"],
        "candidate": best,
        "reasons": best["reasons"],
        "expiration": expiration.isoformat(),
        "checked": len(candidates),
    }
