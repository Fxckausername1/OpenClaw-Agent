#!/usr/bin/env bash
# generate artist leads only — digest summarized in morning_report.py (full DMs in the CSV)
set -u
export HOME="/home/heff"
export PATH="/home/heff/.openclaw/workspace/venv/bin:$PATH"
ROOT="/home/heff/.openclaw/workspace"; mkdir -p "$ROOT/logs"
OUT="$ROOT/logs/artist_pipeline_$(date -u +%Y%m%d_%H%M%S).log"
"$ROOT/venv/bin/python" "$ROOT/artist_pipeline.py" > "$OUT" 2>&1
EXIT=$?
if [ "$EXIT" -ne 0 ]; then
  openclaw message send --channel telegram --target 7590346809 --message "Artist pipeline failed (exit $EXIT). Check $OUT." >/dev/null 2>&1
fi
exit 0
