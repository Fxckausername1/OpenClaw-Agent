"""BT-2 fill models, fill eligibility, and friction metrics -- roadmap
Section 7.

Two fill models, both selectable via strategy_spec's fill_model field:
`midpoint` (an OPTIMISTIC BOUND ONLY -- NEVER HEADLINE this model's P&L,
see the note below) and `base_realistic` (entry at ask/marketable-limit,
exit at bid/marketable-limit -- the PRIMARY model for long options, per
Section 7).

Fill eligibility (Section 7): a marketable fill can only use a quote
timestamped at or after decision_ts + reaction latency (no fill on
information that predates a realistic reaction time); locked/crossed/one-
sided quotes are rejected outright; a trading-halt window freezes fills
entirely; fill quantity is capped by displayed size, never assumed
unlimited; a missed fill is always a recorded OUTCOME (a specific
FillResult.status), never a silently dropped signal.

Quote staleness and order lifetime are separate controls. Contract selection
requires a recent point-in-time quote. Once selected, an entry may fill only
from decision_ts + reaction latency through the live-matching 20-second order
TTL. Exit fills remain unbounded by that entry TTL because an owned position
must keep trying to liquidate; long quote gaps are flagged in the result.

Historical observations are trade-associated NBBO, not a continuous quote
stream. The model can prove that a qualifying observation occurred inside the
entry window, but it cannot reconstruct queue priority, partial fills between
observations, or a fill-during-cancel race that the source never recorded.
"""

from __future__ import annotations

import dataclasses
from typing import Optional

import pandas as pd

FILL_MODEL_MIDPOINT = "midpoint"
FILL_MODEL_BASE_REALISTIC = "base_realistic"
VALID_FILL_MODELS = (FILL_MODEL_MIDPOINT, FILL_MODEL_BASE_REALISTIC)

# NEVER HEADLINE `midpoint` P&L. It exists as an optimistic upper bound for
# comparison only (roadmap Section 7's own words) -- callers that report
# BT-2 results for a real decision must use base_realistic, and any report
# built on the midpoint model must carry FLAG_MIDPOINT_NEVER_HEADLINE
# (bt2_simulator.py wires this automatically whenever fill_model==midpoint).

STATUS_FILLED = "FILLED"
STATUS_FILLED_WORTHLESS = "FILLED_WORTHLESS_ZERO_BID"  # Section 16: a real, if maximally adverse, fill -- not a missed trade
STATUS_MISSED_NO_QUOTE = "MISSED_NO_QUOTE"
STATUS_MISSED_ENTRY_TTL = "MISSED_ENTRY_TTL"
STATUS_MISSED_LOCKED_CROSSED = "MISSED_LOCKED_CROSSED"
STATUS_MISSED_ONE_SIDED = "MISSED_ONE_SIDED"
STATUS_MISSED_ZERO_SIZE = "MISSED_ZERO_DISPLAYED_SIZE"
STATUS_MISSED_HALTED = "MISSED_TRADING_HALTED"

FILLED_STATUSES = (STATUS_FILLED, STATUS_FILLED_WORTHLESS)

GAP_FLAG_SECONDS = 60.0  # reporting-only: a fill this far past the reaction-
                          # latency floor is still used, but the gap gets
                          # flagged, never silently absorbed.


@dataclasses.dataclass(frozen=True)
class FillConfig:
    fill_model: str = FILL_MODEL_BASE_REALISTIC
    # Time from decision_ts to when a marketable order could realistically
    # reach the book -- a research judgment call, not yet calibrated
    # against any real fill-timing data. Flagged to heff.
    reaction_latency_seconds: float = 3.0
    # Matches smc.config.SmcConfig: cancel an unfilled entry 20 seconds after
    # submission. The window starts after reaction latency.
    entry_ttl_seconds: float = 20.0
    # Regulatory/exchange fees only -- heff's own accounts are commission-
    # free (Robinhood/Alpaca). No existing per-contract fee constant was
    # found anywhere else in this codebase to reuse, so this is a fresh
    # research default (small per-contract regulatory-fee approximation),
    # not copied from a production constant. Flagged to heff.
    fee_per_contract: float = 0.05
    # Section 16 "Trading halt": (start, end) timestamp pairs during which
    # NO fill may occur regardless of quote validity -- fills are frozen,
    # not merely delayed by the ordinary latency floor.
    halted_windows: tuple = ()
    gap_flag_seconds: float = GAP_FLAG_SECONDS


@dataclasses.dataclass
class FillResult:
    status: str
    quote_ts: Optional[pd.Timestamp]
    bid: Optional[float]
    ask: Optional[float]
    fill_price: Optional[float]
    quantity: int
    fee: float
    contemporaneous_mid: Optional[float]
    reason: Optional[str] = None
    gap_seconds: Optional[float] = None
    gap_flagged: bool = False

    @property
    def filled(self) -> bool:
        return self.status in FILLED_STATUSES


def _is_locked_or_crossed(bid: float, ask: float) -> bool:
    return bid >= ask


def _is_one_sided(bid, ask) -> bool:
    return bid is None or ask is None or pd.isna(bid) or pd.isna(ask) or ask <= 0


def _in_any_window(ts, windows: tuple) -> bool:
    for start, end in windows:
        if pd.Timestamp(start) <= ts <= pd.Timestamp(end):
            return True
    return False


def _after_latency_floor(
    quotes: pd.DataFrame, decision_ts, config: FillConfig,
    max_wait_seconds: Optional[float] = None,
) -> pd.DataFrame:
    if quotes is None or quotes.empty:
        return quotes if quotes is not None else pd.DataFrame()
    floor = pd.Timestamp(decision_ts) + pd.Timedelta(seconds=config.reaction_latency_seconds)
    eligible = quotes[quotes["quote_ts"] >= floor]
    if max_wait_seconds is not None:
        deadline = floor + pd.Timedelta(seconds=max_wait_seconds)
        eligible = eligible[eligible["quote_ts"] <= deadline]
    return eligible.sort_values("quote_ts")


def eligible_quotes(
    quotes: pd.DataFrame, decision_ts, config: FillConfig,
    max_wait_seconds: Optional[float] = None,
) -> pd.DataFrame:
    """Section 7's core eligibility rule: decision_ts + reaction latency
    must PRECEDE the quote timestamp used for a marketable fill. Also
    drops any quote inside a declared halt window (fills freeze, not just
    delay, during a halt)."""
    after_floor = _after_latency_floor(quotes, decision_ts, config, max_wait_seconds)
    if config.halted_windows and not after_floor.empty:
        return after_floor[~after_floor["quote_ts"].apply(lambda ts: _in_any_window(ts, config.halted_windows))]
    return after_floor


def _first_valid_quote(quotes: pd.DataFrame):
    """Walks quotes in time order, returns the first two-sided, non-
    crossed/locked quote within the supplied eligibility window. Entry calls
    supply a TTL-bounded window; exit calls continue to the next valid quote.
    Returns (None, reason) for the first disqualifying condition
    encountered otherwise -- a missed fill always carries a specific,
    honest reason, never a generic 'no quote'."""
    if quotes is None or quotes.empty:
        return None, STATUS_MISSED_NO_QUOTE
    for _, row in quotes.iterrows():
        bid, ask = row.get("bid"), row.get("ask")
        if _is_one_sided(bid, ask):
            continue
        if bid > 0 and _is_locked_or_crossed(bid, ask):
            continue
        return row, None
    first = quotes.iloc[0]
    bid, ask = first.get("bid"), first.get("ask")
    if _is_one_sided(bid, ask):
        return None, STATUS_MISSED_ONE_SIDED
    return None, STATUS_MISSED_LOCKED_CROSSED


def _resolve(quotes: pd.DataFrame, ts, quantity: int, config: FillConfig, side: str) -> FillResult:
    """side: 'entry' (buy at ask, capped by ask_size) or 'exit' (sell at
    bid, capped by bid_size, with Section 16's zero-bid special case)."""
    all_after_floor = _after_latency_floor(quotes, ts, config)
    max_wait = config.entry_ttl_seconds if side == "entry" else None
    after_floor = _after_latency_floor(quotes, ts, config, max_wait)
    usable = eligible_quotes(quotes, ts, config, max_wait)

    if all_after_floor.empty:
        return FillResult(status=STATUS_MISSED_NO_QUOTE, quote_ts=None, bid=None, ask=None,
                           fill_price=None, quantity=0, fee=0.0, contemporaneous_mid=None,
                           reason=f"{side} fill unavailable: no quote at/after the reaction-latency floor")
    if side == "entry" and after_floor.empty:
        return FillResult(status=STATUS_MISSED_ENTRY_TTL, quote_ts=None, bid=None, ask=None,
                           fill_price=None, quantity=0, fee=0.0, contemporaneous_mid=None,
                           reason=("entry fill unavailable: first post-latency quote arrived "
                                   f"after the {config.entry_ttl_seconds:.1f}s entry TTL"))
    if usable.empty:
        return FillResult(status=STATUS_MISSED_HALTED, quote_ts=None, bid=None, ask=None,
                           fill_price=None, quantity=0, fee=0.0, contemporaneous_mid=None,
                           reason=f"{side} fill unavailable: every remaining quote fell inside a trading-halt window")

    row, miss_reason = _first_valid_quote(usable)
    if row is None:
        return FillResult(status=miss_reason, quote_ts=None, bid=None, ask=None,
                           fill_price=None, quantity=0, fee=0.0, contemporaneous_mid=None,
                           reason=f"{side} fill unavailable: {miss_reason}")

    bid, ask = float(row["bid"]), float(row["ask"])
    mid = (bid + ask) / 2.0
    floor_ts = pd.Timestamp(ts) + pd.Timedelta(seconds=config.reaction_latency_seconds)
    gap_seconds = round((row["quote_ts"] - floor_ts).total_seconds(), 2)
    gap_flagged = gap_seconds > config.gap_flag_seconds

    if side == "exit" and bid <= 0:
        # Section 16: bid becomes zero -- realistic liquidation, never
        # midpoint, and a REAL fill (total loss), not a missed trade.
        return FillResult(status=STATUS_FILLED_WORTHLESS, quote_ts=row["quote_ts"], bid=bid, ask=ask,
                           fill_price=0.0, quantity=quantity, fee=round(config.fee_per_contract * quantity, 4),
                           contemporaneous_mid=round(mid, 4),
                           reason="bid was zero at exit -- realistic liquidation value is $0, not midpoint",
                           gap_seconds=gap_seconds, gap_flagged=gap_flagged)

    size_col = "ask_size" if side == "entry" else "bid_size"
    displayed_size = row.get(size_col)
    fill_qty = quantity
    if displayed_size is not None and not pd.isna(displayed_size):
        fill_qty = min(quantity, int(displayed_size))
    if fill_qty <= 0:
        return FillResult(status=STATUS_MISSED_ZERO_SIZE, quote_ts=row["quote_ts"], bid=bid, ask=ask,
                           fill_price=None, quantity=0, fee=0.0, contemporaneous_mid=round(mid, 4),
                           reason=f"displayed {size_col} was zero at the first eligible quote",
                           gap_seconds=gap_seconds, gap_flagged=gap_flagged)

    marketable_price = ask if side == "entry" else bid
    fill_price = mid if config.fill_model == FILL_MODEL_MIDPOINT else marketable_price
    fee = config.fee_per_contract * fill_qty
    return FillResult(status=STATUS_FILLED, quote_ts=row["quote_ts"], bid=bid, ask=ask,
                       fill_price=round(fill_price, 4), quantity=fill_qty, fee=round(fee, 4),
                       contemporaneous_mid=round(mid, 4), gap_seconds=gap_seconds, gap_flagged=gap_flagged)


def simulate_entry_fill(quotes: pd.DataFrame, decision_ts, quantity: int, config: FillConfig) -> FillResult:
    """quotes: candidate quotes for the SELECTED contract only (quote_ts,
    bid, ask, ask_size, bid_size columns). Buys at the ask (or midpoint for
    the labeled-optimistic model), capped by displayed ask size."""
    return _resolve(quotes, decision_ts, quantity, config, side="entry")


def simulate_exit_fill(quotes: pd.DataFrame, exit_ts, quantity: int, config: FillConfig) -> FillResult:
    """Same eligibility machinery as entry, but sells at bid (or midpoint
    for the labeled-optimistic model)."""
    return _resolve(quotes, exit_ts, quantity, config, side="exit")


def effective_spread_paid(fill_price: float, contemporaneous_mid: float) -> float:
    """Section 7's effective_spread_paid = 2 * abs(fill - contemporaneous_mid).

    UNITS: OPTION PREMIUM (per-share), NOT account dollars. This is a reporting
    metric defined by the roadmap, and it is deliberately kept in premium units
    to stay faithful to that definition -- but it must NEVER be subtracted from a
    dollar-denominated P&L. See `slippage_dollars` for the P&L-safe version and
    the 2026-07-31 bug note in `friction_metrics`."""
    return round(2 * abs(fill_price - contemporaneous_mid), 4)


def slippage_dollars(entry: FillResult, exit: FillResult, quantity: int) -> dict:
    """SIGNED execution cost in ACCOUNT DOLLARS -- the P&L-safe slippage.

    Signed (not abs) and not halved, because the algebra then reconciles exactly
    with zero fudge factors:

        entry_cost = (entry_fill - entry_mid) * qty * 100   # >0 when paying up
        exit_cost  = (exit_mid  - exit_fill) * qty * 100    # >0 when receiving less

        mid_pnl - entry_cost - exit_cost
          = (exit_mid - entry_mid) - (entry_fill - entry_mid) - (exit_mid - exit_fill)
          = exit_fill - entry_fill                                    [x qty x 100]

    i.e. the midpoint-minus-slippage decomposition is IDENTICALLY equal to direct
    fill-to-fill P&L. `friction_metrics` asserts this at runtime rather than
    trusting the comment."""
    entry_cost = ((entry.fill_price - entry.contemporaneous_mid) * quantity * 100
                  if entry.filled else 0.0)
    exit_cost = ((exit.contemporaneous_mid - exit.fill_price) * quantity * 100
                 if exit.filled else 0.0)
    return {
        "entry_slippage_dollars": round(entry_cost, 6),
        "exit_slippage_dollars": round(exit_cost, 6),
        "total_slippage_dollars": round(entry_cost + exit_cost, 6),
    }


def friction_metrics(entry: FillResult, exit: FillResult, planned_gross_profit: Optional[float],
                     quantity: Optional[int] = None) -> dict:
    """Section 7's friction metrics, with units made explicit after a real
    accounting bug.

    THE BUG (found 2026-07-31): the previous version returned
    `round_trip_friction = entry_slippage + exit_slippage + fees`, summing
    entry/exit slippage in OPTION PREMIUM units with fees in ACCOUNT DOLLARS.
    bt2_simulator then did `net_pnl = gross_pnl(dollars) - slippage(premium) -
    fees(dollars)`, under-subtracting execution cost by a factor of qty*100/2.
    On the 162-session B1 baseline that inflated reported expectancy from
    $6.19 to $7.27 per trade (~$1.08, i.e. ~17%) and also made
    `friction_share_of_target` meaninglessly small.

    THE FIX: `*_dollars` fields are the only ones P&L may use. The premium-unit
    Section 7 metrics are retained under their original names for continuity, now
    explicitly suffixed/labelled, and are reporting-only.

    `quantity` defaults to the entry fill quantity when not supplied."""
    qty = quantity if quantity is not None else (entry.quantity or 0)

    # --- Section 7 reporting metrics (OPTION PREMIUM units -- never for P&L)
    entry_slippage_premium = (effective_spread_paid(entry.fill_price, entry.contemporaneous_mid)
                              if entry.filled else 0.0)
    exit_slippage_premium = (effective_spread_paid(exit.fill_price, exit.contemporaneous_mid)
                             if exit.filled else 0.0)

    # --- P&L-safe values (ACCOUNT DOLLARS)
    slip = slippage_dollars(entry, exit, qty)
    fees_dollars = round((entry.fee or 0.0) + (exit.fee or 0.0), 4)
    round_trip_friction_dollars = round(slip["total_slippage_dollars"] + fees_dollars, 4)
    friction_share = (
        round(round_trip_friction_dollars / planned_gross_profit, 4)
        if planned_gross_profit and planned_gross_profit > 0 else None
    )

    out = {
        # premium-unit, reporting only
        "entry_slippage": entry_slippage_premium,
        "exit_slippage": exit_slippage_premium,
        "entry_slippage_premium": entry_slippage_premium,
        "exit_slippage_premium": exit_slippage_premium,
        # dollars -- the only fields P&L may consume
        "fees": fees_dollars,
        "quantity": qty,
        "round_trip_friction_dollars": round_trip_friction_dollars,
        "friction_share_of_target": friction_share,
        **slip,
    }

    # --- runtime proof of the identity documented in slippage_dollars(). A future
    # edit that reintroduces a unit mix-up fails HERE, loudly, instead of silently
    # shifting every reported expectancy again.
    if entry.filled and exit.filled and qty:
        direct = (exit.fill_price - entry.fill_price) * qty * 100
        mid = (exit.contemporaneous_mid - entry.contemporaneous_mid) * qty * 100
        decomposed = mid - slip["total_slippage_dollars"]
        if abs(decomposed - direct) > 1e-6:
            raise AssertionError(
                f"friction decomposition does not reconcile to direct fill P&L: "
                f"direct={direct!r} decomposed={decomposed!r} (entry_fill={entry.fill_price} "
                f"entry_mid={entry.contemporaneous_mid} exit_fill={exit.fill_price} "
                f"exit_mid={exit.contemporaneous_mid} qty={qty})")
        out["reconciles_to_direct_fill_pnl"] = True
    return out
