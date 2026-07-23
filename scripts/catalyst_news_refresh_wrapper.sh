#!/usr/bin/env bash
set -u
export HOME="/home/heff"
export PATH="/home/heff/.openclaw/workspace/venv/bin:$PATH"
ROOT="/home/heff/.openclaw/workspace"
LOG="$ROOT/logs/catalyst_news_refresh.log"

mkdir -p "$ROOT/logs"
exec 9>/tmp/catalyst_news_refresh.lock
flock -n 9 || exit 0

read -r HOUR MINUTE WEEKDAY < <(TZ=America/New_York date '+%H %M %u')
[ "$WEEKDAY" -le 5 ] || exit 0
[ "$HOUR" = "08" ] && [ "$MINUTE" -ge 34 ] && [ "$MINUTE" -le 44 ] || exit 0

echo "$(date -u +%FT%TZ) starting independent calendar + catalyst refresh" >> "$LOG"
cd "$ROOT" || exit 1
"$ROOT/venv/bin/python" "$ROOT/official_calendar_pull.py" >> "$LOG" 2>&1
CAL_STATUS=$?
"$ROOT/venv/bin/python" "$ROOT/catalyst_news_pull.py" >> "$LOG" 2>&1
CAT_STATUS=$?
echo "$(date -u +%FT%TZ) calendar=$CAL_STATUS catalyst=$CAT_STATUS" >> "$LOG"
[ "$CAL_STATUS" -eq 0 ] && [ "$CAT_STATUS" -eq 0 ]
