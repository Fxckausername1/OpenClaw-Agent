"""Trade classification and dedup -- TD-STD Section 5.

classify_trades() turns a raw option_history_trade_quote DataFrame (columns
confirmed live: symbol, expiration, strike, right, trade_timestamp,
quote_timestamp, sequence, condition, size, exchange, price, bid_size,
bid_exchange, bid, bid_condition, ask_size, ask_exchange, ask, ask_condition)
into a normalized frame carrying contract_id, classification,
classification_confidence, and excluded_reason.
"""

from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd

from .schemas import (
    CLASS_ASK, CLASS_BID, CLASS_COMPLEX, CLASS_MID, CLASS_OUTSIDE, CLASS_STALE,
    SIMPLE_TRADE_CONDITIONS, contract_id, normalize_right, parse_expiration,
)

STALE_QUOTE_AGE_MS = 3000  # matched quote older than this is not trustworthy for direction
EDGE_PROXIMITY = 0.0005  # within 0.05% of bid/ask (or exactly at it) counts as "at that side"


def _quote_age_ms(trade_ts: pd.Series, quote_ts: pd.Series) -> pd.Series:
    delta = (trade_ts - quote_ts).dt.total_seconds() * 1000.0
    return delta.clip(lower=0)


def add_contract_id(raw: pd.DataFrame) -> pd.DataFrame:
    """Adds contract_id plus normalized underlying/right columns.

    REAL BUG this fixes, found live: ThetaData's raw response names the
    ticker column `symbol` (confirmed via a live pull) and `right` as the
    full word "CALL"/"PUT" -- but every downstream aggregate function
    (wall_aggregates, accumulate_contract_stats) was written against
    `underlying`/normalized single-letter `right`, matching this schema
    module's own contract_id()/ContractIdentity naming. A synthetic test
    fixture happened to use "underlying" as its field name, masking the
    mismatch until a real backfill run hit a genuine KeyError. Adding both
    normalized columns here, once, is cheaper and less error-prone than
    fixing every downstream reference separately."""
    if raw.empty:
        out = raw.copy()
        for col in ("contract_id", "underlying"):
            out[col] = pd.Series(dtype="object")
        return out
    df = raw.copy()
    df["contract_id"] = [
        contract_id(row.symbol, parse_expiration(row.expiration), row.strike, row.right)
        for row in df.itertuples()
    ]
    df["underlying"] = df["symbol"]
    df["right"] = df["right"].map(normalize_right)
    return df


def classify_trades(raw: pd.DataFrame) -> pd.DataFrame:
    """Returns a copy of `raw` with contract_id/classification/
    classification_confidence/excluded_reason columns added. Never drops a
    row -- ambiguous/complex/stale trades are kept and labeled, per TD-STD
    Section 3's "Normalizer must not do: Discard ambiguous events silently."
    Always recomputes contract_id (cheap, pure function of
    symbol/expiration/strike/right) rather than trusting a caller-supplied
    column -- see dedup_trades()'s docstring for the exact bug this avoids.
    """
    if raw.empty:
        out = add_contract_id(raw)
        for col in ("classification", "classification_confidence", "excluded_reason"):
            out[col] = pd.Series(dtype="object")
        return out

    df = add_contract_id(raw)

    quote_age_ms = _quote_age_ms(df["trade_timestamp"], df["quote_timestamp"])
    df["quote_age_ms"] = quote_age_ms

    bid = df["bid"].to_numpy(dtype=float)
    ask = df["ask"].to_numpy(dtype=float)
    price = df["price"].to_numpy(dtype=float)
    condition = df["condition"].to_numpy(dtype=int)

    crossed_or_locked = (bid >= ask) & (bid > 0) & (ask > 0)
    invalid_quote = (bid <= 0) | (ask <= 0) | np.isnan(bid) | np.isnan(ask)
    stale = (quote_age_ms.to_numpy() > STALE_QUOTE_AGE_MS)
    complex_condition = ~np.isin(condition, list(SIMPLE_TRADE_CONDITIONS))

    at_ask = np.abs(price - ask) <= np.maximum(EDGE_PROXIMITY * ask, 0.005)
    at_bid = np.abs(price - bid) <= np.maximum(EDGE_PROXIMITY * bid, 0.005)
    outside = (price > ask * (1 + 0.0001)) | (price < bid * (1 - 0.0001))
    outside = outside & ~invalid_quote

    classification = np.full(len(df), CLASS_MID, dtype=object)
    excluded_reason = np.full(len(df), None, dtype=object)

    # Priority: complex/auction > invalid/crossed quote > stale > outside > at-ask/at-bid > mid.
    classification[:] = CLASS_MID
    classification[at_ask] = CLASS_ASK
    classification[at_bid & ~at_ask] = CLASS_BID
    classification[outside] = CLASS_OUTSIDE
    excluded_reason[outside] = "trade_outside_nbbo"
    classification[stale] = CLASS_STALE
    excluded_reason[stale] = "quote_age_over_threshold"
    classification[invalid_quote | crossed_or_locked] = CLASS_OUTSIDE
    excluded_reason[invalid_quote] = "invalid_quote"
    excluded_reason[crossed_or_locked] = "crossed_or_locked_market"
    classification[complex_condition] = CLASS_COMPLEX
    excluded_reason[complex_condition] = "non_simple_trade_condition"

    df["classification"] = classification
    # Confidence is binary: 1.0 for a clean ASK/BID directional read, 0.0
    # otherwise (MID/OUTSIDE/STALE/COMPLEX make no directional claim to be
    # confident about).
    df["classification_confidence"] = np.where(
        np.isin(classification, [CLASS_ASK, CLASS_BID]), 1.0, 0.0,
    )
    df["excluded_reason"] = excluded_reason

    return df


def dedup_trades(df: pd.DataFrame) -> pd.DataFrame:
    """Idempotent re-collection guard: cron cycles may re-request an
    overlapping [start_time, end_time] window on purpose (safer than risking
    a gap), so drop exact duplicates on (contract_id, sequence).

    ALWAYS recomputes contract_id rather than trusting a pre-existing
    column, even if one is already present. Real bug this avoids (caught
    via a live backfill run, not a mock): append_raw() concatenates an
    `existing` parquet partition (which, after the fix below, already
    carries contract_id from a prior cycle) with freshly-pulled `new_rows`
    (raw, no contract_id yet) before calling this function -- pandas concat
    unions the columns, so a naive "add it only if the column is missing"
    check would see the column already present (with NaN for every new row)
    and skip recomputing it, silently corrupting every row pulled after the
    first cycle."""
    if df.empty or "sequence" not in df.columns:
        return df
    df = add_contract_id(df)
    return df.drop_duplicates(subset=["contract_id", "sequence"], keep="first").reset_index(drop=True)


def coverage_stats(classified: pd.DataFrame) -> dict:
    """trade_classification_coverage / ambiguous_trade_fraction for the
    required source-health object -- TD-STD Section 3."""
    n = len(classified)
    if n == 0:
        return {"trade_classification_coverage": None, "ambiguous_trade_fraction": None}
    directional = classified["classification"].isin([CLASS_ASK, CLASS_BID]).sum()
    ambiguous = (classified["classification"] == CLASS_MID).sum()
    return {
        "trade_classification_coverage": float(directional) / n,
        "ambiguous_trade_fraction": float(ambiguous) / n,
    }
