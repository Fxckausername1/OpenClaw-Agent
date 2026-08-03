"""Immutable run identity and per-run output isolation for BT experiments.

The problem this fixes. Every BT-3 experiment so far wrote into ONE fixed path
per experiment name, e.g.
`data/thetadata/bt3_b1_indicator_only/bt3_b1_indicator_only_ledger.json`, and
`bt2_simulator.append_ledger_rows` APPENDS. So a re-run with different code, a
different config, or a partial (RAM-aborted) execution silently accumulated its
rows into the same "canonical" ledger alongside earlier, differently-produced
rows. Nothing in the file distinguished them. That makes any ledger-level
statistic un-auditable after the second run, and it is how a partial run's rows
can quietly contaminate a headline number.

The rule now: every execution gets an immutable `run_id`, its OWN directory, and
a manifest recording exactly what produced it (code hashes, config, git commit,
completeness). A run directory is never reused and never appended to by a later
run. Comparisons are made BETWEEN run directories, not inside a shared file.

Completeness is recorded explicitly (`completed`, `sessions_processed` vs
`sessions_expected`) because this box's RAM guard can legitimately abort a
backfill/replay part-way through -- a partial run is valid data about a subset,
but it must never be mistaken for a full one.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
import os
import subprocess
import uuid
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
RUNS_DIR = ROOT / "data" / "thetadata" / "runs"

# Modules whose content materially determines a backtest result. Hashed into the
# manifest so two runs can be compared for "same code?" without guessing.
CODE_FILES = (
    "thetadata_pipeline/bt2_simulator.py",
    "thetadata_pipeline/bt2_fills.py",
    "thetadata_pipeline/bt2_exits.py",
    "thetadata_pipeline/bt2_selector.py",
    "thetadata_pipeline/heff_smc_engine.py",
    "thetadata_pipeline/heff_smc_replay.py",
)


def _git_commit() -> Optional[str]:
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(ROOT),
                              capture_output=True, text=True, timeout=10)
        return out.stdout.strip() or None
    except Exception:
        return None


def _git_dirty() -> Optional[bool]:
    try:
        out = subprocess.run(["git", "status", "--porcelain"], cwd=str(ROOT),
                              capture_output=True, text=True, timeout=15)
        return bool(out.stdout.strip())
    except Exception:
        return None


def _hash_file(rel: str) -> Optional[str]:
    path = ROOT / rel
    if not path.exists():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


@dataclasses.dataclass(frozen=True)
class RunContext:
    run_id: str
    experiment_id: str
    root: Path

    @property
    def ledger_path(self) -> Path:
        return self.root / "ledger.json"

    @property
    def report_path(self) -> Path:
        return self.root / "report.json"

    @property
    def summary_path(self) -> Path:
        return self.root / "summary.md"

    @property
    def manifest_path(self) -> Path:
        return self.root / "manifest.json"

    def read_manifest(self) -> dict:
        return json.loads(self.manifest_path.read_text())

    def update_manifest(self, **fields) -> dict:
        manifest = self.read_manifest()
        manifest.update(fields)
        manifest["updated_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
        tmp = self.manifest_path.with_suffix(f".tmp{os.getpid()}")
        tmp.write_text(json.dumps(manifest, indent=2, default=str))
        os.replace(tmp, self.manifest_path)
        return manifest

    def mark_complete(self, *, sessions_processed: int, sessions_expected: int,
                      note: str = "") -> dict:
        return self.update_manifest(
            completed=sessions_processed >= sessions_expected,
            sessions_processed=sessions_processed,
            sessions_expected=sessions_expected,
            completeness_note=note or (
                "" if sessions_processed >= sessions_expected else
                f"PARTIAL RUN: {sessions_processed}/{sessions_expected} sessions. Do NOT "
                "compare this against a full run as if it were one."),
        )


def new_run(experiment_id: str, *, params: Optional[dict] = None,
            label: str = "", runs_dir: Path = RUNS_DIR) -> RunContext:
    """Creates a fresh, never-before-used run directory + manifest.

    run_id is timestamp-prefixed so directory listing is chronological, with a
    short uuid suffix so two runs started in the same second cannot collide.
    Refuses to touch an existing directory -- immutability is the point."""
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_id = f"{stamp}-{uuid.uuid4().hex[:6]}"
    root = Path(runs_dir) / experiment_id / run_id
    if root.exists():
        raise RuntimeError(f"run directory already exists, refusing to reuse: {root}")
    root.mkdir(parents=True)

    manifest = {
        "run_id": run_id,
        "experiment_id": experiment_id,
        "label": label,
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "git_commit": _git_commit(),
        "git_dirty": _git_dirty(),
        "code_hashes": {rel: _hash_file(rel) for rel in CODE_FILES},
        "params": params or {},
        "completed": False,
        "sessions_processed": None,
        "sessions_expected": None,
        "completeness_note": "run in progress",
    }
    (root / "manifest.json").write_text(json.dumps(manifest, indent=2, default=str))
    return RunContext(run_id=run_id, experiment_id=experiment_id, root=root)


def list_runs(experiment_id: str, runs_dir: Path = RUNS_DIR) -> list:
    base = Path(runs_dir) / experiment_id
    if not base.exists():
        return []
    out = []
    for d in sorted(base.iterdir()):
        manifest = d / "manifest.json"
        if manifest.is_file():
            try:
                out.append(json.loads(manifest.read_text()))
            except json.JSONDecodeError:
                continue
    return out


def assert_fresh_ledger(path: Path) -> None:
    """Guard for the append-contamination failure mode: a run's ledger must not
    already exist when the run starts."""
    if Path(path).exists():
        raise RuntimeError(
            f"ledger {path} already exists -- a run must never append to a ledger from "
            "another run. Use new_run() to get an isolated directory.")
