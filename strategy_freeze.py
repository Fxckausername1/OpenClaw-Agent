#!/usr/bin/env python3
"""Create a non-secret, immutable manifest of the current trading system."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent
OUT_DIR = ROOT / "data" / "research" / "freezes"
CRITICAL_FILES = [
    "mean_reversion_scanner.py",
    "orb_scanner.py",
    "alpaca_executor.py",
    "alpaca_recon.py",
    "portfolio_gate.py",
    "guardrails.py",
    "paper_eval.py",
    "orb_paper_eval.py",
    "walkforward_search.py",
    "data/live_params.json",
]


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def command(args: list[str]) -> str | None:
    try:
        return subprocess.check_output(args, cwd=ROOT, text=True, stderr=subprocess.DEVNULL).strip()
    except Exception:
        return None


def atomic_write(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    temp.write_text(payload)
    os.chmod(temp, 0o600)
    os.replace(temp, path)


def build_manifest(label: str) -> dict:
    files = []
    for relative in CRITICAL_FILES:
        path = ROOT / relative
        files.append(
            {
                "path": relative,
                "exists": path.exists(),
                "bytes": path.stat().st_size if path.exists() else None,
                "sha256": sha256(path) if path.exists() else None,
            }
        )
    live_params = None
    params_path = ROOT / "data/live_params.json"
    if params_path.exists():
        live_params = json.loads(params_path.read_text())
    created = datetime.now(timezone.utc)
    freeze_id = f"freeze_{created.strftime('%Y%m%dT%H%M%SZ')}"
    return {
        "freeze_id": freeze_id,
        "created_at": created.isoformat(timespec="seconds"),
        "label": label,
        "purpose": "research baseline only; no runtime behavior changed",
        "git": {
            "head": command(["git", "rev-parse", "HEAD"]),
            "branch": command(["git", "branch", "--show-current"]),
            "status": command(["git", "status", "--short"]),
        },
        "live_params": live_params,
        "files": files,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--label", default="pre-validation truth-layer baseline")
    args = ap.parse_args()
    manifest = build_manifest(args.label)
    payload = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    out = OUT_DIR / f"{manifest['freeze_id']}.json"
    if out.exists():
        raise SystemExit(f"refusing to overwrite existing freeze: {out}")
    atomic_write(out, payload)
    atomic_write(OUT_DIR / "latest.json", payload)
    print(out)
    print(manifest["freeze_id"])


if __name__ == "__main__":
    main()
