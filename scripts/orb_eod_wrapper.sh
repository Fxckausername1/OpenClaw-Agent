#!/usr/bin/env bash
set -u
export HOME="/home/heff"
export PATH="/home/heff/.openclaw/workspace/venv/bin:$PATH"
ROOT="/home/heff/.openclaw/workspace"
LOGDIR="$ROOT/logs"
mkdir -p "$LOGDIR"
TS=$(date -u +"%Y%m%d_%H%M%S")
OUT="$LOGDIR/orb_eod_${TS}.log"
RECAP="$ROOT/data/orb_recap_latest.txt"
"$ROOT/venv/bin/python" "$ROOT/orb_paper_eval.py" --eod > "$OUT" 2>&1
if [ -s "$RECAP" ]; then
  openclaw message send --channel telegram --target 7590346809 \
    --message "$(cat "$RECAP")" >> "$OUT" 2>&1
fi
exit 0
