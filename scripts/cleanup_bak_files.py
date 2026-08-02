#!/usr/bin/env python3
"""Housekeeping (2026-07-05 audit, tightened same day after heff flagged the risk):
prune pre-edit code-backup snapshots so disk usage doesn't grow unbounded forever.

Deliberately narrow, by construction, not by convention:
  - ONLY looks directly inside the workspace root and workspace/scripts -- never
    recurses, never touches data/, docs/, trading-stack/, trading_py/, venv/,
    node_modules/, or any other subdirectory. Those are structurally unreachable,
    not just excluded by a name filter.
  - ONLY matches the "<name>.py.bak_<label>" / "<name>.sh.bak_<label>" convention
    this codebase uses before every live-code edit. Never touches .db/.json/.csv/
    .txt/.md backups (e.g. data/options_eval.db.bak_20260702_133113_prereset, the
    pre-reset options-ledger snapshot, or live_params.json.bak_* risk-config
    history) -- those aren't even in the two scanned directories, but the pattern
    match is a second, independent guard against ever widening scope by accident.
  - ALWAYS keeps the single newest backup of every distinct file, forever,
    regardless of age. Only a file's OLDER, redundant backups are ever candidates
    for deletion, and only once they clear RETENTION_DAYS.

Run manually with --dry-run to see exactly what it would do without deleting
anything: ./venv/bin/python scripts/cleanup_bak_files.py --dry-run
"""
import argparse
import os
import re
import time
from pathlib import Path

ROOT = Path("/home/heff/.openclaw/workspace")
SCAN_DIRS = [ROOT, ROOT / "scripts"]
RETENTION_DAYS = 30
BAK_RE = re.compile(r"^(?P<base>.+\.(?:py|sh))\.bak_.+$")


def find_backup_groups():
    """Return {(dir, base_name): [Path, ...]} for every matching backup, grouped by
    the directory it lives in + the original filename it's a backup of."""
    groups = {}
    for d in SCAN_DIRS:
        if not d.is_dir():
            continue
        for entry in os.scandir(d):
            if not entry.is_file(follow_symlinks=False):
                continue
            m = BAK_RE.match(entry.name)
            if not m:
                continue
            key = (str(d), m.group("base"))
            groups.setdefault(key, []).append(Path(entry.path))
    return groups


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="log what would be removed, delete nothing")
    args = ap.parse_args()

    cutoff = time.time() - RETENTION_DAYS * 86400
    groups = find_backup_groups()
    kept_newest = 0
    removed = 0

    for (d, base), paths in sorted(groups.items()):
        paths.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        newest, rest = paths[0], paths[1:]
        kept_newest += 1
        print(f"KEEP (newest of {base}): {newest}")
        for p in rest:
            age_days = (time.time() - p.stat().st_mtime) / 86400
            if p.stat().st_mtime < cutoff:
                if args.dry_run:
                    print(f"WOULD REMOVE ({age_days:.0f}d old): {p}")
                else:
                    print(f"REMOVE ({age_days:.0f}d old): {p}")
                    p.unlink()
                removed += 1
            else:
                print(f"keep (only {age_days:.0f}d old, under {RETENTION_DAYS}d): {p}")

    print(f"--- summary: {kept_newest} distinct files tracked, "
          f"{'would remove' if args.dry_run else 'removed'} {removed} old duplicate(s) ---")


if __name__ == "__main__":
    main()
