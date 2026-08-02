"""Explicit quote-feed provenance.

The first pass widened `is_nbbo` to a set that included a feed literal
"thetadata". That is too loose in one specific way: it makes trust depend on
a bare string that any caller can supply, so an arbitrary value containing
"theta", or a typo'd provider, could end up being treated as NBBO. Trust
must come from an explicit allowlist of KNOWN feed identities, not from
string shape.

FROZEN TRUSTED IDENTITIES:

    alpaca_opra          Alpaca's OPRA feed -- real NBBO
    thetadata_opra_nbbo  ThetaData's OPRA NBBO -- real NBBO, different carrier

EXPLICITLY UNTRUSTED (known, named, and refused for pricing decisions):

    alpaca_indicative    derived/aggregated, NOT NBBO

Anything else -- unknown provider, empty, None, malformed -- is refused. The
default is refusal, so a new provider has to be added here deliberately
rather than working by accident.

Provider identity is RETAINED on every quote and every broker-event
comparison rather than collapsed to a boolean, so "which feed priced this
fill" is always answerable after the fact. A boolean would tell you a
decision was allowed; it would not tell you what it was based on.
"""
from __future__ import annotations

import dataclasses
from typing import Optional

FEED_ALPACA_OPRA = "alpaca_opra"
FEED_THETADATA_OPRA_NBBO = "thetadata_opra_nbbo"
FEED_ALPACA_INDICATIVE = "alpaca_indicative"

# Real NBBO sources. Membership here is the ONLY thing that grants trust.
TRUSTED_NBBO_FEEDS = frozenset({FEED_ALPACA_OPRA, FEED_THETADATA_OPRA_NBBO})

# Known-but-untrusted: named so a refusal says WHY, not merely "unknown".
KNOWN_NON_NBBO_FEEDS = frozenset({FEED_ALPACA_INDICATIVE})

ALL_KNOWN_FEEDS = TRUSTED_NBBO_FEEDS | KNOWN_NON_NBBO_FEEDS

# Legacy labels that predate this module, mapped forward so existing records
# stay readable. Deliberately explicit rather than a prefix rule.
_LEGACY_ALIASES = {
    "opra": FEED_ALPACA_OPRA,
    "indicative": FEED_ALPACA_INDICATIVE,
    "thetadata": FEED_THETADATA_OPRA_NBBO,
}


class FeedProvenanceError(ValueError):
    """The feed identity is missing, malformed or unknown."""


def normalize_feed(feed) -> str:
    """Maps a legacy label onto a canonical identity. Raises on anything not
    recognised -- guessing would defeat the allowlist."""
    if not isinstance(feed, str) or not feed.strip():
        raise FeedProvenanceError(f"missing or malformed feed identity: {feed!r}")
    f = feed.strip().lower()
    f = _LEGACY_ALIASES.get(f, f)
    if f not in ALL_KNOWN_FEEDS:
        raise FeedProvenanceError(
            f"unknown quote provider {feed!r}; trusted NBBO feeds are "
            f"{sorted(TRUSTED_NBBO_FEEDS)}")
    return f


def is_trusted_nbbo(feed) -> bool:
    """True only for an explicitly allowlisted NBBO identity. Never raises --
    an unknown provider is simply not trusted."""
    try:
        return normalize_feed(feed) in TRUSTED_NBBO_FEEDS
    except FeedProvenanceError:
        return False


@dataclasses.dataclass(frozen=True)
class QuoteProvenance:
    """Retained on every pricing decision and broker-event comparison."""
    feed: str
    trusted_nbbo: bool
    exchange_ts: Optional[str]
    receipt_ts: Optional[str]
    age_seconds: Optional[float]
    stream_generation: Optional[int]
    stale: bool
    reason: Optional[str] = None

    def as_dict(self) -> dict:
        return dataclasses.asdict(self)


def evaluate_quote_provenance(feed, *, exchange_ts=None, receipt_ts=None,
                              age_seconds=None, stream_generation=None,
                              max_age_seconds: float = 10.0) -> QuoteProvenance:
    """One place that decides whether a quote may price a decision, and
    records why. Staleness is a first-class refusal reason, not a silent
    pass: a fresh-looking NBBO label on a 45-second-old snapshot is exactly
    the combination that must not price a stop."""
    reason = None
    try:
        canonical = normalize_feed(feed)
        trusted = canonical in TRUSTED_NBBO_FEEDS
        if not trusted:
            reason = f"{canonical} is not an NBBO feed"
    except FeedProvenanceError as e:
        canonical, trusted = "unknown", False
        reason = str(e)

    stale = age_seconds is None or age_seconds > max_age_seconds
    if stale and reason is None:
        reason = (f"quote age {age_seconds}s exceeds {max_age_seconds}s ceiling"
                  if age_seconds is not None else "quote age unknown")

    return QuoteProvenance(
        feed=canonical, trusted_nbbo=trusted, exchange_ts=exchange_ts,
        receipt_ts=receipt_ts,
        age_seconds=None if age_seconds is None else round(float(age_seconds), 4),
        stream_generation=stream_generation, stale=stale, reason=reason)


def may_price_decision(prov: QuoteProvenance) -> bool:
    """A quote may price an entry or exit only if it is BOTH a trusted NBBO
    identity AND fresh."""
    return bool(prov.trusted_nbbo and not prov.stale)
