#!/usr/bin/env bash
set -u
export HOME="/home/heff"
export PATH="/home/heff/.openclaw/workspace/venv/bin:$PATH"
ROOT="/home/heff/.openclaw/workspace"
LOGDIR="$ROOT/logs"
mkdir -p "$LOGDIR"
OUT="$LOGDIR/dashboard_snapshot_wrapper.log"

# 2026-07-05 audit: no overlap guard existed for this 1-59/2 job. Non-blocking so a slow
# GitHub push/API read just lets the next tick skip instead of piling up a second process.
exec 9>/tmp/dashboard_snapshot.lock
flock -n 9 || exit 0

"$ROOT/venv/bin/python" "$ROOT/scripts/dashboard_snapshot.py" > "$OUT" 2>&1
exit 0
