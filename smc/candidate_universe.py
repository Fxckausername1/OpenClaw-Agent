"""Merge layer between the ThetaData streaming quote cache (hot path) and
the REST Greek cache (periodic refresh), producing ONE atomic
selection-ready book for smc.selector_variant_b.

Division of responsibility, per heff's explicit correction (2026-08-01):
streaming quotes govern entry/exit decisions; REST is discovery + periodic
Greek refresh ONLY and must never be on the signal->order critical path.
The streaming protocol does not carry delta, so delta necessarily comes
from a DIFFERENT source on a DIFFERENT cadence than bid/ask. That mismatch
is the whole reason this module exists and why quote age and Greek age are
tracked, thresholded and reported SEPARATELY -- a fresh quote paired with
an unboundedly stale delta is exactly the silent-corruption case this layer
must make impossible.

HISTORICAL-FIDELITY NOTE, stated plainly rather than buried: in the tested
Variant B backtest, delta was DERIVED from the same point-in-time book the
quote came from (build_point_in_time_book solves implied vol off that
book's own mid), so "delta age" and "quote age" were structurally the same
number and a delta/quote skew was not physically representable. Live, they
are two independent clocks. The Greek-age gate here therefore guards a
failure mode that has NO historical analogue and was NEVER exercised by the
Variant B result -- it is new, necessary, and unvalidated by that backtest.

Deliberately does NOT tighten any gate the tested Variant B selector
already owns. max_spread_pct_mid defaults to None (delegate entirely to
bt2_selector's own tested spread gates) so this layer cannot silently
shrink the selected population relative to the frozen candidate. The one
genuinely additive gate is Greek age, for the reason above.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import time
from typing import Optional

import pandas as pd

from smc.selector_variant_b import select_variant_b_contract
from smc.theta_market_data import build_occ_symbol, parse_occ_symbol
from thetadata_pipeline.schemas import normalize_right

# Columns bt2_selector.evaluate_candidate actually reads. Kept explicit so a
# future change there fails loudly here instead of silently producing a book
# with a missing gate input.
SELECTION_BOOK_COLUMNS = [
    "strike", "right", "expiration", "bid", "ask", "bid_size", "ask_size",
    "delta", "quote_age_seconds",
]

# Ineligibility reason codes -- stable strings, safe to assert on in tests
# and to aggregate in the dashboard.
R_NOT_SUBSCRIBED = "subscription_not_confirmed"
R_NO_QUOTE = "no_quote_received"
R_STALE_GENERATION = "stale_stream_generation"
R_QUOTE_TOO_OLD = "quote_age_exceeds_ceiling"
R_NO_DELTA = "delta_unavailable"
R_DELTA_TOO_OLD = "greek_age_exceeds_ceiling"
R_MISSING_SIDE = "missing_bid_or_ask"
R_NONPOSITIVE = "nonpositive_price"
R_CROSSED = "crossed_market"
R_LOCKED = "locked_market"
R_WIDE_SPREAD = "spread_exceeds_universe_ceiling"
R_DISCONNECTED = "stream_disconnected"


@dataclasses.dataclass(frozen=True)
class UniverseConfig:
    """Frozen thresholds. quote and Greek ages are SEPARATE by design and
    must stay separate -- collapsing them to one number is what would hide
    a fresh-quote/stale-delta pairing.

    max_quote_age_seconds mirrors bt2_selector.SelectorConfig's own
    max_quote_age_seconds (10.0) rather than tightening it, so this layer
    admits exactly the population the tested selector expects to judge.

    max_greek_age_seconds is a judgment call with no backtest behind it
    (see module docstring): 60s is roughly 2x the intended REST refresh
    interval, so one skipped refresh degrades gracefully but two do not.

    max_spread_pct_mid defaults to None = no universe-level spread gate;
    bt2_selector's tested max_spread_dollars / max_spread_pct_mid gates do
    that job. Set it only for a deliberate, documented tightening."""
    max_quote_age_seconds: float = 10.0
    max_greek_age_seconds: float = 60.0
    max_spread_pct_mid: Optional[float] = None
    require_subscription_confirmed: bool = True


@dataclasses.dataclass(frozen=True)
class Candidate:
    """One contract's fully-attributed state at one instant. Every field the
    decision depended on is carried so a decision can be reconstructed after
    the fact from the record alone."""
    occ: str
    expiration: Optional[dt.date]
    strike: Optional[float]
    right: Optional[str]
    # --- Greek side (REST, periodic) ---
    delta: Optional[float]
    delta_source_ts: Optional[dt.datetime]
    delta_age_seconds: Optional[float]
    # --- quote side (streaming, hot path) ---
    bid: Optional[float]
    ask: Optional[float]
    bid_size: Optional[float]
    ask_size: Optional[float]
    exchange_ts: Optional[dt.datetime]      # ThetaData exchange timestamp
    receipt_ts: Optional[dt.datetime]       # local wall clock at receipt
    quote_age_seconds: Optional[float]
    # --- stream state ---
    quote_generation: Optional[int]
    snapshot_generation: int
    subscription_confirmed: bool
    # --- verdict ---
    eligible: bool
    reasons: tuple

    @property
    def spread(self) -> Optional[float]:
        if self.bid is None or self.ask is None:
            return None
        return self.ask - self.bid


@dataclasses.dataclass(frozen=True)
class SelectionSnapshot:
    """The atomic unit a selection runs against. Once constructed, nothing
    the stream thread or the REST refresh thread does can change it, so a
    quote or Greek arriving mid-selection cannot alter a decision partway
    through -- it simply lands in the NEXT snapshot."""
    taken_ts: dt.datetime
    taken_monotonic: float
    generation: int
    connected: bool
    candidates: tuple
    config: UniverseConfig

    @property
    def eligible(self) -> tuple:
        return tuple(c for c in self.candidates if c.eligible)

    def to_book(self) -> pd.DataFrame:
        """Eligible candidates only, in bt2_selector's book shape."""
        rows = [{
            "strike": c.strike, "right": c.right, "expiration": c.expiration,
            "bid": c.bid, "ask": c.ask, "bid_size": c.bid_size,
            "ask_size": c.ask_size, "delta": c.delta,
            "quote_age_seconds": c.quote_age_seconds,
        } for c in self.eligible]
        if not rows:
            return pd.DataFrame(columns=SELECTION_BOOK_COLUMNS)
        return pd.DataFrame(rows, columns=SELECTION_BOOK_COLUMNS)

    def diagnostics(self) -> dict:
        counts: dict = {}
        for c in self.candidates:
            for r in c.reasons:
                counts[r] = counts.get(r, 0) + 1
        return {
            "generation": self.generation, "connected": self.connected,
            "n_candidates": len(self.candidates), "n_eligible": len(self.eligible),
            "rejection_counts": counts,
        }


def canonical_occ(occ: str) -> str:
    """Collapses formatting variants of the same contract to one identity.

    Duplicate-OCC normalization matters because OCC symbols reach this layer
    from three places that pad and case differently (the REST chain, the
    stream's contract dict, and our own builder). Round-tripping through the
    parser guarantees one canonical spelling per real contract, so a
    duplicate can never appear as two candidates."""
    parsed = parse_occ_symbol(str(occ).strip().upper())
    return build_occ_symbol(parsed["root"], parsed["expiration"],
                            parsed["strike"], parsed["right"])


def _evaluate(occ, quote, subscribed, snapshot_generation, connected,
              greek, now_monotonic, config) -> Candidate:
    reasons = []

    delta = delta_src_ts = delta_age = None
    if greek is not None:
        delta = greek.get("delta")
        delta_src_ts = greek.get("source_wall")
        src_m = greek.get("source_monotonic")
        if src_m is not None:
            delta_age = round(now_monotonic - src_m, 3)

    parsed = None
    try:
        parsed = parse_occ_symbol(occ)
    except ValueError:
        parsed = None

    if not connected:
        reasons.append(R_DISCONNECTED)
    if config.require_subscription_confirmed and not subscribed:
        reasons.append(R_NOT_SUBSCRIBED)

    bid = ask = bid_size = ask_size = None
    exchange_ts = receipt_ts = quote_age = quote_gen = None

    if quote is None:
        # Covers the "subscription acknowledged but no live quote received"
        # case explicitly: subscribed can be True while this still fires.
        reasons.append(R_NO_QUOTE)
    else:
        bid, ask = quote.bid, quote.ask
        bid_size, ask_size = quote.bid_size, quote.ask_size
        exchange_ts, receipt_ts = quote.exchange_ts, quote.receipt_ts
        quote_gen = quote.generation
        quote_age = round(now_monotonic - quote.receipt_monotonic, 3)

        if quote_gen != snapshot_generation:
            # A reconnect happened after this quote arrived. Re-acked
            # subscriptions must NOT resurrect it -- a fresh post-reconnect
            # quote is required.
            reasons.append(R_STALE_GENERATION)
        if quote_age > config.max_quote_age_seconds:
            reasons.append(R_QUOTE_TOO_OLD)
        if bid is None or ask is None:
            reasons.append(R_MISSING_SIDE)
        else:
            if bid <= 0 or ask <= 0:
                reasons.append(R_NONPOSITIVE)
            elif bid > ask:
                reasons.append(R_CROSSED)
            elif bid == ask:
                reasons.append(R_LOCKED)
            elif config.max_spread_pct_mid is not None:
                mid = (bid + ask) / 2.0
                if mid > 0 and (ask - bid) / mid > config.max_spread_pct_mid:
                    reasons.append(R_WIDE_SPREAD)

    if delta is None or (isinstance(delta, float) and pd.isna(delta)):
        reasons.append(R_NO_DELTA)
    elif delta_age is None or delta_age > config.max_greek_age_seconds:
        # A stale delta is never silently paired with a fresh quote.
        reasons.append(R_DELTA_TOO_OLD)

    return Candidate(
        occ=occ,
        expiration=parsed["expiration"] if parsed else None,
        strike=parsed["strike"] if parsed else None,
        right=normalize_right(parsed["right"]) if parsed else None,
        delta=delta, delta_source_ts=delta_src_ts, delta_age_seconds=delta_age,
        bid=bid, ask=ask, bid_size=bid_size, ask_size=ask_size,
        exchange_ts=exchange_ts, receipt_ts=receipt_ts, quote_age_seconds=quote_age,
        quote_generation=quote_gen, snapshot_generation=snapshot_generation,
        subscription_confirmed=bool(subscribed),
        eligible=not reasons, reasons=tuple(reasons),
    )


def build_snapshot(stream_snapshot: dict, greek_snapshot: dict,
                   occs, config: UniverseConfig = UniverseConfig()) -> SelectionSnapshot:
    """Pure function over two already-taken snapshots -- no clients, no I/O,
    no clocks of its own beyond the snapshot's own instant. Every candidate
    is aged against the SAME taken_monotonic so ages are mutually comparable
    within one decision."""
    now_m = stream_snapshot["taken_monotonic"]
    gen = stream_snapshot["generation"]
    connected = stream_snapshot["connected"]
    quotes = stream_snapshot["quotes"]
    subs = stream_snapshot["subscribed"]
    deltas = greek_snapshot.get("deltas", {})

    seen = set()
    candidates = []
    for raw in occs:
        try:
            occ = canonical_occ(raw)
        except ValueError:
            continue
        if occ in seen:
            continue          # duplicate OCC collapsed to one candidate
        seen.add(occ)
        candidates.append(_evaluate(
            occ, quotes.get(occ), subs.get(occ, False), gen, connected,
            deltas.get(occ), now_m, config,
        ))

    return SelectionSnapshot(
        taken_ts=stream_snapshot["taken_ts"], taken_monotonic=now_m,
        generation=gen, connected=connected,
        candidates=tuple(candidates), config=config,
    )


def take_snapshot(stream_client, greek_cache, occs,
                  config: UniverseConfig = UniverseConfig()) -> SelectionSnapshot:
    """Takes both underlying snapshots and merges them.

    Stream first, Greeks second: the stream snapshot fixes the decision
    instant (it owns the hot-path data and the generation), and the Greek
    read is a warm in-memory cache read that cannot block on the network.
    Both are internally atomic; ages are then computed against the stream
    snapshot's single instant."""
    occs = [canonical_occ(o) for o in occs]
    stream_snapshot = stream_client.snapshot(occs)
    greek_snapshot = greek_cache.greek_snapshot(occs)
    return build_snapshot(stream_snapshot, greek_snapshot, occs, config)


def select_from_snapshot(snapshot: SelectionSnapshot, right: str, decision_ts):
    """Hands the eligible book to the FROZEN Variant B selector unchanged.
    This module decides what is admissible to look at; it never ranks, never
    re-weights, and never overrides a selector verdict."""
    return select_variant_b_contract(snapshot.to_book(), right, decision_ts)
