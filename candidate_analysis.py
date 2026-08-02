#!/usr/bin/env python3
"""Walk-forward, regime, cost, and bootstrap analysis for candidate replay results."""

from __future__ import annotations

import hashlib
import json
import math
import random
import statistics
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent
SOURCE = ROOT / "data/research/candidate_replay/latest.trades.jsonl"
SUMMARY = ROOT / "data/research/candidate_replay/latest.summary.json"
OUTPUT = ROOT / "data/research/candidate_replay/latest.analysis.json"
COST_BPS = (6, 12, 20, 30)
BOOTSTRAPS = 2000
BASE_SEED = 20260713
FOLDS = (
    {"name": "train_2024_test_2025", "train_years": ("2024",), "test_year": "2025"},
    {"name": "train_2024_2025_test_2026", "train_years": ("2024", "2025"), "test_year": "2026"},
)


def load_rows(path: Path = SOURCE) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return ordered[lower]
    return ordered[lower] * (upper - position) + ordered[upper] * (position - lower)


def stable_seed(label: str) -> int:
    digest = hashlib.sha256(label.encode()).hexdigest()
    return BASE_SEED + int(digest[:8], 16)


def daily_block_ci(rows: list[dict], key: str = "net_r_6bp", label: str = "all", bootstraps: int = BOOTSTRAPS) -> dict:
    by_day = defaultdict(list)
    for row in rows:
        by_day[row["date"]].append(float(row[key]))
    days = sorted(by_day)
    if not days:
        return {"low": None, "high": None, "bootstraps": bootstraps, "seed": stable_seed(label)}
    rng = random.Random(stable_seed(label))
    means = []
    for _ in range(bootstraps):
        total = 0.0
        count = 0
        for _ in days:
            sample = by_day[rng.choice(days)]
            total += sum(sample)
            count += len(sample)
        means.append(total / count if count else 0.0)
    return {
        "low": percentile(means, 0.025),
        "high": percentile(means, 0.975),
        "bootstraps": bootstraps,
        "seed": stable_seed(label),
    }


def basic_stats(rows: list[dict]) -> dict:
    if not rows:
        return {"n": 0, "unique_days": 0}
    result = {
        "n": len(rows),
        "unique_days": len({row["date"] for row in rows}),
        "filled": sum(row["fill_state"] == "filled_closed" for row in rows),
        "fill_rate": sum(row["fill_state"] == "filled_closed" for row in rows) / len(rows),
        "gross_avg_r": statistics.mean(float(row["outcome_r"]) for row in rows),
    }
    for bps in COST_BPS:
        values = [float(row[f"net_r_{bps}bp"]) for row in rows]
        positive = sum(value for value in values if value > 0)
        negative = -sum(value for value in values if value <= 0)
        result[f"net_{bps}bp_avg_r"] = statistics.mean(values)
        result[f"net_{bps}bp_total_r"] = sum(values)
        result[f"net_{bps}bp_win_rate"] = sum(value > 0 for value in values) / len(values)
        result[f"net_{bps}bp_profit_factor"] = positive / negative if negative else None
    return result


def stats(rows: list[dict], label: str) -> dict:
    result = basic_stats(rows)
    ci = daily_block_ci(rows, label=label)
    result["daily_block_bootstrap_95pct_mean_r_6bp"] = ci
    if ci["low"] is not None and ci["low"] > 0:
        result["verdict_6bp"] = "positive_confirmed"
    elif ci["high"] is not None and ci["high"] < 0:
        result["verdict_6bp"] = "negative_confirmed"
    else:
        result["verdict_6bp"] = "inconclusive"
    return result


def year_filter(rows: list[dict], years: tuple[str, ...]) -> list[dict]:
    return [row for row in rows if row["date"][:4] in years]


def walk_forward(rows: list[dict], strategy: str, names: list[str]) -> tuple[list[dict], list[dict]]:
    folds = []
    combined_oos = []
    for fold in FOLDS:
        ranked = []
        for name in names:
            candidate_rows = [row for row in rows if row["candidate"] == name]
            train = year_filter(candidate_rows, fold["train_years"])
            train_stats = stats(train, f"{strategy}:{fold['name']}:{name}:train")
            if train_stats["n"] < 200 or train_stats["unique_days"] < 60:
                continue
            ci = train_stats["daily_block_bootstrap_95pct_mean_r_6bp"]
            ranked.append((ci["low"], train_stats["net_6bp_avg_r"], name, train_stats))
        if not ranked:
            folds.append({**fold, "selected": None, "reason": "no candidate met training coverage"})
            continue
        _, _, selected, train_stats = max(ranked, key=lambda item: (item[0], item[1], item[2]))
        test = [
            row for row in rows
            if row["candidate"] == selected and row["date"].startswith(fold["test_year"])
        ]
        test_stats = stats(test, f"{strategy}:{fold['name']}:{selected}:test")
        folds.append({
            **fold,
            "selected": selected,
            "selection_rule": "maximum training daily-block-bootstrap lower 95% bound at 6bp; minimum 200 intents and 60 days",
            "train": train_stats,
            "test": test_stats,
        })
        combined_oos.extend(test)
    return folds, combined_oos


def analyze_strategy(rows: list[dict], strategy: str, names: list[str]) -> dict:
    candidate_reports = {}
    for name in names:
        group = [row for row in rows if row["candidate"] == name]
        candidate_reports[name] = {
            "all": stats(group, f"{strategy}:{name}:all"),
            "by_year": {
                year: basic_stats([row for row in group if row["date"].startswith(year)])
                for year in sorted({row["date"][:4] for row in group})
            },
            "by_side": {
                side: basic_stats([row for row in group if row["side"] == side])
                for side in ("LONG", "SHORT")
            },
            "by_regime": {
                regime: basic_stats([row for row in group if row["regime"] == regime])
                for regime in ("bull", "bear", "neutral", "unknown")
            },
        }
    folds, combined_oos = walk_forward(rows, strategy, names)
    oos_stats = stats(combined_oos, f"{strategy}:combined_oos")
    fold_tests = [fold.get("test") for fold in folds if fold.get("test")]
    coverage_ok = oos_stats.get("unique_days", 0) >= 150
    fold_means_positive = len(fold_tests) == len(FOLDS) and all(test.get("net_6bp_avg_r", 0) > 0 for test in fold_tests)
    ci = oos_stats["daily_block_bootstrap_95pct_mean_r_6bp"]
    ci_positive = ci["low"] is not None and ci["low"] > 0
    historical_pass = coverage_ok and fold_means_positive and ci_positive
    gate = {
        "historical_walk_forward": "PASS" if historical_pass else "FAIL",
        "live_promotion": "BLOCKED_FORWARD_SAMPLE_REQUIRED" if historical_pass else "REJECTED",
        "checks": {
            "combined_oos_minimum_150_days": coverage_ok,
            "both_fold_test_means_positive_at_6bp": fold_means_positive,
            "combined_oos_bootstrap_lower_bound_positive_at_6bp": ci_positive,
            "discovery_contamination_cleared": False,
        },
        "reason": (
            "historical evidence passed, but the family was designed after baseline inspection and requires future forward data"
            if historical_pass
            else "historical walk-forward evidence did not meet the pre-registered promotion threshold"
        ),
    }
    return {
        "candidates": candidate_reports,
        "walk_forward_folds": folds,
        "combined_selected_oos": oos_stats,
        "combined_selected_oos_by_regime": {
            regime: basic_stats([row for row in combined_oos if row["regime"] == regime])
            for regime in ("bull", "bear", "neutral", "unknown")
        },
        "gate": gate,
    }


def main() -> None:
    rows = load_rows()
    replay_summary = json.loads(SUMMARY.read_text())
    report = {
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source_run_id": replay_summary["run_id"],
        "method": {
            "selection": "anchored historical walk-forward candidate selection using training-only bootstrap lower bound",
            "folds": list(FOLDS),
            "costs_bps": list(COST_BPS),
            "regime": "SPY prior close versus prior 20-session SMA and prior 5-session return; same-day data excluded",
            "same_bar": "stop-first",
            "discovery_warning": "candidate family was designed after full-baseline review; future forward sample is mandatory",
        },
        "strategies": {},
    }
    for strategy in ("MR", "ORB"):
        names = [item["name"] for item in replay_summary["candidates"] if item["strategy"] == strategy]
        report["strategies"][strategy] = analyze_strategy(rows, strategy, names)
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
