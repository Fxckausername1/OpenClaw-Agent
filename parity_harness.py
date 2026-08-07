#!/usr/bin/env python3
"""Offline parity audit for recorded triggers, orders, fills, and backtest contracts.

The harness reads files only and writes reports under data/research/parity_reports.
It never imports a scanner or executor, makes network requests, or submits orders.
"""

from __future__ import annotations

import argparse
import csv
import glob
import hashlib
import json
import os
import statistics
import subprocess
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
REPORT_DIR = DATA / "research" / "parity_reports"


def read_jsonl(path: Path):
    if not path.exists():
        return
    for number, line in enumerate(path.read_text().splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"invalid JSON in {path}:{number}: {exc}") from exc


def load_triggers() -> dict[str, dict]:
    out = {}
    patterns = (DATA / "mr_triggers_*.jsonl", DATA / "orb_triggers_*.jsonl")
    for pattern in patterns:
        for raw in sorted(glob.glob(str(pattern))):
            for row in read_jsonl(Path(raw)):
                if row.get("trade_id"):
                    out[row["trade_id"]] = row
    return out


def load_outcomes() -> dict[str, dict]:
    out = {}
    for name in ("paper_trades.csv", "orb_paper_trades.csv"):
        path = DATA / name
        if not path.exists():
            continue
        with path.open(newline="") as fh:
            for row in csv.DictReader(fh):
                if row.get("trade_id"):
                    out[row["trade_id"]] = row
    return out


def num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def money2(value: float) -> str:
    return f"{float(value):.2f}"


def expected_payload(trigger: dict) -> dict:
    side_long = str(trigger.get("side", "")).upper() == "LONG"
    entry = round(float(trigger["entry"]), 2)
    stop = round(float(trigger["stop"]), 2)
    stop = min(stop, entry - 0.01) if side_long else max(stop, entry + 0.01)
    strategy = str(trigger.get("strategy", ""))
    is_orb = strategy.upper() == "ORB" or str(trigger.get("trade_id", "")).startswith("ORB:")
    result = {
        "symbol": trigger.get("ticker"),
        "qty": str(int(trigger.get("shares") or 0)),
        "side": "buy" if side_long else "sell",
        "type": "limit",
        "limit_price": money2(entry),
        "time_in_force": "day",
        "order_class": "oto" if is_orb else "bracket",
        "stop_price": money2(stop),
        "take_profit": None if is_orb else money2(float(trigger["t1"])),
    }
    return result


def normalize_actual(payload: dict) -> dict:
    return {
        "symbol": payload.get("symbol"),
        "qty": str(payload.get("qty")) if payload.get("qty") is not None else None,
        "side": payload.get("side"),
        "type": payload.get("type"),
        "limit_price": money2(payload["limit_price"]) if payload.get("limit_price") is not None else None,
        "time_in_force": payload.get("time_in_force"),
        "order_class": payload.get("order_class"),
        "stop_price": money2((payload.get("stop_loss") or {}).get("stop_price"))
        if (payload.get("stop_loss") or {}).get("stop_price") is not None
        else None,
        "take_profit": money2((payload.get("take_profit") or {}).get("limit_price"))
        if (payload.get("take_profit") or {}).get("limit_price") is not None
        else None,
    }


def check_order_payloads(triggers: dict[str, dict]) -> dict:
    path = DATA / "alpaca_orders.jsonl"
    checked = 0
    exact = 0
    missing_trigger = []
    mismatches = []
    for order in read_jsonl(path):
        if not isinstance(order.get("alpaca"), str):
            continue
        tid = order.get("trade_id")
        trigger = triggers.get(tid)
        if trigger is None:
            missing_trigger.append(tid)
            continue
        checked += 1
        expected = expected_payload(trigger)
        actual = normalize_actual(order.get("payload") or {})
        diff = {key: {"expected": expected[key], "actual": actual[key]} for key in expected if expected[key] != actual[key]}
        if diff:
            mismatches.append({"trade_id": tid, "fields": diff})
        else:
            exact += 1
    return {
        "checked": checked,
        "exact": exact,
        "exact_rate": exact / checked if checked else None,
        "missing_trigger_count": len(missing_trigger),
        "missing_trigger_ids": missing_trigger,
        "mismatch_count": len(mismatches),
        "mismatches": mismatches[:50],
    }


def check_fill_selection(outcomes: dict[str, dict]) -> dict:
    recon_path = DATA / "alpaca_recon_snapshot.json"
    if not recon_path.exists():
        return {"available": False}
    recon = json.loads(recon_path.read_text())
    buckets: dict[str, dict[str, list[float]]] = {
        "MR": {"filled": [], "never_filled": []},
        "ORB": {"filled": [], "never_filled": []},
    }
    for row in recon.get("rows", []):
        tid = row.get("tid")
        strategy = "ORB" if str(tid).startswith("ORB:") else "MR"
        outcome = outcomes.get(tid) or {}
        paper_r = num(outcome.get("outcome_r"))
        if paper_r is None:
            continue
        via = str(row.get("close_via") or "")
        if via.startswith("never_filled"):
            buckets[strategy]["never_filled"].append(paper_r)
        elif row.get("real_r") is not None:
            buckets[strategy]["filled"].append(paper_r)

    result = {"available": True, "strategies": {}}
    for strategy, group in buckets.items():
        filled = group["filled"]
        missed = group["never_filled"]
        result["strategies"][strategy] = {
            "filled_n": len(filled),
            "filled_counterfactual_avg_r": statistics.mean(filled) if filled else None,
            "never_filled_n": len(missed),
            "never_filled_counterfactual_avg_r": statistics.mean(missed) if missed else None,
            "never_filled_win_rate": sum(v > 0 for v in missed) / len(missed) if missed else None,
            "selection_gap_r": statistics.mean(missed) - statistics.mean(filled) if missed and filled else None,
        }
    return result


def contains(path: str, needle: str) -> bool:
    p = ROOT / path
    return p.exists() and needle in p.read_text()


def crontab_text() -> str:
    try:
        return subprocess.check_output(["crontab", "-l"], text=True, stderr=subprocess.DEVNULL)
    except Exception:
        return ""


def check_contracts() -> dict:
    cron = crontab_text()
    checks = [
        {
            "id": "mr_scan_cadence",
            "live": "5-minute cron" if "*/5" in cron and "mean_reversion_wrapper.sh" in cron else "unknown",
            "backtest": "15-minute sampling" if contains("walkforward_search.py", "ts.minute % 15") else "unknown",
            "parity": not ("*/5" in cron and contains("walkforward_search.py", "ts.minute % 15")),
            "severity": "high",
        },
        {
            "id": "orb_break_definition",
            "live": "close-confirmed" if contains("orb_scanner.py", 'b["Close"] > orh') else "unknown",
            "backtest": "wick-touch" if contains("walkforward_search.py", "pH[j] >= orh") else "unknown",
            "parity": not (
                contains("orb_scanner.py", 'b["Close"] > orh')
                and contains("walkforward_search.py", "pH[j] >= orh")
            ),
            "severity": "critical",
        },
        {
            "id": "mr_universe",
            "live": "wide universe" if contains("mean_reversion_scanner.py", "load_universe_for_scanning") else "unknown",
            "backtest": "core S&P 100" if contains("backtest_mean_reversion.py", "fetch_sp100") else "unknown",
            "parity": not (
                contains("mean_reversion_scanner.py", "load_universe_for_scanning")
                and contains("backtest_mean_reversion.py", "fetch_sp100")
            ),
            "severity": "critical",
        },
        {
            "id": "orb_entry_bar_handling",
            "live": "order submitted after confirmed bar",
            "backtest": "multiple inconsistent simulators",
            "parity": False,
            "severity": "critical",
        },
    ]
    return {"checks": checks, "passed": sum(c["parity"] for c in checks), "failed": sum(not c["parity"] for c in checks)}


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def check_freeze() -> dict:
    path = DATA / "research/freezes/latest.json"
    if not path.exists():
        return {"available": False, "unchanged": False, "mismatches": ["freeze absent"]}
    freeze = json.loads(path.read_text())
    mismatches = []
    for row in freeze.get("files", []):
        if not row.get("exists"):
            continue
        current = ROOT / row["path"]
        if not current.exists() or sha256(current) != row.get("sha256"):
            mismatches.append(row["path"])
    return {
        "available": True,
        "freeze_id": freeze.get("freeze_id"),
        "unchanged": not mismatches,
        "mismatches": mismatches,
    }


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    temp.write_text(text)
    os.chmod(temp, 0o600)
    os.replace(temp, path)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--strict", action="store_true", help="exit nonzero when parity failures are found")
    args = ap.parse_args()

    triggers = load_triggers()
    outcomes = load_outcomes()
    created = datetime.now(timezone.utc)
    report_id = f"parity_{created.strftime('%Y%m%dT%H%M%SZ')}"
    report = {
        "report_id": report_id,
        "created_at": created.isoformat(timespec="seconds"),
        "mode": "offline_read_only",
        "freeze": check_freeze(),
        "recorded_order_payload_parity": check_order_payloads(triggers),
        "fill_selection": check_fill_selection(outcomes),
        "live_backtest_contracts": check_contracts(),
        "verdict": "mismatches_found",
    }
    payload = json.dumps(report, indent=2, sort_keys=True) + "\n"
    out = REPORT_DIR / f"{report_id}.json"
    atomic_write(out, payload)
    atomic_write(REPORT_DIR / "latest.json", payload)
    print(payload, end="")
    if args.strict and (
        not report["freeze"]["unchanged"]
        or report["recorded_order_payload_parity"]["mismatch_count"]
        or report["live_backtest_contracts"]["failed"]
    ):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
