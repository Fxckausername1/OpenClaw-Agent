#!/usr/bin/env python3
"""Evaluate pre-registered MR/ORB signal-quality filters with point-in-time features."""

from __future__ import annotations

import json
import os
import statistics
from collections import defaultdict
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

import pandas as pd

from candidate_replay import (
    CACHE,
    COST_BPS,
    DATA,
    SOURCE,
    atomic_write,
    bar_time,
    bars_from_frame,
    build_regimes,
    compact_row,
    number,
    simulate_next_open,
)


ROOT = Path(__file__).resolve().parent
OUT_DIR = DATA / "research/signal_quality"
SECTOR_MAP = DATA / "sector_map.json"
CONTRACT_VERSION = "signal-quality-2026-07-13.1"


CANDIDATES = (
    # MR primary family.
    {"name": "mr_sq_next_open_eod", "strategy": "MR", "selection_eligible": True},
    {"name": "mr_sq_reclaim", "strategy": "MR", "reclaim": True, "selection_eligible": True},
    {
        "name": "mr_sq_reclaim_location",
        "strategy": "MR",
        "reclaim": True,
        "close_location": 0.70,
        "selection_eligible": True,
    },
    {
        "name": "mr_sq_exhaustion2_reclaim",
        "strategy": "MR",
        "reclaim": True,
        "exhaustion_score": 2,
        "selection_eligible": True,
    },
    {
        "name": "mr_sq_exhaustion2_reclaim_volume125",
        "strategy": "MR",
        "reclaim": True,
        "exhaustion_score": 2,
        "volume_ratio": 1.25,
        "selection_eligible": True,
    },
    {"name": "mr_sq_breadth_turn", "strategy": "MR", "breadth_turn": True, "selection_eligible": True},
    {
        "name": "mr_sq_combined",
        "strategy": "MR",
        "reclaim": True,
        "close_location": 0.70,
        "exhaustion_score": 2,
        "breadth_turn": True,
        "selection_eligible": True,
    },
    # MR neighborhood check, excluded from walk-forward selection.
    {
        "name": "mr_sq_exhaustion3_reclaim",
        "strategy": "MR",
        "reclaim": True,
        "exhaustion_score": 3,
        "selection_eligible": False,
    },
    # ORB primary family.
    {"name": "orb_sq_next_open_eod", "strategy": "ORB", "selection_eligible": True},
    {"name": "orb_sq_gap003", "strategy": "ORB", "gap": 0.003, "selection_eligible": True},
    {"name": "orb_sq_drive05", "strategy": "ORB", "drive": 0.50, "selection_eligible": True},
    {"name": "orb_sq_breadth55", "strategy": "ORB", "breadth": 0.55, "selection_eligible": True},
    {"name": "orb_sq_sector55", "strategy": "ORB", "sector_breadth": 0.55, "selection_eligible": True},
    {
        "name": "orb_sq_gap003_drive03",
        "strategy": "ORB",
        "gap": 0.003,
        "drive": 0.30,
        "selection_eligible": True,
    },
    {
        "name": "orb_sq_combined",
        "strategy": "ORB",
        "gap": 0.003,
        "drive": 0.30,
        "breadth": 0.55,
        "sector_breadth": 0.55,
        "selection_eligible": True,
    },
    # ORB threshold-neighborhood checks, excluded from walk-forward selection.
    {"name": "orb_sq_gap002", "strategy": "ORB", "gap": 0.002, "selection_eligible": False},
    {"name": "orb_sq_gap005", "strategy": "ORB", "gap": 0.005, "selection_eligible": False},
    {"name": "orb_sq_drive03", "strategy": "ORB", "drive": 0.30, "selection_eligible": False},
)


def columns(frame: pd.DataFrame) -> tuple[str, str, str]:
    open_col = "Open" if "Open" in frame.columns else "open"
    close_col = "Close" if "Close" in frame.columns else "close"
    volume_col = "Volume" if "Volume" in frame.columns else "volume"
    return open_col, close_col, volume_col


def normalized_frame(path: Path) -> pd.DataFrame:
    frame = pd.read_parquet(path).sort_index().copy()
    index = pd.DatetimeIndex(frame.index)
    if index.tz is not None:
        index = index.tz_localize(None)
    frame.index = index
    return frame


def build_breadth(symbols: list[str], timestamps: set[datetime], sector_map: dict) -> tuple[dict, dict, dict]:
    needed = pd.DatetimeIndex(sorted(timestamps))
    market_counts = defaultdict(lambda: [0, 0])
    sector_counts = defaultdict(lambda: [0, 0])
    errors = {}
    for symbol in symbols:
        try:
            frame = normalized_frame(CACHE / f"{symbol}.parquet")
            open_col, close_col, _ = columns(frame)
            day_keys = pd.Series(frame.index.date, index=frame.index)
            day_open = frame[open_col].groupby(day_keys).transform("first")
            selected = frame.index.intersection(needed)
            if selected.empty:
                continue
            direction = frame.loc[selected, close_col].astype(float) > day_open.loc[selected].astype(float)
            sector = sector_map.get(symbol)
            for timestamp, is_up in direction.items():
                key = timestamp.to_pydatetime()
                market_counts[key][0] += int(bool(is_up))
                market_counts[key][1] += 1
                if sector:
                    sector_counts[(key, sector)][0] += int(bool(is_up))
                    sector_counts[(key, sector)][1] += 1
        except Exception as exc:
            errors[symbol] = str(exc)
    market = {
        timestamp: up / count
        for timestamp, (up, count) in market_counts.items()
        if count >= 50
    }
    sector = {
        key: up / count
        for key, (up, count) in sector_counts.items()
        if count >= 3
    }
    return market, sector, errors


def signed_pass(value: float | None, side: str, long_threshold: float) -> bool:
    if value is None:
        return False
    return value >= long_threshold if side == "LONG" else value <= 1.0 - long_threshold


def aligned_magnitude(value: float | None, side: str, threshold: float) -> bool:
    if value is None:
        return False
    return value >= threshold if side == "LONG" else value <= -threshold


def extract_features(
    source: dict,
    bars: list[dict],
    prior_close: float | None,
    market_breadth: dict,
    sector_breadth: dict,
    sector: str | None,
) -> dict:
    index = int(source["signal_index"])
    signal = bars[index]
    side = source["side"]
    direction = 1.0 if side == "LONG" else -1.0
    close = number(signal, "close", "Close")
    high = number(signal, "high", "High")
    low = number(signal, "low", "Low")
    boundary = float(source["entry"])
    span = max(high - low, 1e-9)
    raw_location = (close - low) / span
    aligned_location = raw_location if side == "LONG" else 1.0 - raw_location
    reclaim = close > boundary if side == "LONG" else close < boundary

    window = bars[max(0, index - 12) : index + 1]
    z_values = [number(bar, "z") for bar in window]
    rsi_values = [number(bar, "rsi") for bar in window]
    vdev_values = [number(bar, "vwap_dev") for bar in window]
    z_values = [value for value in z_values if value is not None]
    rsi_values = [value for value in rsi_values if value is not None]
    vdev_values = [value for value in vdev_values if value is not None]
    exhaustion_checks = []
    if side == "LONG":
        exhaustion_checks = [
            bool(z_values and min(z_values) <= -2.0),
            bool(rsi_values and min(rsi_values) <= 25.0),
            bool(vdev_values and min(vdev_values) <= -0.02),
        ]
    else:
        exhaustion_checks = [
            bool(z_values and max(z_values) >= 2.0),
            bool(rsi_values and max(rsi_values) >= 75.0),
            bool(vdev_values and max(vdev_values) >= 0.02),
        ]

    prior_volumes = [number(bar, "volume", "Volume") for bar in bars[max(0, index - 20) : index]]
    prior_volumes = [value for value in prior_volumes if value is not None and value > 0]
    signal_volume = number(signal, "volume", "Volume")
    volume_ratio = signal_volume / statistics.median(prior_volumes) if signal_volume and prior_volumes else None

    opening = [bar for bar in bars if bar_time(bar).time() < time(9, 45)]
    gap = drive = None
    if opening:
        first_open = number(opening[0], "open", "Open")
        last_close = number(opening[-1], "close", "Close")
        opening_high = max(number(bar, "high", "High") for bar in opening)
        opening_low = min(number(bar, "low", "Low") for bar in opening)
        opening_range = opening_high - opening_low
        if prior_close and first_open:
            gap = first_open / prior_close - 1.0
        if opening_range > 0:
            drive = (last_close - first_open) / opening_range

    timestamp = datetime.fromisoformat(source["signal_time"]).replace(tzinfo=None)
    breadth = market_breadth.get(timestamp)
    previous_breadth = market_breadth.get(timestamp - timedelta(minutes=5))
    breadth_delta = breadth - previous_breadth if breadth is not None and previous_breadth is not None else None
    sector_value = sector_breadth.get((timestamp, sector)) if sector else None
    breadth_turn = False
    if breadth is not None and breadth_delta is not None:
        breadth_turn = (
            breadth <= 0.40 and breadth_delta >= 0.03
            if side == "LONG"
            else breadth >= 0.60 and breadth_delta <= -0.03
        )

    return {
        "reclaim": reclaim,
        "aligned_close_location": aligned_location,
        "exhaustion_score": sum(exhaustion_checks),
        "volume_ratio": volume_ratio,
        "overnight_gap": gap,
        "aligned_gap": gap * direction if gap is not None else None,
        "opening_drive": drive,
        "aligned_drive": drive * direction if drive is not None else None,
        "market_breadth": breadth,
        "breadth_delta": breadth_delta,
        "breadth_turn": breadth_turn,
        "sector_breadth": sector_value,
    }


def passes(features: dict, source: dict, candidate: dict) -> bool:
    if candidate.get("reclaim") and not features["reclaim"]:
        return False
    if candidate.get("close_location") and features["aligned_close_location"] < float(candidate["close_location"]):
        return False
    if candidate.get("exhaustion_score") and features["exhaustion_score"] < int(candidate["exhaustion_score"]):
        return False
    if candidate.get("volume_ratio") and (features["volume_ratio"] is None or features["volume_ratio"] < float(candidate["volume_ratio"])):
        return False
    if candidate.get("breadth_turn") and not features["breadth_turn"]:
        return False
    if candidate.get("gap") and not aligned_magnitude(features["overnight_gap"], source["side"], float(candidate["gap"])):
        return False
    if candidate.get("drive") and not aligned_magnitude(features["opening_drive"], source["side"], float(candidate["drive"])):
        return False
    if candidate.get("breadth") and not signed_pass(features["market_breadth"], source["side"], float(candidate["breadth"])):
        return False
    if candidate.get("sector_breadth") and not signed_pass(features["sector_breadth"], source["side"], float(candidate["sector_breadth"])):
        return False
    return True


def summarize(rows: list[dict]) -> dict:
    result = {"intents": len(rows), "days": len({row["date"] for row in rows})}
    for bps in COST_BPS:
        values = [row[f"net_r_{bps}bp"] for row in rows]
        result[f"net_{bps}bp_avg_r"] = sum(values) / len(values) if values else None
    return result


def main() -> None:
    source_rows = [json.loads(line) for line in SOURCE.read_text().splitlines() if line.strip()]
    by_symbol = defaultdict(list)
    for row in source_rows:
        by_symbol[row["symbol"]].append(row)
    symbols = sorted(by_symbol)
    sector_map = json.loads(SECTOR_MAP.read_text()) if SECTOR_MAP.exists() else {}
    signal_timestamps = {
        datetime.fromisoformat(row["signal_time"]).replace(tzinfo=None) for row in source_rows
    }
    timestamps = signal_timestamps | {timestamp - timedelta(minutes=5) for timestamp in signal_timestamps}
    market_breadth, sector_breadth, breadth_errors = build_breadth(symbols, timestamps, sector_map)
    regimes = build_regimes()
    candidates_by_strategy = defaultdict(list)
    for candidate in CANDIDATES:
        candidates_by_strategy[candidate["strategy"]].append(candidate)

    output_rows = []
    exclusions = defaultdict(int)
    errors = []
    feature_coverage = defaultdict(int)
    for symbol in symbols:
        try:
            frame = normalized_frame(CACHE / f"{symbol}.parquet")
            _, close_col, _ = columns(frame)
            grouped = list(frame.groupby(frame.index.date, sort=True))
            daily = {str(day): bars_from_frame(group) for day, group in grouped}
            prior_close_by_day = {}
            prior_close = None
            for day, group in grouped:
                day_s = str(day)
                prior_close_by_day[day_s] = prior_close
                prior_close = float(group.iloc[-1][close_col])
        except Exception as exc:
            errors.append({"symbol": symbol, "error": str(exc)})
            continue
        for source in by_symbol[symbol]:
            bars = daily.get(source["date"], [])
            if not bars or int(source["signal_index"]) >= len(bars):
                errors.append({"symbol": symbol, "date": source["date"], "error": "missing signal bar"})
                continue
            features = extract_features(
                source,
                bars,
                prior_close_by_day.get(source["date"]),
                market_breadth,
                sector_breadth,
                sector_map.get(symbol),
            )
            for key, value in features.items():
                if value is not None:
                    feature_coverage[key] += 1
            outcome = simulate_next_open(bars, source, target_rr=None)
            for candidate in candidates_by_strategy[source["strategy"]]:
                if not passes(features, source, candidate):
                    exclusions[candidate["name"]] += 1
                    continue
                row = compact_row(source, {**candidate, "entry_mode": "next_open"}, regimes.get(source["date"], "unknown"), outcome)
                row.update({key: value for key, value in features.items() if value is not None})
                output_rows.append(row)

    created = datetime.now(timezone.utc)
    run_id = f"signal_quality_{created.strftime('%Y%m%dT%H%M%SZ')}"
    report = {
        "run_id": run_id,
        "created_at": created.isoformat(timespec="seconds"),
        "contract_version": CONTRACT_VERSION,
        "source": str(SOURCE),
        "candidate_count": len(CANDIDATES),
        "candidates": list(CANDIDATES),
        "feature_coverage": dict(feature_coverage),
        "breadth_timestamps": len(market_breadth),
        "sector_breadth_points": len(sector_breadth),
        "breadth_errors": breadth_errors,
        "exclusions": dict(exclusions),
        "errors": errors,
        "results": {
            candidate["name"]: summarize([row for row in output_rows if row["candidate"] == candidate["name"]])
            for candidate in CANDIDATES
        },
        "limitations": [
            "only seven true premarket snapshots exist, so overnight gap is derived from prior close to regular-session open and premarket volume is not tested",
            "breadth uses the current cached/static universe rather than point-in-time constituents",
            "sector breadth uses mapped cached symbols and requires at least three members per timestamp",
            "candidate family was designed after baseline review and remains discovery-contaminated",
            "five-minute OHLCV uses pessimistic stop-first same-bar ordering",
        ],
    }
    payload = "".join(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n" for row in output_rows)
    atomic_write(OUT_DIR / f"{run_id}.trades.jsonl", payload)
    atomic_write(OUT_DIR / f"{run_id}.summary.json", json.dumps(report, indent=2, sort_keys=True) + "\n")
    atomic_write(OUT_DIR / "latest.trades.jsonl", payload)
    atomic_write(OUT_DIR / "latest.summary.json", json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
