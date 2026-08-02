"""Entry and exit order policy for VARIANT_B_NO_SWEEP's paper forward test.

ENTRY: marketable limit, never a market order, priced from the live
ThetaData NBBO, hard-capped so the intended debit including fees can never
exceed $100.

ENTRY TTL = 20 seconds, and the justification is operational fill timing,
NOT historical P&L:

    The ten real entries of 2026-07-31 are cleanly bimodal in time-to-fill:
        0s, 0s, 0s, 0s, 1s, 0s, 0s, 2s   -> eight fills within 0-2 seconds
        66s, 437s                        -> two stragglers
    There is NOTHING between 2 and 66 seconds. A marketable limit against a
    liquid QQQ 0DTE contract either fills essentially immediately or the
    market has moved away from the premise that produced it. A 20-second TTL
    sits in the middle of that empty gap: it cannot plausibly cancel a
    healthy fill (10x the slowest healthy one observed), and it would have
    cancelled BOTH stragglers -- including the 437-second resting order that
    became trade #10's -$39 loss.

    That the backtest's FillConfig also models a 20-second entry TTL is a
    convenience for interpreting the forward result against it, not the
    reason for the choice. The number is adopted here on the fill-timing
    evidence above and is stated explicitly rather than inherited silently.

EXIT: the stop-exit method is DELIBERATELY NOT FROZEN in this module yet.
Market vs aggressive marketable limit must be benchmarked with controlled
Alpaca PAPER orders first (heff's requirement 6), and the winner frozen
before Session 1. `ExitMethod` enumerates the candidates and
`build_exit_order` implements both so the benchmark can run; nothing here
picks one.

What this module does NOT do, by design: it never defers, re-prices, or
retries on a schedule. The 2026-07-31 exit path cancelled orders that missed
a 2-second fill window and re-submitted them ~2 minutes later at a worse
bid, walking two stops from -20% to -66% and -42%. Continuous management of
a live exit order belongs to the persistent daemon, not to a re-pricing
ladder.
"""
from __future__ import annotations

import dataclasses
import math
from typing import Optional

DEBIT_CAP_DOLLARS = 100.0
FEE_PER_CONTRACT = 0.05          # same regulatory-fee approximation as every B-series experiment
CONTRACT_MULTIPLIER = 100.0

ENTRY_TTL_SECONDS = 20.0         # see module docstring -- justified on fill timing
TICK = 0.01                      # US options minimum increment at these premiums


class ExitMethod:
    MARKET = "market"
    MARKETABLE_LIMIT = "marketable_limit"
    ALL = (MARKET, MARKETABLE_LIMIT)


class OrderPolicyError(ValueError):
    pass


def round_to_tick(price: float, *, up: bool) -> float:
    """Options quote in whole cents; a sub-penny limit is rejected by the
    venue. Rounds AWAY from us (up for buys, down for sells) so rounding can
    never make an order less marketable than intended."""
    ticks = price / TICK
    n = math.ceil(ticks - 1e-9) if up else math.floor(ticks + 1e-9)
    return round(n * TICK, 2)


def total_debit_dollars(limit_price: float, quantity: int = 1,
                        fee_per_contract: float = FEE_PER_CONTRACT) -> float:
    """Actual dollars at risk if filled at this limit, INCLUDING fees --
    never the quoted premium alone. Identical definition to
    selector_policy_experiment.total_debit_dollars."""
    return round(float(limit_price) * CONTRACT_MULTIPLIER * quantity
                 + fee_per_contract * quantity, 4)


def max_affordable_limit(quantity: int = 1,
                         fee_per_contract: float = FEE_PER_CONTRACT) -> float:
    """Highest whole-cent limit whose total debit still fits under the cap."""
    raw = (DEBIT_CAP_DOLLARS - fee_per_contract * quantity) / (CONTRACT_MULTIPLIER * quantity)
    return round_to_tick(raw, up=False)


@dataclasses.dataclass(frozen=True)
class EntryOrder:
    occ: str
    side: str                    # always "buy" for this strategy
    quantity: int
    limit_price: float
    time_in_force: str
    ttl_seconds: float
    # Provenance: exactly the quote this limit was computed from.
    quote_bid: Optional[float]
    quote_ask: Optional[float]
    quote_age_seconds: Optional[float]
    quote_exchange_ts: Optional[object]
    quote_generation: Optional[int]
    intended_debit: float
    capped: bool                 # True if the cap, not the quote, set the limit

    def as_alpaca_payload(self, client_order_id: str) -> dict:
        return {
            "symbol": self.occ, "qty": str(self.quantity), "side": self.side,
            "type": "limit", "time_in_force": self.time_in_force,
            "limit_price": f"{self.limit_price:.2f}",
            "client_order_id": client_order_id,
        }


def build_entry_order(occ: str, quote, quantity: int = 1,
                      time_in_force: str = "day",
                      ttl_seconds: float = ENTRY_TTL_SECONDS) -> EntryOrder:
    """Marketable limit at the ask (crossing the spread to take liquidity),
    then hard-capped at the $100 debit ceiling.

    Priced at the ask rather than the mid on purpose: an entry resting at the
    mid is exactly the order that sits unfilled while the premise decays --
    the 437-second fill. We want a fill now or no fill at all, which is what
    the TTL then enforces.

    Raises rather than guessing when the quote cannot support a marketable
    limit; a caller must never receive a silently degraded order."""
    bid = getattr(quote, "bid", None)
    ask = getattr(quote, "ask", None)
    if ask is None or bid is None:
        raise OrderPolicyError(f"{occ}: cannot price an entry without a two-sided quote")
    if ask <= 0 or bid <= 0:
        raise OrderPolicyError(f"{occ}: non-positive quote bid={bid} ask={ask}")
    if bid > ask:
        raise OrderPolicyError(f"{occ}: crossed quote bid={bid} ask={ask}")

    limit = round_to_tick(float(ask), up=True)
    ceiling = max_affordable_limit(quantity)
    capped = limit > ceiling
    if capped:
        limit = ceiling
    if limit <= 0:
        raise OrderPolicyError(
            f"{occ}: $100 cap admits no valid limit at quantity {quantity}")

    debit = total_debit_dollars(limit, quantity)
    if debit > DEBIT_CAP_DOLLARS + 1e-9:
        raise OrderPolicyError(
            f"{occ}: intended debit ${debit:.2f} exceeds ${DEBIT_CAP_DOLLARS:.2f} cap")

    return EntryOrder(
        occ=occ, side="buy", quantity=quantity, limit_price=limit,
        time_in_force=time_in_force, ttl_seconds=ttl_seconds,
        quote_bid=bid, quote_ask=ask,
        quote_age_seconds=getattr(quote, "age_seconds", lambda: None)(),
        quote_exchange_ts=getattr(quote, "exchange_ts", None),
        quote_generation=getattr(quote, "generation", None),
        intended_debit=debit, capped=capped,
    )


@dataclasses.dataclass(frozen=True)
class ExitOrder:
    occ: str
    side: str                    # always "sell"
    quantity: int
    order_type: str              # "market" | "limit"
    limit_price: Optional[float]
    time_in_force: str
    method: str
    quote_bid: Optional[float]
    quote_ask: Optional[float]
    quote_age_seconds: Optional[float]
    aggression_ticks: int

    def as_alpaca_payload(self, client_order_id: str) -> dict:
        payload = {
            "symbol": self.occ, "qty": str(self.quantity), "side": self.side,
            "type": self.order_type, "time_in_force": self.time_in_force,
            "client_order_id": client_order_id,
        }
        if self.limit_price is not None:
            payload["limit_price"] = f"{self.limit_price:.2f}"
        return payload


def build_exit_order(occ: str, quote, method: str, quantity: int = 1,
                     aggression_ticks: int = 2,
                     time_in_force: str = "day") -> ExitOrder:
    """Builds either candidate exit. NEITHER is frozen -- the benchmark
    picks one before Session 1.

    For MARKETABLE_LIMIT the limit is placed `aggression_ticks` BELOW the
    bid, not at it. Pricing a protective sell exactly at the displayed bid is
    the 2026-07-31 failure: the bid was a lagging indicative print, the order
    rested, and the loss ran. Crossing through by a couple of ticks buys
    immediacy at a bounded, known cost -- which is the correct trade for a
    stop."""
    if method not in ExitMethod.ALL:
        raise OrderPolicyError(f"unknown exit method {method!r}")
    bid = getattr(quote, "bid", None)
    ask = getattr(quote, "ask", None)

    if method == ExitMethod.MARKET:
        return ExitOrder(occ=occ, side="sell", quantity=quantity,
                         order_type="market", limit_price=None,
                         time_in_force=time_in_force, method=method,
                         quote_bid=bid, quote_ask=ask,
                         quote_age_seconds=getattr(quote, "age_seconds", lambda: None)(),
                         aggression_ticks=0)

    if bid is None or bid <= 0:
        raise OrderPolicyError(
            f"{occ}: cannot price a marketable-limit exit without a positive bid")
    limit = round_to_tick(max(float(bid) - aggression_ticks * TICK, TICK), up=False)
    return ExitOrder(occ=occ, side="sell", quantity=quantity,
                     order_type="limit", limit_price=limit,
                     time_in_force=time_in_force, method=method,
                     quote_bid=bid, quote_ask=ask,
                     quote_age_seconds=getattr(quote, "age_seconds", lambda: None)(),
                     aggression_ticks=aggression_ticks)
