#!/usr/bin/env python3
"""Evaluate pre-registered execution candidates over frozen canonical MR/ORB intents."""

from __future__ import annotations

import argparse
import json
import math
import os
from collections import defaultdict
from datetime import datetime, time, timezone
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
SOURCE = DATA / "research/canonical_replay/latest.trades.jsonl"
CACHE = DATA / "wf_cache"
SPY_DAILY = DATA / "wf_daily_cache/SPY.parquet"
OUT_DIR = DATA / "research/candidate_replay"
COST_BPS = (6, 12, 20, 30)
CONTRACT_VERSION = "candidate-2026-07-13.1"


CANDIDATES = (
    # Frozen references.
    {"name": "mr_boundary_baseline", "strategy": "MR", "entry_mode": "boundary_limit"},
    {"name": "orb_boundary_baseline", "strategy": "ORB", "entry_mode": "boundary_limit"},
    # One-variable MR hypotheses, followed by one pre-registered combination.
    {"name": "mr_next_open_eod", "strategy": "MR", "entry_mode": "next_open"},
    {"name": "mr_next_open_1r", "strategy": "MR", "entry_mode": "next_open", "target_rr": 1.0},
    {"name": "mr_next_open_1p5r", "strategy": "MR", "entry_mode": "next_open", "target_rr": 1.5},
    {
        "name": "mr_next_open_1r_gap025",
        "strategy": "MR",
        "entry_mode": "next_open",
        "target_rr": 1.0,
        "max_extension_r": 0.25,
    },
    {
        "name": "mr_next_open_1r_risk005",
        "strategy": "MR",
        "entry_mode": "next_open",
        "target_rr": 1.0,
        "min_boundary_risk_frac": 0.005,
    },
    {
        "name": "mr_next_open_1r_neutral",
        "strategy": "MR",
        "entry_mode": "next_open",
        "target_rr": 1.0,
        "regime_filter": "neutral",
    },
    {
        "name": "mr_next_open_1r_risk005_neutral",
        "strategy": "MR",
        "entry_mode": "next_open",
        "target_rr": 1.0,
        "min_boundary_risk_frac": 0.005,
        "regime_filter": "neutral",
    },
    # One-variable ORB hypotheses, followed by one pre-registered combination.
    {"name": "orb_next_open_eod", "strategy": "ORB", "entry_mode": "next_open"},
    {"name": "orb_next_open_1r", "strategy": "ORB", "entry_mode": "next_open", "target_rr": 1.0},
    {"name": "orb_next_open_1p5r", "strategy": "ORB", "entry_mode": "next_open", "target_rr": 1.5},
    {
        "name": "orb_next_open_1r_gap025",
        "strategy": "ORB",
        "entry_mode": "next_open",
        "target_rr": 1.0,
        "max_extension_r": 0.25,
    },
    {
        "name": "orb_next_open_1r_range0035",
        "strategy": "ORB",
        "entry_mode": "next_open",
        "target_rr": 1.0,
        "min_boundary_risk_frac": 0.0035,
    },
    {
        "name": "orb_next_open_1r_early",
        "strategy": "ORB",
        "entry_mode": "next_open",
        "target_rr": 1.0,
        "signal_time_end": "10:30",
    },
    {
        "name": "orb_next_open_1r_aligned",
        "strategy": "ORB",
        "entry_mode": "next_open",
        "target_rr": 1.0,
        "regime_filter": "aligned",
    },
    {
        "name": "orb_next_open_1r_range0035_early_aligned",
        "strategy": "ORB",
        "entry_mode": "next_open",
        "target_rr": 1.0,
        "min_boundary_risk_frac": 0.0035,
        "signal_time_end": "10:30",
        "regime_filter": "aligned",
    },
)


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    temp.write_text(text)
    os.chmod(temp, 0o600)
    os.replace(temp, path)


def clean(value):
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    if hasattr(value, "item"):
        value = value.item()
    return value


def bars_from_frame(frame: pd.DataFrame) -> list[dict]:
    bars = []
    for timestamp, row in frame.iterrows():
        bar = {str(key): clean(value) for key, value in row.to_dict().items()}
        bar["time"] = pd.Timestamp(timestamp).to_pydatetime().replace(tzinfo=None)
        bars.append(bar)
    return bars


def number(bar: dict, *names: str) -> float | None:
    for name in names:
        if bar.get(name) is not None:
            return float(bar[name])
    return None


def bar_time(bar: dict) -> datetime:
    raw = bar.get("time", bar.get("timestamp", bar.get("ts")))
    return raw if isinstance(raw, datetime) else datetime.fromisoformat(str(raw))


def clock(value: str) -> time:
    hour, minute = value.split(":", 1)
    return time(int(hour), int(minute))


def side_r(side: str, entry: float, stop: float, exit_price: float) -> float:
    direction = 1.0 if side == "LONG" else -1.0
    return (exit_price - entry) * direction / abs(entry - stop)


def build_regimes(path: Path = SPY_DAILY) -> dict[str, str]:
    """Use only closes strictly before each date; no same-day information leaks in."""
    frame = pd.read_parquet(path).sort_index()
    close_column = "Close" if "Close" in frame.columns else "close"
    closes = [(str(pd.Timestamp(index).date()), float(value)) for index, value in frame[close_column].items()]
    regimes = {}
    for index, (day, _) in enumerate(closes):
        history = [value for _, value in closes[:index]]
        if len(history) < 20:
            regimes[day] = "unknown"
            continue
        prior = history[-1]
        sma20 = sum(history[-20:]) / 20.0
        return5 = prior / history[-6] - 1.0 if len(history) >= 6 else 0.0
        distance = prior / sma20 - 1.0
        if distance >= 0.005 and return5 > 0:
            regimes[day] = "bull"
        elif distance <= -0.005 and return5 < 0:
            regimes[day] = "bear"
        else:
            regimes[day] = "neutral"
    return regimes


def static_filter(row: dict, candidate: dict, regime: str) -> bool:
    boundary_risk = float(row["risk_frac"])
    if boundary_risk < float(candidate.get("min_boundary_risk_frac", 0.0)):
        return False
    signal_clock = datetime.fromisoformat(row["signal_time"]).time()
    if candidate.get("signal_time_end") and signal_clock > clock(candidate["signal_time_end"]):
        return False
    regime_filter = candidate.get("regime_filter")
    if regime_filter == "neutral" and regime != "neutral":
        return False
    if regime_filter == "aligned":
        if not ((row["side"] == "LONG" and regime == "bull") or (row["side"] == "SHORT" and regime == "bear")):
            return False
    return True


def simulate_next_open(bars: list[dict], row: dict, target_rr: float | None) -> dict:
    future = bars[int(row["signal_index"]) + 1 :]
    if not future:
        return {"fill_state": "never_filled", "outcome_r": 0.0, "exit_reason": "no_next_bar"}
    first = future[0]
    entry = number(first, "open", "Open")
    stop = float(row["stop"])
    side = row["side"]
    if entry is None or (side == "LONG" and entry <= stop) or (side == "SHORT" and entry >= stop):
        return {"fill_state": "never_filled", "outcome_r": 0.0, "exit_reason": "invalid_gap"}
    risk = abs(entry - stop)
    target = None
    if target_rr is not None:
        target = entry + risk * target_rr if side == "LONG" else entry - risk * target_rr

    for bar in future:
        high = number(bar, "high", "High")
        low = number(bar, "low", "Low")
        if high is None or low is None:
            continue
        stop_hit = low <= stop if side == "LONG" else high >= stop
        target_hit = target is not None and (high >= target if side == "LONG" else low <= target)
        if stop_hit:
            return {
                "fill_state": "filled_closed",
                "fill_price": entry,
                "fill_time": bar_time(first).isoformat(),
                "exit_price": stop,
                "exit_time": bar_time(bar).isoformat(),
                "exit_reason": "stop",
                "outcome_r": -1.0,
            }
        if target_hit:
            return {
                "fill_state": "filled_closed",
                "fill_price": entry,
                "fill_time": bar_time(first).isoformat(),
                "exit_price": target,
                "exit_time": bar_time(bar).isoformat(),
                "exit_reason": "target",
                "outcome_r": float(target_rr),
            }
    close = number(future[-1], "close", "Close")
    return {
        "fill_state": "filled_closed",
        "fill_price": entry,
        "fill_time": bar_time(first).isoformat(),
        "exit_price": close,
        "exit_time": bar_time(future[-1]).isoformat(),
        "exit_reason": "eod",
        "outcome_r": side_r(side, entry, stop, close),
    }


def compact_row(source: dict, candidate: dict, regime: str, outcome: dict) -> dict:
    fill_price = outcome.get("fill_price")
    stop = float(source["stop"])
    risk_frac = abs(float(fill_price) - stop) / float(fill_price) if fill_price else float(source["risk_frac"])
    result = {
        "candidate": candidate["name"],
        "strategy": source["strategy"],
        "symbol": source["symbol"],
        "date": source["date"],
        "side": source["side"],
        "signal_time": source["signal_time"],
        "signal_index": source["signal_index"],
        "regime": regime,
        "entry_mode": candidate["entry_mode"],
        "fill_state": outcome["fill_state"],
        "exit_reason": outcome["exit_reason"],
        "outcome_r": float(outcome["outcome_r"]),
        "risk_frac": risk_frac,
    }
    if fill_price is not None:
        result["fill_price"] = float(fill_price)
        result["stop"] = stop
        result["fill_time"] = outcome.get("fill_time")
        result["exit_time"] = outcome.get("exit_time")
    for bps in COST_BPS:
        friction = (bps / 10000.0) / max(risk_frac, 1e-9) if outcome["fill_state"] == "filled_closed" else 0.0
        result[f"net_r_{bps}bp"] = float(outcome["outcome_r"]) - friction
    return result


def summarize(rows: list[dict]) -> dict:
    result = {"intents": len(rows), "days": len({row["date"] for row in rows})}
    result["filled"] = sum(row["fill_state"] == "filled_closed" for row in rows)
    result["fill_rate"] = result["filled"] / len(rows) if rows else None
    for bps in COST_BPS:
        values = [row[f"net_r_{bps}bp"] for row in rows]
        result[f"net_{bps}bp_avg_r"] = sum(values) / len(values) if values else None
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=SOURCE)
    parser.add_argument("--max-symbols", type=int)
    args = parser.parse_args()

    source_rows = [json.loads(line) for line in args.source.read_text().splitlines() if line.strip()]
    by_symbol = defaultdict(list)
    for row in source_rows:
        by_symbol[row["symbol"]].append(row)
    symbols = sorted(by_symbol)
    if args.max_symbols:
        symbols = symbols[: args.max_symbols]
    regimes = build_regimes()
    candidates_by_strategy = defaultdict(list)
    for candidate in CANDIDATES:
        candidates_by_strategy[candidate["strategy"]].append(candidate)

    output_rows = []
    errors = []
    exclusions = defaultdict(lambda: defaultdict(int))
    for symbol in symbols:
        try:
            frame = pd.read_parquet(CACHE / f"{symbol}.parquet").sort_index()
            days = {str(day): bars_from_frame(group) for day, group in frame.groupby(frame.index.date, sort=True)}
        except Exception as exc:
            errors.append({"symbol": symbol, "error": str(exc)})
            continue
        for source in by_symbol[symbol]:
            regime = regimes.get(source["date"], "unknown")
            for candidate in candidates_by_strategy[source["strategy"]]:
                if not static_filter(source, candidate, regime):
                    exclusions[candidate["name"]]["static_filter"] += 1
                    continue
                if candidate["entry_mode"] == "boundary_limit":
                    outcome = {
                        key: source.get(key)
                        for key in ("fill_state", "fill_price", "fill_time", "exit_price", "exit_time", "exit_reason", "outcome_r")
                    }
                else:
                    bars = days.get(source["date"], [])
                    outcome = simulate_next_open(bars, source, candidate.get("target_rr"))
                    if outcome.get("fill_price") is not None and candidate.get("max_extension_r") is not None:
                        boundary_entry = float(source["entry"])
                        boundary_risk = abs(boundary_entry - float(source["stop"]))
                        direction = 1.0 if source["side"] == "LONG" else -1.0
                        extension = (float(outcome["fill_price"]) - boundary_entry) * direction / max(boundary_risk, 1e-9)
                        if extension > float(candidate["max_extension_r"]):
                            exclusions[candidate["name"]]["extension_guard"] += 1
                            continue
                output_rows.append(compact_row(source, candidate, regime, outcome))

    created = datetime.now(timezone.utc)
    run_id = f"candidate_{created.strftime('%Y%m%dT%H%M%SZ')}"
    report = {
        "run_id": run_id,
        "created_at": created.isoformat(timespec="seconds"),
        "contract_version": CONTRACT_VERSION,
        "source": str(args.source),
        "hypothesis_count": len(CANDIDATES),
        "candidates": list(CANDIDATES),
        "results": {
            candidate["name"]: summarize([row for row in output_rows if row["candidate"] == candidate["name"]])
            for candidate in CANDIDATES
        },
        "exclusions": {name: dict(reasons) for name, reasons in exclusions.items()},
        "errors": errors,
        "limitations": [
            "candidate family was designed after reviewing the frozen baseline and is discovery-contaminated",
            "historical walk-forward results remain research evidence, not a substitute for future forward sampling",
            "five-minute OHLCV resolves same-bar stop/target ambiguity stop-first",
            "current cached/static universe is not point-in-time membership",
            "cost model is deterministic bps friction and does not reconstruct quote-level queue or impact",
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
