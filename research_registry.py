#!/usr/bin/env python3
"""Append-only registry for trading research experiments.

This tool is deliberately standalone. It does not import, call, or modify any live
scanner/executor module. Records are immutable JSONL rows protected by a file lock.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent
RESEARCH_DIR = ROOT / "data" / "research"
REGISTRY = RESEARCH_DIR / "experiment_registry.jsonl"
LOCK = RESEARCH_DIR / ".experiment_registry.lock"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def git_head() -> str | None:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True, stderr=subprocess.DEVNULL
        ).strip()
    except Exception:
        return None


def parse_json_object(raw: str, field: str) -> dict:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"{field} must be valid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise SystemExit(f"{field} must decode to a JSON object")
    return value


def append_record(record: dict, registry: Path = REGISTRY, lock_path: Path = LOCK) -> None:
    registry.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
    with lock_path.open("a+") as lock_fp:
        fcntl.flock(lock_fp, fcntl.LOCK_EX)
        fd = os.open(registry, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, line.encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
            fcntl.flock(lock_fp, fcntl.LOCK_UN)


def cmd_add(args: argparse.Namespace) -> None:
    record = {
        "experiment_id": f"exp_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}_{uuid.uuid4().hex[:8]}",
        "created_at": utc_now(),
        "name": args.name,
        "hypothesis": args.hypothesis,
        "strategy": args.strategy,
        "stage": args.stage,
        "status": args.status,
        "parameters": parse_json_object(args.parameters, "--parameters"),
        "data": parse_json_object(args.data, "--data"),
        "execution_model": parse_json_object(args.execution_model, "--execution-model"),
        "cost_model": parse_json_object(args.cost_model, "--cost-model"),
        "parent_experiment_id": args.parent,
        "freeze_id": args.freeze_id,
        "notes": args.notes,
        "git_head": git_head(),
    }
    append_record(record)
    print(json.dumps(record, indent=2, sort_keys=True))


def cmd_list(args: argparse.Namespace) -> None:
    if not REGISTRY.exists():
        print("no experiments registered")
        return
    rows = []
    for line in REGISTRY.read_text().splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if args.strategy and row.get("strategy") != args.strategy:
            continue
        rows.append(row)
    for row in rows[-args.limit :]:
        print(
            f"{row.get('experiment_id')} | {row.get('strategy')} | {row.get('stage')} | "
            f"{row.get('status')} | {row.get('name')}"
        )


def cmd_verify(_: argparse.Namespace) -> None:
    if not REGISTRY.exists():
        print("registry absent (valid empty state)")
        return
    seen = set()
    count = 0
    for number, line in enumerate(REGISTRY.read_text().splitlines(), 1):
        row = json.loads(line)
        eid = row["experiment_id"]
        if eid in seen:
            raise SystemExit(f"duplicate experiment_id on line {number}: {eid}")
        seen.add(eid)
        count += 1
    print(f"registry valid: {count} immutable records")


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="command", required=True)
    add = sub.add_parser("add", help="append one immutable experiment record")
    add.add_argument("--name", required=True)
    add.add_argument("--hypothesis", required=True)
    add.add_argument("--strategy", required=True, choices=["MR", "ORB", "PORTFOLIO", "EXECUTION", "DATA"])
    add.add_argument("--stage", required=True, choices=["planned", "development", "walk_forward", "sealed_oos", "forward_paper"])
    add.add_argument("--status", default="planned", choices=["planned", "running", "completed", "accepted", "rejected", "retired"])
    add.add_argument("--parameters", default="{}")
    add.add_argument("--data", default="{}")
    add.add_argument("--execution-model", default="{}")
    add.add_argument("--cost-model", default="{}")
    add.add_argument("--parent")
    add.add_argument("--freeze-id")
    add.add_argument("--notes", default="")
    add.set_defaults(func=cmd_add)
    ls = sub.add_parser("list", help="show recent registry rows")
    ls.add_argument("--strategy")
    ls.add_argument("--limit", type=int, default=20)
    ls.set_defaults(func=cmd_list)
    verify = sub.add_parser("verify", help="validate JSON and unique experiment IDs")
    verify.set_defaults(func=cmd_verify)
    return ap


if __name__ == "__main__":
    args = parser().parse_args()
    args.func(args)
