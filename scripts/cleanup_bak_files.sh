#!/usr/bin/env bash
# Housekeeping (2026-07-05 audit, tightened same day after heff flagged the risk of
# touching real work). Thin wrapper around cleanup_bak_files.py, which is scoped by
# construction to ONLY the workspace root + scripts/ dir, ONLY *.py.bak_*/*.sh.bak_*
# filenames, and ALWAYS keeps the newest backup of every file forever -- see that
# script's own docstring for the full guarantee. Weekly cron, off-hours.
set -u

ROOT="/home/heff/.openclaw/workspace"
LOGDIR="$ROOT/logs"
mkdir -p "$LOGDIR"
OUT="$LOGDIR/bak_cleanup.log"

{
  echo "=== $(date -u +%Y-%m-%dT%H:%M:%SZ) bak cleanup ==="
  "$ROOT/venv/bin/python" "$ROOT/scripts/cleanup_bak_files.py"
  echo "done"
} >> "$OUT" 2>&1
