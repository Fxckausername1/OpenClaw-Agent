#!/usr/bin/env python3
"""Build a research-only signal -> gate -> order -> fill/outcome ledger.

Inputs are existing runtime logs. Outputs live only under data/research and are
derived/replaceable; no scanner, executor, cron, or dashboard file is touched.
"""

from __future__ import annotations

import csv
import glob
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
OUT_DIR = DATA / "research" / "intent_ledgers"


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


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    temp.write_text(text)
    os.chmod(temp, 0o600)
    os.replace(temp, path)


def file_fingerprint(paths: list[Path]) -> str:
    h = hashlib.sha256()
    for path in sorted(paths):
        h.update(str(path.relative_to(ROOT)).encode())
        h.update(str(path.stat().st_size).encode())
        h.update(str(path.stat().st_mtime_ns).encode())
    return h.hexdigest()


def main() -> None:
    trigger_paths = [Path(p) for p in glob.glob(str(DATA / "mr_triggers_*.jsonl"))]
    trigger_paths += [Path(p) for p in glob.glob(str(DATA / "orb_triggers_*.jsonl"))]
    rows: dict[str, dict] = {}

    for path in sorted(trigger_paths):
        for trigger in read_jsonl(path):
            tid = trigger.get("trade_id")
            if not tid:
                continue
            row = rows.setdefault(tid, {"trade_id": tid})
            row.update(
                {
                    "strategy": "ORB" if tid.startswith("ORB:") else "MR",
                    "strategy_version": trigger.get("strategy"),
                    "ticker": trigger.get("ticker"),
                    "side": trigger.get("side"),
                    "signal_time": trigger.get("entry_time"),
                    "detected_at": trigger.get("detected_at"),
                    "intended_entry": trigger.get("entry"),
                    "intended_stop": trigger.get("stop"),
                    "intended_target": trigger.get("target", trigger.get("t1")),
                    "planned_rr": trigger.get("planned_rr"),
                    "risk_dollars": trigger.get("risk_dollars"),
                    "universe": trigger.get("universe"),
                    "regime_ok": trigger.get("regime_ok"),
                    "sector_hot": trigger.get("sector_hot"),
                    "trigger_source": str(path.relative_to(ROOT)),
                    "gate_approved": False,
                    "gate_rejections": [],
                    "submitted": False,
                    "fill_state": "not_submitted",
                }
            )

    gate_path = DATA / "portfolio_gate_log.jsonl"
    for event in read_jsonl(gate_path):
        for tid in event.get("approved", []):
            row = rows.setdefault(tid, {"trade_id": tid, "strategy": "ORB" if tid.startswith("ORB:") else "MR"})
            row["gate_approved"] = True
            row["gate_last_context"] = event.get("context")
            row["gate_last_ts"] = event.get("ts")
        for rejected in event.get("rejected", []):
            tid = rejected.get("trade_id")
            if not tid:
                continue
            row = rows.setdefault(tid, {"trade_id": tid, "strategy": "ORB" if tid.startswith("ORB:") else "MR"})
            row.setdefault("gate_rejections", []).append(
                {"ts": event.get("ts"), "context": event.get("context"), "reason": rejected.get("reason")}
            )

    orders_path = DATA / "alpaca_orders.jsonl"
    for order in read_jsonl(orders_path):
        tid = order.get("trade_id")
        if not tid:
            continue
        row = rows.setdefault(tid, {"trade_id": tid, "strategy": "ORB" if tid.startswith("ORB:") else "MR"})
        row["submitted"] = isinstance(order.get("alpaca"), str)
        row["order_time"] = order.get("ts")
        row["order_mode"] = order.get("mode")
        payload = order.get("payload") or {}
        row["order_type"] = payload.get("type")
        row["order_limit"] = payload.get("limit_price")
        row["order_qty"] = payload.get("qty")
        row["order_class"] = payload.get("order_class")
        row["fill_state"] = "submitted" if row["submitted"] else "submission_failed"

    outcomes = {}
    for name in ("paper_trades.csv", "orb_paper_trades.csv"):
        path = DATA / name
        if not path.exists():
            continue
        with path.open(newline="") as fh:
            for outcome in csv.DictReader(fh):
                outcomes[outcome.get("trade_id")] = outcome
    for tid, outcome in outcomes.items():
        if tid not in rows:
            continue
        row = rows[tid]
        row["paper_outcome_r"] = outcome.get("outcome_r")
        row["paper_exit_reason"] = outcome.get("exit_reason")
        row["paper_exit_time"] = outcome.get("close_time")

    recon_path = DATA / "alpaca_recon_snapshot.json"
    if recon_path.exists():
        recon = json.loads(recon_path.read_text())
        for rec in recon.get("rows", []):
            tid = rec.get("tid")
            if not tid:
                continue
            row = rows.setdefault(tid, {"trade_id": tid, "strategy": "ORB" if tid.startswith("ORB:") else "MR"})
            row["real_r"] = rec.get("real_r")
            row["paired_sim_r"] = rec.get("sim_r")
            row["entry_slip_bps"] = rec.get("entry_slip_bps")
            row["close_via"] = rec.get("close_via")
            row["is_open"] = rec.get("open")
            via = str(rec.get("close_via") or "")
            if via.startswith("never_filled"):
                row["fill_state"] = "never_filled"
            elif rec.get("open"):
                row["fill_state"] = "filled_open"
            elif rec.get("real_r") is not None:
                row["fill_state"] = "filled_closed"
            elif via:
                row["fill_state"] = "closed_unresolved"

    ordered = [rows[k] for k in sorted(rows)]
    created = datetime.now(timezone.utc)
    ledger_id = f"intent_{created.strftime('%Y%m%dT%H%M%SZ')}"
    payload = "".join(json.dumps(r, sort_keys=True, separators=(",", ":")) + "\n" for r in ordered)
    out = OUT_DIR / f"{ledger_id}.jsonl"
    atomic_write(out, payload)
    atomic_write(OUT_DIR / "latest.jsonl", payload)

    inputs = [p for p in trigger_paths + [gate_path, orders_path, recon_path, DATA / "paper_trades.csv", DATA / "orb_paper_trades.csv"] if p.exists()]
    counts = {
        "intents": len(ordered),
        "approved": sum(bool(r.get("gate_approved")) for r in ordered),
        "submitted": sum(bool(r.get("submitted")) for r in ordered),
        "filled_closed": sum(r.get("fill_state") == "filled_closed" for r in ordered),
        "never_filled": sum(r.get("fill_state") == "never_filled" for r in ordered),
        "not_submitted": sum(r.get("fill_state") == "not_submitted" for r in ordered),
    }
    manifest = {
        "ledger_id": ledger_id,
        "created_at": created.isoformat(timespec="seconds"),
        "input_fingerprint": file_fingerprint(inputs),
        "inputs": [str(p.relative_to(ROOT)) for p in sorted(inputs)],
        "counts": counts,
        "output": str(out.relative_to(ROOT)),
    }
    atomic_write(OUT_DIR / f"{ledger_id}.manifest.json", json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    atomic_write(OUT_DIR / "latest.manifest.json", json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    print(json.dumps(manifest, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
