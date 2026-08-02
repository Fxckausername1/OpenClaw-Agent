#!/usr/bin/env bash
set -u
export HOME="/home/heff"
export PATH="/home/heff/.openclaw/workspace/venv/bin:$PATH"
ROOT="/home/heff/.openclaw/workspace"
LOGDIR="$ROOT/logs"
mkdir -p "$LOGDIR"
TS=$(date -u +"%Y%m%d_%H%M%S")
OUT="$LOGDIR/combined_weekly_${TS}.log"
SUMMARY="$ROOT/data/combined_weekly_latest.txt"
"$ROOT/venv/bin/python" "$ROOT/paper_combined_weekly.py" > "$OUT" 2>&1
if [ -s "$SUMMARY" ]; then
  openclaw message send --channel telegram --target 7590346809 \
    --message "$(cat "$SUMMARY")" >> "$OUT" 2>&1
fi
exit 0
