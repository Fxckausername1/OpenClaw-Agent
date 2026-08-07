#!/usr/bin/env python3
"""Build deterministic forward ORB observations and report promotion readiness."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import statistics
from collections import defaultdict
from datetime import date, datetime, time as clock, timedelta, timezone
from pathlib import Path

from candidate_replay import simulate_next_open
from canonical_strategy_contracts import detect_orb
from premarket_forward_collector import ET, FORWARD_GATE, verify_manifest


ROOT = Path(__file__).resolve().parent
ARCHIVE = ROOT / "data/research/premarket_forward"
EVALUATION_DIR = ARCHIVE / "evaluation"
SECTOR_MAP_PATH = ROOT / "data/sector_map.json"
EVALUATION_PROTOCOL = ROOT / "PREMARKET_FORWARD_EVALUATION.md"
EVALUATION_VERSION = "premarket-evaluation-2026-07-13.2"
MIN_RELATIVE_VOLUME_BASELINE_DAYS = 20
REFERENCE_SYMBOLS = {"SPY", "XLB", "XLC", "XLE", "XLF", "XLI", "XLK", "XLP", "XLRE", "XLU", "XLV", "XLY"}
REQUIRED_FEATURES = (
    "prior_close",
    "last_price",
    "premarket_volume",
    "premarket_vwap",
    "premarket_range_frac",
    "late_30m_return",
    "spread_bps",
    "market_gap",
    "sector_gap",
)
COST_BPS = (6, 12)
CANDIDATES = (
    {"name": "orb_forward_baseline", "promotion_eligible": False},
    {"name": "orb_pm_gap003", "promotion_eligible": True, "gap": 0.003, "primary": True},
    {"name": "orb_pm_gap005", "promotion_eligible": True, "gap": 0.005, "robustness": True},
    {
        "name": "orb_pm_gap003_structure",
        "promotion_eligible": True,
        "gap": 0.003,
        "aligned_vwap": True,
        "aligned_late_30m": True,
        "max_spread_bps": 15.0,
    },
    {
        "name": "orb_pm_gap003_rvol150",
        "promotion_eligible": True,
        "gap": 0.003,
        "minimum_relative_volume": 1.5,
    },
    {
        "name": "orb_pm_gap003_market_sector",
        "promotion_eligible": True,
        "gap": 0.003,
        "market_confirmation": True,
        "sector_confirmation": True,
    },
    {
        "name": "orb_pm_combined",
        "promotion_eligible": True,
        "gap": 0.003,
        "minimum_relative_volume": 1.5,
        "aligned_vwap": True,
        "aligned_late_30m": True,
        "max_spread_bps": 15.0,
        "market_confirmation": True,
        "sector_confirmation": True,
    },
)


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def atomic_write(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(payload)
    temporary.replace(path)


def parse_timestamp(raw: str) -> datetime:
    return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(ET)


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def number(container: dict | None, key: str) -> float | None:
    if not container or container.get(key) is None:
        return None
    try:
        return float(container[key])
    except (TypeError, ValueError):
        return None


def snapshot_value(snapshot: dict | None, section: str, key: str) -> float | None:
    return number((snapshot or {}).get(section), key)


def valid_quote(snapshot: dict | None) -> tuple[bool, float | None]:
    quote = (snapshot or {}).get("latestQuote") or {}
    bid = number(quote, "bp")
    ask = number(quote, "ap")
    if bid is None or ask is None or bid <= 0 or ask <= bid:
        return False, None
    midpoint = (bid + ask) / 2.0
    return True, (ask - bid) / midpoint * 10000.0


def raw_capture_features(records: list[dict], manifest: dict) -> dict[str, dict]:
    bars_by_symbol_feed: dict[tuple[str, str], list[dict]] = defaultdict(list)
    snapshots_by_symbol_feed: dict[tuple[str, str], dict] = {}
    for record in records:
        symbol = record.get("symbol")
        if record.get("record_type") == "bar" and symbol:
            timestamp = parse_timestamp(record["bar"]["t"])
            if clock(4, 0) <= timestamp.time() < clock(9, 30):
                feed = record.get("feed") or "iex"
                bars_by_symbol_feed[(symbol, feed)].append(record["bar"])
        elif record.get("record_type") == "snapshot" and symbol:
            feed = record.get("feed") or "iex"
            snapshots_by_symbol_feed[(symbol, feed)] = record.get("snapshot") or {}

    output = {}
    for symbol in manifest.get("symbols", []):
        iex_bars = sorted(bars_by_symbol_feed.get((symbol, "iex"), []), key=lambda bar: bar["t"])
        sip_bars = sorted(bars_by_symbol_feed.get((symbol, "sip"), []), key=lambda bar: bar["t"])
        bars = sip_bars or iex_bars
        iex_snapshot = snapshots_by_symbol_feed.get((symbol, "iex"))
        delayed_snapshot = snapshots_by_symbol_feed.get((symbol, "delayed_sip"))
        snapshot = delayed_snapshot or iex_snapshot
        snapshot_trade = snapshot_value(iex_snapshot, "latestTrade", "p") or snapshot_value(delayed_snapshot, "latestTrade", "p")
        last_price = snapshot_trade or (number(iex_bars[-1], "c") if iex_bars else None) or (number(bars[-1], "c") if bars else None)
        late_start = parse_timestamp(bars[-1]["t"]) - timedelta(minutes=30) if bars else None
        volume = sum(number(bar, "v") or 0.0 for bar in bars)
        weighted = sum((number(bar, "vw") or number(bar, "c") or 0.0) * (number(bar, "v") or 0.0) for bar in bars)
        vwap = weighted / volume if volume > 0 else None
        highs = [number(bar, "h") for bar in bars]
        lows = [number(bar, "l") for bar in bars]
        highs = [value for value in highs if value is not None]
        lows = [value for value in lows if value is not None]
        prior_close = snapshot_value(snapshot, "prevDailyBar", "c")
        premarket_range = max(highs) - min(lows) if highs and lows else None
        range_frac = premarket_range / prior_close if premarket_range is not None and prior_close else None
        late = [bar for bar in bars if late_start is not None and parse_timestamp(bar["t"]) >= late_start]
        late_return = None
        late_range_ratio = None
        if len(late) >= 2:
            first = number(late[0], "o")
            last = number(late[-1], "c")
            if first and last:
                late_return = last / first - 1.0
            late_highs = [number(bar, "h") for bar in late if number(bar, "h") is not None]
            late_lows = [number(bar, "l") for bar in late if number(bar, "l") is not None]
            if late_highs and late_lows and premarket_range and premarket_range > 0:
                late_range_ratio = (max(late_highs) - min(late_lows)) / premarket_range
        quote_ok, spread_bps = valid_quote(snapshot)
        output[symbol] = {
            "symbol": symbol,
            "bar_count": len(bars),
            "feature_bar_feed": "sip" if sip_bars else "iex",
            "feature_cutoff": bars[-1]["t"] if bars else None,
            "prior_close": prior_close,
            "last_price": last_price,
            "overnight_gap": last_price / prior_close - 1.0 if last_price and prior_close else None,
            "premarket_volume": volume if bars else None,
            "premarket_vwap": vwap,
            "vwap_position": last_price / vwap - 1.0 if last_price and vwap else None,
            "premarket_range_frac": range_frac,
            "late_30m_return": late_return,
            "late_30m_range_ratio": late_range_ratio,
            "quote_valid": quote_ok,
            "spread_bps": spread_bps,
        }
    return output


def enrich_features(raw: dict[str, dict], sector_map: dict, volume_history: dict[str, list[float]]) -> dict[str, dict]:
    market_gap = (raw.get("SPY") or {}).get("overnight_gap")
    output = {}
    for symbol, feature in raw.items():
        sector = sector_map.get(symbol)
        history = volume_history.get(symbol, [])[-MIN_RELATIVE_VOLUME_BASELINE_DAYS:]
        current_volume = feature.get("premarket_volume")
        baseline = statistics.median(history) if len(history) >= MIN_RELATIVE_VOLUME_BASELINE_DAYS else None
        output[symbol] = {
            **feature,
            "sector_etf": sector,
            "market_gap": market_gap,
            "sector_gap": (raw.get(sector) or {}).get("overnight_gap") if sector else None,
            "relative_volume_baseline_days": len(history),
            "premarket_relative_volume": current_volume / baseline if current_volume is not None and baseline and baseline > 0 else None,
        }
    return output


def aggregate_regular_five_minute(records: list[dict]) -> dict[str, list[dict]]:
    buckets: dict[tuple[str, datetime], list[dict]] = defaultdict(list)
    for record in records:
        if record.get("record_type") != "bar":
            continue
        timestamp = parse_timestamp(record["bar"]["t"])
        if not (clock(9, 30) <= timestamp.time() < clock(16, 0)):
            continue
        bucket = timestamp.replace(minute=timestamp.minute - timestamp.minute % 5, second=0, microsecond=0)
        buckets[(record["symbol"], bucket)].append(record["bar"])
    output: dict[str, list[dict]] = defaultdict(list)
    for (symbol, bucket), bars in sorted(buckets.items()):
        bars.sort(key=lambda bar: bar["t"])
        volumes = [number(bar, "v") or 0.0 for bar in bars]
        total_volume = sum(volumes)
        output[symbol].append({
            "time": bucket.isoformat(),
            "open": number(bars[0], "o"),
            "high": max(number(bar, "h") for bar in bars),
            "low": min(number(bar, "l") for bar in bars),
            "close": number(bars[-1], "c"),
            "volume": total_volume,
        })
    return dict(output)


def aligned(value: float | None, side: str, threshold: float = 0.0) -> bool:
    if value is None:
        return False
    direction = 1.0 if side == "LONG" else -1.0
    return value * direction >= threshold


def candidate_passes(candidate: dict, feature: dict, side: str) -> bool:
    if candidate.get("gap") is not None and not aligned(feature.get("overnight_gap"), side, float(candidate["gap"])):
        return False
    if candidate.get("minimum_relative_volume") is not None:
        value = feature.get("premarket_relative_volume")
        if value is None or value < float(candidate["minimum_relative_volume"]):
            return False
    if candidate.get("aligned_vwap") and not aligned(feature.get("vwap_position"), side):
        return False
    if candidate.get("aligned_late_30m") and not aligned(feature.get("late_30m_return"), side):
        return False
    if candidate.get("max_spread_bps") is not None:
        spread = feature.get("spread_bps")
        if spread is None or spread > float(candidate["max_spread_bps"]):
            return False
    if candidate.get("market_confirmation") and not aligned(feature.get("market_gap"), side):
        return False
    if candidate.get("sector_confirmation") and not aligned(feature.get("sector_gap"), side):
        return False
    return True


def outcome_row(session_date: str, symbol: str, candidate: dict, intent: dict, feature: dict, bars: list[dict]) -> dict | None:
    outcome = simulate_next_open(bars, intent, target_rr=None)
    if outcome.get("fill_state") != "filled_closed" or not outcome.get("fill_price"):
        return None
    fill = float(outcome["fill_price"])
    risk_frac = abs(fill - float(intent["stop"])) / fill
    if risk_frac <= 0:
        return None
    row = {
        "evaluation_version": EVALUATION_VERSION,
        "session_date": session_date,
        "candidate": candidate["name"],
        "promotion_eligible": bool(candidate.get("promotion_eligible")),
        "symbol": symbol,
        "side": intent["side"],
        "signal_time": intent["signal_time"],
        "entry": fill,
        "stop": float(intent["stop"]),
        "risk_frac": risk_frac,
        "exit_reason": outcome["exit_reason"],
        "outcome_r": float(outcome["outcome_r"]),
        "features": feature,
    }
    for bps in COST_BPS:
        row[f"net_r_{bps}bp"] = row["outcome_r"] - (bps / 10000.0) / risk_frac
    return row


def observed_holiday(day: date) -> date:
    if day.weekday() == 5:
        return day - timedelta(days=1)
    if day.weekday() == 6:
        return day + timedelta(days=1)
    return day


def nth_weekday(year: int, month: int, weekday: int, occurrence: int) -> date:
    day = date(year, month, 1)
    day += timedelta(days=(weekday - day.weekday()) % 7)
    return day + timedelta(weeks=occurrence - 1)


def last_weekday(year: int, month: int, weekday: int) -> date:
    next_month = date(year + (month == 12), month % 12 + 1, 1)
    day = next_month - timedelta(days=1)
    return day - timedelta(days=(day.weekday() - weekday) % 7)


def easter_sunday(year: int) -> date:
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = (h + l - 7 * m + 114) % 31 + 1
    return date(year, month, day)


def nyse_holidays(year: int) -> set[date]:
    return {
        observed_holiday(date(year, 1, 1)),
        nth_weekday(year, 1, 0, 3),
        nth_weekday(year, 2, 0, 3),
        easter_sunday(year) - timedelta(days=2),
        last_weekday(year, 5, 0),
        observed_holiday(date(year, 6, 19)),
        observed_holiday(date(year, 7, 4)),
        nth_weekday(year, 9, 0, 1),
        nth_weekday(year, 11, 3, 4),
        observed_holiday(date(year, 12, 25)),
    }


def is_market_day(day: date) -> bool:
    return day.weekday() < 5 and day not in nyse_holidays(day.year)


def add_market_days(start: date, count: int) -> date:
    day = start
    remaining = count
    while remaining > 0:
        day += timedelta(days=1)
        if is_market_day(day):
            remaining -= 1
    return day


def daily_block_bootstrap_low(rows: list[dict], field: str, samples: int = 2000) -> float | None:
    by_day: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        by_day[row["session_date"]].append(float(row[field]))
    days = sorted(by_day)
    if len(days) < 2:
        return None
    rng = random.Random(20260713)
    estimates = []
    for _ in range(samples):
        selected = [days[rng.randrange(len(days))] for _ in days]
        values = [value for day in selected for value in by_day[day]]
        estimates.append(statistics.mean(values))
    estimates.sort()
    return estimates[max(0, math.floor(0.025 * len(estimates)) - 1)]


def candidate_gate(rows: list[dict], quality: dict, sealed_days: int) -> dict:
    ordered = sorted(rows, key=lambda row: (row["session_date"], row["symbol"], row["signal_time"]))
    midpoint = len(ordered) // 2
    first = ordered[:midpoint]
    second = ordered[midpoint:]
    checks = {
        "minimum_sealed_market_days": sealed_days >= FORWARD_GATE["minimum_sealed_market_days"],
        "minimum_eligible_observations": len(ordered) >= FORWARD_GATE["minimum_eligible_observations"],
        "first_half_positive_at_6bp": bool(first) and statistics.mean(row["net_r_6bp"] for row in first) > 0,
        "second_half_positive_at_6bp": bool(second) and statistics.mean(row["net_r_6bp"] for row in second) > 0,
        "bootstrap_lower_95pct_positive_at_6bp": (daily_block_bootstrap_low(ordered, "net_r_6bp") or float("-inf")) > 0,
        "mean_nonnegative_at_12bp": bool(ordered) and statistics.mean(row["net_r_12bp"] for row in ordered) >= 0,
        "quote_coverage": quality["quote_coverage"] >= FORWARD_GATE["minimum_quote_coverage"],
        "required_feature_missingness": all(value <= FORWARD_GATE["maximum_required_field_missing_rate"] for value in quality["missing_rate_by_feature"].values()),
        "disjoint_future_sample": True,
    }
    enough = checks["minimum_sealed_market_days"] and checks["minimum_eligible_observations"]
    return {
        "decision": "PASS" if enough and all(checks.values()) else ("FAIL" if enough else "NOT_ENOUGH_DATA"),
        "checks": checks,
    }


def build_status(session_quality: list[dict], observations: list[dict], as_of: date) -> dict:
    sealed_days = len(session_quality)
    remaining_days = max(0, FORWARD_GATE["minimum_sealed_market_days"] - sealed_days)
    day_gate_date = add_market_days(as_of, remaining_days)
    total_symbols = sum(item["stock_symbols"] for item in session_quality)
    valid_quotes = sum(item["valid_quotes"] for item in session_quality)
    missing_counts = {name: sum(item["missing_counts"].get(name, 0) for item in session_quality) for name in REQUIRED_FEATURES}
    quality = {
        "quote_coverage": valid_quotes / total_symbols if total_symbols else 0.0,
        "missing_rate_by_feature": {name: missing_counts[name] / total_symbols if total_symbols else 1.0 for name in REQUIRED_FEATURES},
        "symbols_evaluated": total_symbols,
    }
    by_candidate: dict[str, list[dict]] = defaultdict(list)
    for row in observations:
        by_candidate[row["candidate"]].append(row)
    candidates = {}
    for candidate in CANDIDATES:
        rows = by_candidate[candidate["name"]]
        count = len(rows)
        days = len({row["session_date"] for row in rows})
        remaining_observations = max(0, FORWARD_GATE["minimum_eligible_observations"] - count)
        pace = count / sealed_days if sealed_days >= 20 and count > 0 else None
        observation_days_needed = math.ceil(remaining_observations / pace) if pace else None
        observation_gate_date = add_market_days(as_of, observation_days_needed) if observation_days_needed is not None else None
        full_gate_date = max(day_gate_date, observation_gate_date) if observation_gate_date else None
        candidates[candidate["name"]] = {
            "promotion_eligible": bool(candidate.get("promotion_eligible")),
            "observations": count,
            "observation_days": days,
            "observations_remaining": remaining_observations,
            "observations_per_sealed_day": pace,
            "estimated_observation_gate_date": observation_gate_date.isoformat() if observation_gate_date else None,
            "estimated_full_gate_date": full_gate_date.isoformat() if full_gate_date else None,
            "mean_net_r_6bp": statistics.mean(row["net_r_6bp"] for row in rows) if rows else None,
            "mean_net_r_12bp": statistics.mean(row["net_r_12bp"] for row in rows) if rows else None,
            "gate": candidate_gate(rows, quality, sealed_days) if candidate.get("promotion_eligible") else {"decision": "CONTEXT_ONLY"},
        }
    if sealed_days < 20:
        stage = "COLLECTING_FOR_20_DAY_QUALITY_REVIEW"
    elif sealed_days < 60:
        stage = "COLLECTING_FOR_60_DAY_STABILITY_REVIEW"
    elif sealed_days < FORWARD_GATE["minimum_sealed_market_days"]:
        stage = "COLLECTING_FOR_120_DAY_GATE"
    elif max((item["observations"] for item in candidates.values() if item["promotion_eligible"]), default=0) < FORWARD_GATE["minimum_eligible_observations"]:
        stage = "DAY_GATE_MET_WAITING_FOR_OBSERVATIONS"
    else:
        stage = "PROMOTION_GATE_EVALUATION_AVAILABLE"
    projected_dates = [item["estimated_full_gate_date"] for item in candidates.values() if item["promotion_eligible"] and item["estimated_full_gate_date"]]
    earliest_full_gate_date = min(projected_dates) if projected_dates else None
    return {
        "evaluation_version": EVALUATION_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "stage": stage,
        "sealed_scored_market_days": sealed_days,
        "minimum_sealed_market_days": FORWARD_GATE["minimum_sealed_market_days"],
        "market_days_remaining": remaining_days,
        "estimated_day_gate_date": day_gate_date.isoformat(),
        "estimate_basis": "NYSE weekday and standard full-day holiday calendar; unscheduled closures can move the date",
        "observation_gate": FORWARD_GATE["minimum_eligible_observations"],
        "earliest_candidate_full_gate_date": earliest_full_gate_date,
        "observation_projection_available": sealed_days >= 20,
        "next_reviews": {"quality_review_days": 20, "stability_review_days": 60, "promotion_review_days": 120},
        "data_quality": quality,
        "candidates": candidates,
        "message": f"{sealed_days}/{FORWARD_GATE['minimum_sealed_market_days']} sealed scored market days; {remaining_days} remaining; day gate estimate {day_gate_date.isoformat()}. " + (f"Earliest candidate full-gate estimate {earliest_full_gate_date}." if earliest_full_gate_date else "Observation-date projection starts after 20 days."),
    }


def evaluate_archive(archive: Path = ARCHIVE, output_dir: Path = EVALUATION_DIR, write: bool = True, sector_map_path: Path = SECTOR_MAP_PATH) -> tuple[dict, list[dict]]:
    sector_map = json.loads(sector_map_path.read_text())
    volume_history: dict[str, list[float]] = defaultdict(list)
    observations = []
    session_quality = []
    source_manifests = []
    for directory in sorted(path for path in archive.iterdir() if path.is_dir() and path.name[:4].isdigit()):
        capture_manifest_path = directory / "iex_capture_0915.manifest.json"
        sip_manifest_path = directory / "sip_session_backfill_1600.manifest.json"
        if not (capture_manifest_path.exists() and sip_manifest_path.exists()):
            continue
        capture_manifest = verify_manifest(capture_manifest_path)
        sip_manifest = verify_manifest(sip_manifest_path)
        if capture_manifest.get("protocol_version") != "premarket-forward-2026-07-13.4":
            continue
        if sip_manifest.get("protocol_version") != "premarket-forward-2026-07-13.4":
            continue
        if capture_manifest.get("session_date") != directory.name or sip_manifest.get("session_date") != directory.name:
            raise RuntimeError(f"session-date mismatch in {directory}")
        if capture_manifest.get("universe_sha256") != sip_manifest.get("universe_sha256"):
            raise RuntimeError(f"universe mismatch in {directory}")
        if not capture_manifest.get("decision_available_preopen") or not sip_manifest.get("outcome_available_postclose"):
            continue
        capture_records = read_jsonl(directory / capture_manifest["data_file"])
        sip_records = read_jsonl(directory / sip_manifest["data_file"])
        raw = raw_capture_features(capture_records, capture_manifest)
        features = enrich_features(raw, sector_map, volume_history)
        regular = aggregate_regular_five_minute(sip_records)
        stock_symbols = [symbol for symbol in capture_manifest["symbols"] if symbol not in REFERENCE_SYMBOLS]
        missing_counts = {name: 0 for name in REQUIRED_FEATURES}
        valid_quotes = 0
        for symbol in stock_symbols:
            feature = features.get(symbol, {})
            valid_quotes += int(bool(feature.get("quote_valid")))
            for name in REQUIRED_FEATURES:
                missing_counts[name] += int(feature.get(name) is None)
            bars = regular.get(symbol, [])
            intents = detect_orb(bars, {"use_sector_gate": False}) if bars else []
            if intents:
                intent = intents[0]
                for candidate in CANDIDATES:
                    if candidate_passes(candidate, feature, intent["side"]):
                        row = outcome_row(directory.name, symbol, candidate, intent, feature, bars)
                        if row:
                            observations.append(row)
        for symbol, feature in raw.items():
            volume = feature.get("premarket_volume")
            if volume is not None:
                volume_history[symbol].append(float(volume))
        session_quality.append({
            "session_date": directory.name,
            "stock_symbols": len(stock_symbols),
            "valid_quotes": valid_quotes,
            "missing_counts": missing_counts,
            "capture_records": capture_manifest["record_count"],
            "sip_records": sip_manifest["record_count"],
        })
        source_manifests.extend([str(capture_manifest_path.relative_to(archive)), str(sip_manifest_path.relative_to(archive))])

    status = build_status(session_quality, observations, datetime.now(ET).date())
    status["source_manifests"] = source_manifests
    status["evaluation_protocol_sha256"] = sha256_bytes(EVALUATION_PROTOCOL.read_bytes()) if EVALUATION_PROTOCOL.exists() else None
    status["evaluator_sha256"] = sha256_bytes(Path(__file__).read_bytes())
    if write:
        rows_payload = "".join(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n" for row in observations)
        atomic_write(output_dir / "observations.jsonl", rows_payload)
        status["observations_sha256"] = sha256_bytes(rows_payload.encode())
        atomic_write(output_dir / "status.json", json.dumps(status, indent=2, sort_keys=True) + "\n")
        atomic_write(output_dir / "STATUS.txt", status["message"] + "\n")
    return status, observations


def verify_outputs(output_dir: Path = EVALUATION_DIR) -> dict:
    status = json.loads((output_dir / "status.json").read_text())
    payload = (output_dir / "observations.jsonl").read_bytes()
    if sha256_bytes(payload) != status["observations_sha256"]:
        raise RuntimeError("derived observations hash mismatch")
    return {"status": "verified", "days": status["sealed_scored_market_days"], "observations": len(payload.splitlines())}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["update", "status", "verify"], default="update")
    parser.add_argument("--archive", type=Path, default=ARCHIVE)
    parser.add_argument("--output-dir", type=Path, default=EVALUATION_DIR)
    args = parser.parse_args()
    if args.mode == "update":
        result, _ = evaluate_archive(args.archive, args.output_dir, write=True)
    elif args.mode == "status":
        result = json.loads((args.output_dir / "status.json").read_text())
    else:
        result = verify_outputs(args.output_dir)
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
