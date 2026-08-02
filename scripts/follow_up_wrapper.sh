#!/usr/bin/env bash
# generate followup data only — Telegram send consolidated into morning_report.py
set -u
export HOME="/home/heff"
export PATH="/home/heff/.openclaw/workspace/venv/bin:$PATH"
ROOT="/home/heff/.openclaw/workspace"; mkdir -p "$ROOT/logs"
export GOOGLE_SA_KEY_FILE="$ROOT/credentials/gcp_sa.json"
"$ROOT/venv/bin/python" "$ROOT/follow_up_reminders.py" > "$ROOT/logs/followup_$(date -u +%Y%m%d_%H%M%S).log" 2>&1
exit 0
