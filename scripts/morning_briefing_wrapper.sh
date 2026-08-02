#!/usr/bin/env bash
set -u
export HOME="/home/heff"
export PATH="/home/heff/.openclaw/workspace/venv/bin:$PATH"
ROOT="/home/heff/.openclaw/workspace"
LOGDIR="$ROOT/logs"
mkdir -p "$LOGDIR"
TS=$(date -u +"%Y%m%d_%H%M%S")
OUT="$LOGDIR/briefing_${TS}.log"
MSG="$ROOT/data/briefing_latest.txt"

export GOOGLE_SA_KEY_FILE="$ROOT/credentials/gcp_sa.json"
"$ROOT/venv/bin/python" "$ROOT/morning_briefing.py" > "$OUT" 2>&1

if [ -s "$MSG" ]; then
  openclaw message send --channel telegram --target 7590346809 \
    --message "$(cat "$MSG")" >> "$OUT" 2>&1
fi
exit 0
