"""Verifies a REAL ThetaData quote message before quotes are trusted.

The first live quote on Monday must not automatically enable entries. A
socket that connects and a subscription that acks prove transport; they
prove nothing about whether we are reading the message correctly. The
ThetaData subscribe-ack already taught us that a docs-derived synthetic test
passes while the real protocol differs (the ack carries no `contract` field,
only `req_id`), so the parser gate is deliberately unsatisfiable by
synthetic data: `verify_live_message` requires an object that came off the
wire in this process, carrying a stream generation from a live connection.

ELEVEN CHECKS, each mapping to a way a misread would cost money:

    schema            required fields present at all
    occ_reconstruction  the OCC we rebuilt matches the contract we subscribed
    strike_scale      strike is in dollars, not ThetaData's integer mills
    right             C or P, matching what we asked for
    bid_ask_units     prices are option premium (dollars/share), not cents
    exchange_timestamp present, sane, and not in the future
    receipt_timestamp present and after the exchange timestamp
    provenance        an allowlisted trusted NBBO identity
    stream_generation stamped by the CURRENT physical connection
    non_crossed       bid < ask, and neither non-positive
    age_threshold     inside the frozen staleness ceiling

A single failed check leaves the gate RED with the failing check named. We do
not average, score or partially credit: a strike scale error is not offset by
a good timestamp.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
from typing import Optional

from smc.feeds import FEED_THETADATA_OPRA_NBBO, evaluate_quote_provenance

# A QQQ option premium above this is implausible and suggests a unit error
# (e.g. cents read as dollars).
MAX_PLAUSIBLE_PREMIUM = 500.0
# Strikes below this suggest mills were not divided down.
MIN_PLAUSIBLE_STRIKE = 1.0
MAX_PLAUSIBLE_STRIKE = 10000.0
MAX_CLOCK_SKEW_SECONDS = 120.0

CHECKS = ("schema", "occ_reconstruction", "strike_scale", "right",
          "bid_ask_units", "exchange_timestamp", "receipt_timestamp",
          "provenance", "stream_generation", "non_crossed", "age_threshold")


@dataclasses.dataclass
class VerificationResult:
    verified: bool
    checks: dict
    failures: list
    occ: Optional[str] = None
    evidence: Optional[dict] = None

    def as_dict(self) -> dict:
        return dataclasses.asdict(self)


def verify_live_message(quote, *, expected_occ: Optional[str] = None,
                        current_generation: Optional[int] = None,
                        max_age_seconds: float = 10.0,
                        now_utc: Optional[dt.datetime] = None) -> VerificationResult:
    """Runs all eleven checks against ONE real StreamQuote.

    `current_generation` must be the live client's generation. Passing None
    fails the stream_generation check on purpose: a quote that cannot be tied
    to a live physical connection is not evidence of a working live parser."""
    now_utc = now_utc or dt.datetime.now(dt.timezone.utc)
    checks = {c: False for c in CHECKS}
    notes = {}

    if quote is None:
        return VerificationResult(False, checks, list(CHECKS), None,
                                  {"error": "no quote supplied"})

    # 1. schema
    required = ("occ", "bid", "ask", "strike", "right", "expiration",
                "exchange_ts", "receipt_ts", "generation")
    missing = [f for f in required if not hasattr(quote, f)]
    checks["schema"] = not missing
    if missing:
        notes["schema"] = f"missing fields: {missing}"

    # 2. OCC reconstruction
    occ = getattr(quote, "occ", None)
    if expected_occ is not None:
        checks["occ_reconstruction"] = (occ == expected_occ)
        if not checks["occ_reconstruction"]:
            notes["occ_reconstruction"] = f"rebuilt {occ!r} != subscribed {expected_occ!r}"
    else:
        checks["occ_reconstruction"] = bool(occ) and len(str(occ)) >= 15
        if not checks["occ_reconstruction"]:
            notes["occ_reconstruction"] = f"implausible OCC {occ!r}"

    # 3. strike scale -- dollars, not mills
    strike = getattr(quote, "strike", None)
    try:
        checks["strike_scale"] = (strike is not None
                                  and MIN_PLAUSIBLE_STRIKE <= float(strike) <= MAX_PLAUSIBLE_STRIKE)
        if not checks["strike_scale"]:
            notes["strike_scale"] = (f"strike {strike} outside plausible range -- "
                                     "mills not divided down?")
    except (TypeError, ValueError):
        notes["strike_scale"] = f"unparseable strike {strike!r}"

    # 4. right
    right = getattr(quote, "right", None)
    checks["right"] = right in ("C", "P")
    if not checks["right"]:
        notes["right"] = f"right {right!r} is not C or P"

    # 5. bid/ask units
    bid, ask = getattr(quote, "bid", None), getattr(quote, "ask", None)
    try:
        checks["bid_ask_units"] = (bid is not None and ask is not None
                                   and 0 < float(ask) <= MAX_PLAUSIBLE_PREMIUM
                                   and 0 < float(bid) <= MAX_PLAUSIBLE_PREMIUM)
        if not checks["bid_ask_units"]:
            notes["bid_ask_units"] = (f"bid={bid} ask={ask} implausible as option "
                                      "premium -- cents read as dollars?")
    except (TypeError, ValueError):
        notes["bid_ask_units"] = f"unparseable bid/ask {bid!r}/{ask!r}"

    # 6. exchange timestamp
    ets = getattr(quote, "exchange_ts", None)
    if isinstance(ets, dt.datetime):
        skew = (ets - now_utc).total_seconds()
        checks["exchange_timestamp"] = skew <= MAX_CLOCK_SKEW_SECONDS
        if not checks["exchange_timestamp"]:
            notes["exchange_timestamp"] = f"exchange ts {skew:.1f}s in the future"
    else:
        notes["exchange_timestamp"] = f"exchange_ts is {type(ets).__name__}, not datetime"

    # 7. receipt timestamp
    rts = getattr(quote, "receipt_ts", None)
    if isinstance(rts, dt.datetime):
        if isinstance(ets, dt.datetime):
            delta = (rts - ets).total_seconds()
            checks["receipt_timestamp"] = -MAX_CLOCK_SKEW_SECONDS <= delta
            if not checks["receipt_timestamp"]:
                notes["receipt_timestamp"] = (
                    f"receipt precedes exchange ts by {-delta:.1f}s")
        else:
            checks["receipt_timestamp"] = True
    else:
        notes["receipt_timestamp"] = f"receipt_ts is {type(rts).__name__}, not datetime"

    # 8. provenance
    age = None
    try:
        age = quote.age_seconds()
    except Exception:  # noqa: BLE001
        age = None
    prov = evaluate_quote_provenance(
        FEED_THETADATA_OPRA_NBBO, exchange_ts=str(ets), receipt_ts=str(rts),
        age_seconds=age, stream_generation=getattr(quote, "generation", None),
        max_age_seconds=max_age_seconds)
    checks["provenance"] = prov.trusted_nbbo
    if not prov.trusted_nbbo:
        notes["provenance"] = prov.reason or "untrusted feed"

    # 9. stream generation -- must be the CURRENT physical connection
    gen = getattr(quote, "generation", None)
    checks["stream_generation"] = (current_generation is not None
                                   and gen == current_generation and gen > 0)
    if not checks["stream_generation"]:
        notes["stream_generation"] = (
            f"quote generation {gen} != current {current_generation} "
            "(a pre-reconnect or synthetic quote cannot verify a live parser)")

    # 10. non-crossed
    try:
        checks["non_crossed"] = (bid is not None and ask is not None
                                 and float(bid) > 0 and float(ask) > 0
                                 and float(bid) < float(ask))
        if not checks["non_crossed"]:
            notes["non_crossed"] = f"bid={bid} ask={ask} crossed, locked or non-positive"
    except (TypeError, ValueError):
        notes["non_crossed"] = "unparseable prices"

    # 11. age
    checks["age_threshold"] = age is not None and age <= max_age_seconds
    if not checks["age_threshold"]:
        notes["age_threshold"] = f"age {age} exceeds {max_age_seconds}s ceiling"

    failures = [c for c, ok in checks.items() if not ok]
    return VerificationResult(
        verified=not failures, checks=checks, failures=failures, occ=occ,
        evidence={"notes": notes, "age_seconds": age,
                  "generation": gen, "bid": bid, "ask": ask, "strike": strike,
                  "right": right, "verified_ts": now_utc.isoformat()})
