#!/usr/bin/env python3
"""Append-only, fail-open research event ledger for the trading ecosystem."""

import fcntl
import gzip
import hashlib
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
OUT_DIR = ROOT / "data" / "research" / "decision_ledger"
SCHEMA_VERSION = "decision-event.v1"
ET = ZoneInfo("America/New_York")
REQUIRED = {"schema_version", "event_id", "recorded_at", "session_date", "source",
            "event_type", "record_hash", "payload"}


def _json_default(value):
    if hasattr(value, "item"):
        return value.item()
    if isinstance(value, (datetime, Path)):
        return str(value)
    return str(value)


def _code_version():
    try:
        commit = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], cwd=ROOT,
            stderr=subprocess.DEVNULL, timeout=2, text=True).strip()
        dirty = subprocess.run(["git", "diff", "--quiet"], cwd=ROOT, timeout=2).returncode != 0
        return commit + ("+dirty" if dirty else "")
    except Exception:
        return "unknown"


CODE_VERSION = _code_version()


def emit(source, event_type, payload, *, entity_id=None, event_time=None,
         session_date=None, dedupe_key=None):
    """Append one independently hashed event. Logging failure never affects trading."""
    try:
        now = event_time or datetime.now(ET)
        if isinstance(now, str):
            try:
                now = datetime.fromisoformat(now)
            except ValueError:
                now = datetime.now(ET)
        if now.tzinfo is None:
            now = now.replace(tzinfo=ET)
        session_date = session_date or now.astimezone(ET).date().isoformat()
        canonical_key = dedupe_key or f"{source}|{event_type}|{entity_id or ''}|{now.isoformat()}"
        event_id = hashlib.sha256(canonical_key.encode()).hexdigest()[:24]
        row = {
            "schema_version": SCHEMA_VERSION,
            "event_id": event_id,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "event_time": now.astimezone(timezone.utc).isoformat(),
            "session_date": session_date,
            "source": source,
            "event_type": event_type,
            "entity_id": entity_id,
            "code_version": CODE_VERSION,
            "payload": payload,
        }
        canonical = json.dumps(row, sort_keys=True, separators=(",", ":"), default=_json_default)
        row["record_hash"] = hashlib.sha256(canonical.encode()).hexdigest()
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        path = OUT_DIR / f"{session_date}.jsonl"
        with path.open("a", encoding="utf-8") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            fh.write(json.dumps(row, separators=(",", ":"), default=_json_default) + "\n")
            fh.flush()
            if event_type in {"mr_trigger", "orb_trigger", "gate_decision", "order_submission",
                              "broker_order_state", "flatten_result"}:
                os.fsync(fh.fileno())
            fcntl.flock(fh, fcntl.LOCK_UN)
        return event_id
    except Exception:
        return None


def verify(path=None):
    paths = ([Path(path)] if path else
             sorted(OUT_DIR.glob("*.jsonl")) + sorted(OUT_DIR.glob("*.jsonl.gz")))
    report = {"files": len(paths), "rows": 0, "invalid_json": 0, "bad_hash": 0,
              "missing_fields": 0, "duplicate_event_ids": 0}
    seen = set()
    for p in paths:
        lines = (gzip.open(p, "rt", encoding="utf-8") if p.suffix == ".gz"
                 else p.open("r", encoding="utf-8"))
        for line in lines:
            if not line.strip():
                continue
            report["rows"] += 1
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                report["invalid_json"] += 1
                continue
            if not REQUIRED.issubset(row):
                report["missing_fields"] += 1
            event_id = row.get("event_id")
            if event_id in seen:
                report["duplicate_event_ids"] += 1
            seen.add(event_id)
            expected = row.pop("record_hash", None)
            canonical = json.dumps(row, sort_keys=True, separators=(",", ":"), default=_json_default)
            if hashlib.sha256(canonical.encode()).hexdigest() != expected:
                report["bad_hash"] += 1
    report["ok"] = not any(report[k] for k in
                           ("invalid_json", "bad_hash", "missing_fields", "duplicate_event_ids"))
    return report


def compact(days=7):
    """Gzip verified old partitions without deleting any events."""
    cutoff = datetime.now(ET).date().toordinal() - days
    done = []
    for path in sorted(OUT_DIR.glob("*.jsonl")):
        try:
            day = datetime.fromisoformat(path.stem).date()
        except ValueError:
            continue
        if day.toordinal() > cutoff:
            continue
        check = verify(path)
        if not check["ok"]:
            continue
        target = path.with_suffix(path.suffix + ".gz")
        tmp = target.with_suffix(target.suffix + ".tmp")
        with path.open("rb") as src, gzip.open(tmp, "wb", compresslevel=6) as dst:
            while True:
                chunk = src.read(1024 * 1024)
                if not chunk:
                    break
                dst.write(chunk)
        os.replace(tmp, target)
        path.unlink()
        done.append({"file": target.name, "rows": check["rows"],
                     "sha256": hashlib.sha256(target.read_bytes()).hexdigest()})
    return done


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--path")
    ap.add_argument("--compact-days", type=int)
    args = ap.parse_args()
    if args.verify:
        print(json.dumps(verify(args.path), indent=2))
    if args.compact_days is not None:
        print(json.dumps(compact(args.compact_days), indent=2))
