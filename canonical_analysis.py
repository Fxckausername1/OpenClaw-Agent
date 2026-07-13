#!/usr/bin/env python3
"""Analyze the latest canonical replay using trading-day block bootstrap."""

from __future__ import annotations

import json
import math
import random
import statistics
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent
SOURCE = ROOT / "data/research/canonical_replay/latest.trades.jsonl"
OUTPUT = ROOT / "data/research/canonical_replay/latest.analysis.json"
SEED = 20260713
BOOTSTRAPS = 5000


def load_rows():
    return [json.loads(line) for line in SOURCE.read_text().splitlines() if line.strip()]


def percentile(values, q):
    ordered = sorted(values)
    if not ordered:
        return None
    pos = (len(ordered) - 1) * q
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return ordered[lo]
    return ordered[lo] * (hi - pos) + ordered[hi] * (pos - lo)


def block_ci(rows, key="net_r_6bp"):
    by_day = defaultdict(list)
    for row in rows:
        by_day[row["date"]].append(float(row[key]))
    days = sorted(by_day)
    if not days:
        return {"low": None, "high": None, "bootstraps": BOOTSTRAPS, "seed": SEED}
    rng = random.Random(SEED)
    means = []
    for _ in range(BOOTSTRAPS):
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
        "bootstraps": BOOTSTRAPS,
        "seed": SEED,
    }


def stats(rows):
    if not rows:
        return {"n": 0}
    gross = [float(r["outcome_r"]) for r in rows]
    net = [float(r["net_r_6bp"]) for r in rows]
    positive = sum(v for v in net if v > 0)
    negative = -sum(v for v in net if v <= 0)
    top_count = max(1, math.ceil(len(net) * 0.05))
    top_profit = sum(sorted(net, reverse=True)[:top_count])
    ci = block_ci(rows)
    return {
        "n": len(rows),
        "unique_days": len({r["date"] for r in rows}),
        "filled": sum(r["fill_state"] == "filled_closed" for r in rows),
        "fill_rate": sum(r["fill_state"] == "filled_closed" for r in rows) / len(rows),
        "gross_avg_r": statistics.mean(gross),
        "net_6bp_avg_r": statistics.mean(net),
        "net_6bp_median_r": statistics.median(net),
        "net_6bp_total_r": sum(net),
        "net_6bp_win_rate": sum(v > 0 for v in net) / len(net),
        "net_6bp_profit_factor": positive / negative if negative else None,
        "top_5pct_share_of_positive_profit": top_profit / positive if positive else None,
        "daily_block_bootstrap_95pct_mean_r": ci,
        "verdict": "negative_confirmed" if ci["high"] is not None and ci["high"] < 0 else "positive_confirmed" if ci["low"] is not None and ci["low"] > 0 else "inconclusive",
    }


def main():
    rows = load_rows()
    report = {
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": str(SOURCE.relative_to(ROOT)),
        "method": "trade intents grouped and resampled by trading date",
        "strategies": {},
    }
    for strategy in ("MR", "ORB"):
        group = [r for r in rows if r["strategy"] == strategy]
        report["strategies"][strategy] = {
            "all": stats(group),
            "by_side": {side: stats([r for r in group if r["side"] == side]) for side in ("LONG", "SHORT")},
            "by_year": {
                year: stats([r for r in group if r["date"].startswith(year)])
                for year in sorted({r["date"][:4] for r in group})
            },
        }
    OUTPUT.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
