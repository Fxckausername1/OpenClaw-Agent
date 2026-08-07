#!/usr/bin/env python3
"""Apply anchored walk-forward, regime, cost, and promotion gates to signal-quality candidates."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from candidate_analysis import analyze_strategy, stats


ROOT = Path(__file__).resolve().parent
SOURCE = ROOT / "data/research/signal_quality/latest.trades.jsonl"
SUMMARY = ROOT / "data/research/signal_quality/latest.summary.json"
OUTPUT = ROOT / "data/research/signal_quality/latest.analysis.json"


def main() -> None:
    rows = [json.loads(line) for line in SOURCE.read_text().splitlines() if line.strip()]
    summary = json.loads(SUMMARY.read_text())
    report = {
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source_run_id": summary["run_id"],
        "method": {
            "execution_reference": "next five-minute bar open, end-of-day exit, stop-first",
            "selection": "primary candidates only; maximum training 6bp daily-block-bootstrap lower bound",
            "folds": ["2024 train -> 2025 test", "2024-2025 train -> 2026 test"],
            "costs_bps": [6, 12, 20, 30],
            "robustness": "neighbor thresholds reported but excluded from walk-forward selection",
            "discovery_warning": "family was designed after baseline inspection and cannot clear live promotion without future data",
        },
        "feature_coverage": summary["feature_coverage"],
        "strategies": {},
    }
    for strategy in ("MR", "ORB"):
        definitions = [item for item in summary["candidates"] if item["strategy"] == strategy]
        primary = [item["name"] for item in definitions if item["selection_eligible"]]
        robustness = [item["name"] for item in definitions if not item["selection_eligible"]]
        section = analyze_strategy(rows, strategy, primary)
        section["robustness_only"] = {
            name: stats([row for row in rows if row["candidate"] == name], f"{strategy}:{name}:robustness")
            for name in robustness
        }
        report["strategies"][strategy] = section
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
