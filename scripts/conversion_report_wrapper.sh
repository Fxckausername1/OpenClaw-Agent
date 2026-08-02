#!/usr/bin/env bash
# Weekly Pentagon conversion funnel -> Telegram. Read-only against the CRM sheet.
set -u
export HOME="/home/heff"
export PATH="/home/heff/.openclaw/workspace/venv/bin:$PATH"
ROOT="/home/heff/.openclaw/workspace"
LOGDIR="$ROOT/logs"
mkdir -p "$LOGDIR"
TS=$(date -u +"%Y%m%d_%H%M%S")
OUT="$LOGDIR/conversion_${TS}.log"
REPORT="$ROOT/data/conversion_report_latest.txt"

export GOOGLE_SA_KEY_FILE="$ROOT/credentials/gcp_sa.json"
"$ROOT/venv/bin/python" "$ROOT/conversion_tracker.py" > "$OUT" 2>&1

if [ -s "$REPORT" ]; then
  openclaw message send --channel telegram --target 7590346809 \
    --message "$(cat "$REPORT")" >> "$OUT" 2>&1
fi
exit 0
