"""One-minute bars, V/OI, and bid/ask fractions -- TD-STD Section 6.

v_oi = classified_and_total_intraday_volume / prior_session_open_interest
bid_fraction = classified_bid_volume / classified_edge_volume
ask_fraction = classified_ask_volume / classified_edge_volume
"""

from __future__ import annotations

import pandas as pd

from .schemas import (
    CLASS_ASK, CLASS_BID, CLASS_MID, WALL_CONFIRMED_DISMANTLING, WALL_FRAGILE,
    WALL_GHOST_CANDIDATE, WALL_INSUFFICIENT_DATA, WALL_REINFORCED,
    WALL_REJECTED_GHOST, WALL_STABLE,
)

MIN_ESTABLISHED_OI = 100  # matches gex_quant_engine.py's flag_ghost_wall floor convention
GHOST_CONFIRMATION_OI_DROP_PCT = 0.15  # TD-5: a >=15% next-day OI decrease counts as real closing
                                        # activity rather than ordinary day-to-day inventory noise.
                                        # Itself an initial research choice, not a validated number --
                                        # same "pending TD-5 calibration" status as the wall-state
                                        # thresholds this function exists to help calibrate.


def minute_bars(classified: pd.DataFrame) -> pd.DataFrame:
    """Per-contract, per-minute flow: signed contract counts by side,
    premium notional, and a coverage ratio. Feeds features.py's options CVD
    and flow-acceleration measures."""
    if classified.empty:
        return pd.DataFrame(columns=[
            "contract_id", "minute", "ask_contracts", "bid_contracts",
            "mid_contracts", "excluded_contracts", "ask_premium", "bid_premium",
            "coverage",
        ])

    df = classified.copy()
    df["minute"] = df["trade_timestamp"].dt.floor("min")
    df["premium"] = df["price"] * df["size"] * 100

    def _agg(group: pd.DataFrame) -> pd.Series:
        ask = group.loc[group["classification"] == CLASS_ASK, "size"].sum()
        bid = group.loc[group["classification"] == CLASS_BID, "size"].sum()
        mid = group.loc[group["classification"] == CLASS_MID, "size"].sum()
        directional_mask = group["classification"].isin([CLASS_ASK, CLASS_BID])
        excluded = int((~directional_mask).sum())
        ask_premium = group.loc[group["classification"] == CLASS_ASK, "premium"].sum()
        bid_premium = group.loc[group["classification"] == CLASS_BID, "premium"].sum()
        total = len(group)
        coverage = float(directional_mask.sum()) / total if total else None
        return pd.Series({
            "ask_contracts": ask, "bid_contracts": bid, "mid_contracts": mid,
            "excluded_contracts": excluded, "ask_premium": ask_premium,
            "bid_premium": bid_premium, "coverage": coverage,
        })

    out = df.groupby(["contract_id", "minute"], as_index=False).apply(_agg, include_groups=False)
    return out.reset_index(drop=True)


def wall_aggregates(classified: pd.DataFrame, prior_session_oi: dict[str, float]) -> pd.DataFrame:
    """Per (underlying, expiration, strike, right): v_oi, bid_fraction,
    ask_fraction, ambiguous_fraction. `prior_session_oi` maps contract_id ->
    prior-session open interest (Section 6: 'Use prior-session OI as the
    established inventory denominator')."""
    cols = [
        "contract_id", "underlying", "expiration", "strike", "right",
        "intraday_volume", "classified_volume", "oi", "v_oi",
        "bid_fraction", "ask_fraction", "ambiguous_fraction", "established_oi",
    ]
    if classified.empty:
        return pd.DataFrame(columns=cols)

    grouped = classified.groupby(
        ["contract_id", "underlying", "expiration", "strike", "right"], as_index=False
    ).apply(_wall_agg_one, include_groups=False)
    grouped = grouped.reset_index(drop=True)

    grouped["oi"] = grouped["contract_id"].map(prior_session_oi)
    grouped["established_oi"] = grouped["oi"].fillna(0) >= MIN_ESTABLISHED_OI
    grouped["v_oi"] = grouped.apply(
        lambda r: (r["intraday_volume"] / r["oi"]) if r["oi"] and r["oi"] > 0 else None, axis=1
    )
    return grouped[cols]


def _wall_agg_one(group: pd.DataFrame) -> pd.Series:
    # REAL BUG, found via a non-uniform-size regression test (an earlier
    # fixture used the same size for every row in a class, which made
    # row-count and volume-sum ratios coincidentally identical and masked
    # this): (classification == X).sum() on a boolean Series counts ROWS
    # (trade ticks), not contract/share VOLUME. TD-STD Section 6's v_oi and
    # this file's own module docstring both define these as volume ratios
    # ("classified_bid_volume / classified_edge_volume"), so every fraction
    # here must be size-weighted, matching intraday_volume's existing
    # (already-correct) group["size"].sum().
    total = int(group["size"].sum())
    ask = int(group.loc[group["classification"] == CLASS_ASK, "size"].sum())
    bid = int(group.loc[group["classification"] == CLASS_BID, "size"].sum())
    mid = int(group.loc[group["classification"] == CLASS_MID, "size"].sum())
    edge = ask + bid
    return pd.Series({
        "intraday_volume": total,
        "classified_volume": edge,
        "bid_fraction": (bid / edge) if edge else None,
        "ask_fraction": (ask / edge) if edge else None,
        "ambiguous_fraction": (mid / total) if total else None,
    })


def accumulate_contract_stats(existing: dict, classified_batch: pd.DataFrame) -> dict:
    """Incrementally folds ONE cycle's small classified batch into a
    persisted per-contract running-total dict (ask/bid/mid/total counts).

    REAL DESIGN FIX, not a micro-optimization: v_oi needs cumulative
    session-to-date volume (TD-STD Section 6), which first tempted a
    "re-read the whole day's raw partition every cycle" approach --
    live-tested against real SPY 2026-07-24 data, a single day's
    near-money-windowed trade+quote history came back as 1,235,319 rows,
    and concatenating/deduping that every ~5 minutes crashed with a real
    numpy ArrayMemoryError on this 1.9GB box. This function keeps only a
    tiny per-contract counter (a few ints per contract_id, not
    million-row DataFrames) persisted across cycles, updated with just the
    NEW rows each cycle -- the standard incremental-aggregator pattern
    TD-STD Section 3 itself specifies ('Aggregator: must not overwrite raw
    records' implies its own running state, not a from-scratch rebuild).

    Mutates and returns `existing`."""
    if classified_batch.empty:
        return existing

    # sum() of the size COLUMN (contract/share volume), not groupby's own
    # .size() method (row/trade-tick count) -- a real bug this exact naming
    # collision caused on the first pass, caught by a test whose fixture
    # used a size != 1 (row-count and volume only look identical at size=1).
    counts = (
        classified_batch.groupby(["contract_id", "classification"])["size"].sum().unstack(fill_value=0)
    )
    meta = classified_batch[["contract_id", "underlying", "expiration", "strike", "right"]].drop_duplicates("contract_id").set_index("contract_id")

    for cid, row in counts.iterrows():
        entry = existing.setdefault(cid, {
            "underlying": meta.loc[cid, "underlying"],
            "expiration": str(meta.loc[cid, "expiration"]),
            "strike": float(meta.loc[cid, "strike"]),
            "right": meta.loc[cid, "right"],
            "ask": 0, "bid": 0, "mid": 0, "total": 0,
        })
        entry["ask"] += int(row.get(CLASS_ASK, 0))
        entry["bid"] += int(row.get(CLASS_BID, 0))
        entry["mid"] += int(row.get(CLASS_MID, 0))
        entry["total"] += int(row.sum())
    return existing


def wall_aggregates_from_stats(stats: dict, prior_session_oi: dict[str, float]) -> pd.DataFrame:
    """Same output shape/logic as wall_aggregates(), but built from the
    small cumulative stats dict (accumulate_contract_stats) instead of a
    full trade-level DataFrame -- O(contract_count), not O(day_size)."""
    cols = [
        "contract_id", "underlying", "expiration", "strike", "right",
        "intraday_volume", "classified_volume", "oi", "v_oi",
        "bid_fraction", "ask_fraction", "ambiguous_fraction", "established_oi",
    ]
    if not stats:
        return pd.DataFrame(columns=cols)

    rows = []
    for cid, s in stats.items():
        ask, bid, mid, total = s["ask"], s["bid"], s["mid"], s["total"]
        edge = ask + bid
        oi = prior_session_oi.get(cid)
        established = bool(oi) and oi >= MIN_ESTABLISHED_OI
        rows.append({
            "contract_id": cid, "underlying": s["underlying"], "expiration": s["expiration"],
            "strike": s["strike"], "right": s["right"],
            "intraday_volume": total, "classified_volume": edge, "oi": oi,
            "v_oi": (total / oi) if established else None,
            "bid_fraction": (bid / edge) if edge else None,
            "ask_fraction": (ask / edge) if edge else None,
            "ambiguous_fraction": (mid / total) if total else None,
            "established_oi": established,
        })
    return pd.DataFrame(rows, columns=cols)


def classify_wall_state(row: pd.Series) -> str:
    """Wall-state machine -- TD-STD Section 6 / CB-V4 Section 5. Thresholds
    (v_oi>1.05, bid/ask_fraction>0.70 for Ghost/Reinforced; a lower band for
    Fragile) are the only concrete numbers CB-V4's own doc gives; treat as
    initial research thresholds pending TD-5 calibration, not a validated
    edge."""
    if not row.get("established_oi") or row.get("v_oi") is None:
        return WALL_INSUFFICIENT_DATA
    v_oi = row["v_oi"]
    bid_frac = row.get("bid_fraction")
    ask_frac = row.get("ask_fraction")
    if bid_frac is None or ask_frac is None:
        return WALL_INSUFFICIENT_DATA
    if v_oi > 1.05 and bid_frac > 0.70:
        return WALL_GHOST_CANDIDATE
    if v_oi > 1.05 and ask_frac > 0.70:
        return WALL_REINFORCED
    if v_oi > 0.5 and 0.55 < bid_frac <= 0.70:
        return WALL_FRAGILE
    return WALL_STABLE


def label_next_day_confirmation(wall_df: pd.DataFrame, next_day_oi: dict[str, float]) -> pd.DataFrame:
    """TD-STD Section 6's 'Next-day validation: Compare new OI; confirm,
    reject or leave unresolved' -- TD-5's ground truth for Ghost Wall
    calibration. WALL_CONFIRMED_DISMANTLING/WALL_REJECTED_GHOST have existed
    as named constants in schemas.py since TD-1..TD-4 but were never
    actually computed anywhere until this function.

    Only rows already classified WALL_GHOST_CANDIDATE get a verdict; every
    other row's `next_day_confirmation_status` is None (not applicable, not
    "rejected" -- a stable/reinforced wall was never a ghost candidate to
    confirm or reject in the first place). Missing next-day OI (last day in
    a backfill window, or the contract expired) leaves the status None too,
    honestly, rather than guessing which way an absent data point would
    have gone."""
    out = wall_df.copy()
    out["next_day_confirmation_status"] = None
    if out.empty:
        return out

    def _status(row: pd.Series):
        if row.get("wall_state") != WALL_GHOST_CANDIDATE:
            return None
        prior_oi = row.get("oi")
        new_oi = next_day_oi.get(row.get("contract_id"))
        if not prior_oi or prior_oi <= 0 or new_oi is None:
            return None
        drop_pct = (prior_oi - new_oi) / prior_oi
        if drop_pct >= GHOST_CONFIRMATION_OI_DROP_PCT:
            return WALL_CONFIRMED_DISMANTLING
        return WALL_REJECTED_GHOST

    out["next_day_confirmation_status"] = out.apply(_status, axis=1)
    return out
