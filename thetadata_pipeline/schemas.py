"""Canonical contract identity, classification labels, and record shapes.

TD-STD Section 5 (canonical raw and normalized schemas) and Section 6 (wall
feature record). Every derived record carries SCHEMA_VERSION so a later
replay/calibration pass can tell which record shape produced it.
"""

from __future__ import annotations

import datetime as dt
from typing import Optional

from . import SCHEMA_VERSION

# --- Contract identity -------------------------------------------------

def strike_to_mills(strike: float) -> int:
    """OCC-style integer strike (dollars * 1000). Avoids float-equality bugs
    when the same strike arrives from different endpoints/precisions."""
    return int(round(float(strike) * 1000))


def mills_to_strike(strike_mills: int) -> float:
    return strike_mills / 1000.0


def normalize_right(right: str) -> str:
    """ThetaData returns 'CALL'/'PUT' from some endpoints and 'C'/'P' from
    others (observed live, not assumed) -- collapse to a single letter."""
    r = right.strip().upper()
    if r.startswith("C"):
        return "C"
    if r.startswith("P"):
        return "P"
    raise ValueError(f"unrecognized option right: {right!r}")


def contract_id(underlying: str, expiration: dt.date, strike: float, right: str) -> str:
    """underlying + expiration + strike_mills + right, per TD-STD Section 5.
    e.g. SPY_20260724_000750000_C -- sorts and greps cleanly."""
    return f"{underlying}_{expiration:%Y%m%d}_{strike_to_mills(strike):09d}_{normalize_right(right)}"


def occ_symbol(underlying: str, expiration: dt.date, strike: float, right: str) -> str:
    """True OCC option symbol -- root + YYMMDD + C/P + strike*1000 zero-padded
    to 8 digits (e.g. SPY260724C00750000). Distinct from contract_id() above
    (this project's own internal join key): BT-1's manifest needs the real
    OCC symbol recorded per record, matching the format journal-save.js's
    OCC_SYMBOL_RE already validates against on the dashboard side."""
    strike_field = strike_to_mills(strike)
    if strike_field < 0 or strike_field > 99_999_999:
        raise ValueError(f"strike {strike} out of OCC-representable range")
    return f"{underlying}{expiration:%y%m%d}{normalize_right(right)}{strike_field:08d}"


def parse_expiration(value) -> dt.date:
    if isinstance(value, dt.date):
        return value
    return dt.date.fromisoformat(str(value)[:10])


# --- Trade classification (TD-STD Section 5) ----------------------------

CLASS_ASK = "ASK"          # buyer-initiated candidate
CLASS_BID = "BID"          # seller-initiated candidate
CLASS_MID = "MID"          # ambiguous; excluded from directional CVD
CLASS_OUTSIDE = "OUTSIDE"  # outside the valid spread; condition review
CLASS_STALE = "STALE"      # matched quote older than threshold
CLASS_COMPLEX = "COMPLEX"  # auction/multi-leg/complex-order-book/floor

ALL_CLASSIFICATIONS = (
    CLASS_ASK, CLASS_BID, CLASS_MID, CLASS_OUTSIDE, CLASS_STALE, CLASS_COMPLEX,
)

DIRECTIONAL_CLASSIFICATIONS = (CLASS_ASK, CLASS_BID)

# OPRA trade condition codes confirmed live against docs.thetadata.us's
# Trade-Conditions reference (2026-07-25): 0/1/10/14/18/45/95 are simple,
# single-leg regular executions eligible for aggressor-side classification.
# Everything else observed in real SPY 0DTE data (125-138) is an auction,
# multi-leg/complex-order-book, or floor execution and is routed to
# CLASS_COMPLEX regardless of its price-vs-NBBO position -- guessing a
# direction for those would misattribute flow that was never a simple
# customer buy/sell against the book.
SIMPLE_TRADE_CONDITIONS = frozenset({0, 1, 10, 14, 18, 45, 95})

# Wall states -- TD-STD Section 6 / CB-V4 Section 5.
WALL_REINFORCED = "REINFORCED"
WALL_STABLE = "STABLE"
WALL_FRAGILE = "FRAGILE"
WALL_GHOST_CANDIDATE = "GHOST_CANDIDATE"
WALL_CONFIRMED_DISMANTLING = "CONFIRMED_DISMANTLING"
WALL_REJECTED_GHOST = "REJECTED_GHOST"
WALL_INSUFFICIENT_DATA = "INSUFFICIENT_DATA"

QUALITY_FRESH = "FRESH"
QUALITY_DEGRADED = "DEGRADED"
QUALITY_UNAVAILABLE = "UNAVAILABLE"


def source_health(
    provider: str,
    observed_at: str,
    age_seconds: float,
    contracts_expected: int,
    contracts_received: int,
    trade_classification_coverage: Optional[float],
    ambiguous_trade_fraction: Optional[float],
    oi_as_of_session: Optional[str],
    quality: str,
) -> dict:
    """Required source-health object -- TD-STD Section 3 / CB-V4 Section 3."""
    return {
        "provider": provider,
        "observed_at": observed_at,
        "age_seconds": round(float(age_seconds), 3),
        "contracts_expected": int(contracts_expected),
        "contracts_received": int(contracts_received),
        "trade_classification_coverage": (
            round(float(trade_classification_coverage), 4)
            if trade_classification_coverage is not None else None
        ),
        "ambiguous_trade_fraction": (
            round(float(ambiguous_trade_fraction), 4)
            if ambiguous_trade_fraction is not None else None
        ),
        "oi_as_of_session": oi_as_of_session,
        "quality": quality,
        "schema_version": SCHEMA_VERSION,
    }
