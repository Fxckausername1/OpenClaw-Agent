#!/usr/bin/env python3
"""Offline evidence gate for strategy promotion decisions."""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data/research"
OUT = DATA / "promotion_gate/latest.json"


def evaluate(strategy: str, analysis: dict, parity: dict) -> dict:
    metrics = analysis["strategies"][strategy]["all"]
    reasons = []
    contract_failures = int(parity.get("live_backtest_contracts", {}).get("failed", 0))
    if contract_failures:
        reasons.append(f"{contract_failures} unresolved live/backtest contract mismatches")
    if metrics.get("unique_days", 0) < 250:
        reasons.append(f"only {metrics.get('unique_days', 0)} unique historical days; require >=250")
    if metrics.get("verdict") != "positive_confirmed":
        reasons.append(f"daily-block-bootstrap verdict is {metrics.get('verdict')}")
    if (metrics.get("net_6bp_avg_r") or 0) <= 0:
        reasons.append(f"6bp intent expectancy is {metrics.get('net_6bp_avg_r'):+.3f}R")
    ci = metrics.get("daily_block_bootstrap_95pct_mean_r", {})
    if ci.get("low") is None or ci.get("low") <= 0:
        reasons.append(f"95% lower confidence bound is {ci.get('low')}")
    return {
        "strategy": strategy,
        "decision": "BLOCKED" if reasons else "ELIGIBLE_FOR_FORWARD_PAPER_REVIEW",
        "reasons": reasons,
        "metrics": {
            "unique_days": metrics.get("unique_days"),
            "net_6bp_avg_r": metrics.get("net_6bp_avg_r"),
            "bootstrap_95pct": ci,
            "fill_rate": metrics.get("fill_rate"),
        },
    }


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    temp.write_text(text)
    os.chmod(temp, 0o600)
    os.replace(temp, path)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--strict", action="store_true")
    args = ap.parse_args()
    analysis = json.loads((DATA / "canonical_replay/latest.analysis.json").read_text())
    parity = json.loads((DATA / "parity_reports/latest.json").read_text())
    report = {
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "policy": {
            "minimum_unique_days": 250,
            "cost_basis_bps": 6,
            "required_bootstrap_verdict": "positive_confirmed",
            "required_contract_failures": 0,
        },
        "decisions": {strategy: evaluate(strategy, analysis, parity) for strategy in ("MR", "ORB")},
    }
    atomic_write(OUT, json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps(report, indent=2, sort_keys=True))
    if args.strict and any(v["decision"] == "BLOCKED" for v in report["decisions"].values()):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
