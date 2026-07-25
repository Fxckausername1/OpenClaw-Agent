#!/usr/bin/env bash
set -u
export HOME="/home/heff"
export PATH="/home/heff/.openclaw/workspace/venv/bin:$PATH"
ROOT="/home/heff/.openclaw/workspace"
LOG="$ROOT/logs/catalyst_brief.log"
MODE="${1:-build}"

mkdir -p "$ROOT/logs"
exec 9>/tmp/catalyst_brief.lock
flock -n 9 || exit 0

read -r HOUR MINUTE WEEKDAY < <(TZ=America/New_York date '+%H %M %u')
if [ "$WEEKDAY" -gt 5 ]; then
  exit 0
fi

case "$MODE" in
  build)
    [ "$HOUR" = "09" ] && [ "$MINUTE" -ge 51 ] && [ "$MINUTE" -le 56 ] || exit 0
    ;;
  hinge)
    [ "$HOUR" = "10" ] && [ "$MINUTE" -ge 5 ] && [ "$MINUTE" -le 9 ] || exit 0
    ;;
  score)
    [ "$HOUR" = "16" ] && [ "$MINUTE" -ge 8 ] && [ "$MINUTE" -le 20 ] || exit 0
    ;;
  *)
    echo "unknown mode: $MODE" >&2
    exit 2
    ;;
esac

echo "$(date -u +%FT%TZ) starting catalyst brief $MODE" >> "$LOG"
cd "$ROOT" || exit 1
if [ "$MODE" = "build" ]; then
  "$ROOT/venv/bin/python" "$ROOT/macro_news_pull.py" >> "$LOG" 2>&1 ||     echo "$(date -u +%FT%TZ) macro refresh failed; report will disclose unavailable macro coverage" >> "$LOG"
fi
"$ROOT/venv/bin/python" "$ROOT/catalyst_brief.py" --mode "$MODE" --publish >> "$LOG" 2>&1
STATUS=$?
if [ "$STATUS" -eq 0 ] && [ "$MODE" = "build" ]; then
  /usr/bin/openclaw message send --channel telegram --target 7590346809 \
    --message "BOT_NEXUS Catalyst Brief is ready: https://heff-trading-dashboard.netlify.app/briefs.html" >/dev/null 2>&1 || true
fi
echo "$(date -u +%FT%TZ) catalyst brief $MODE exit=$STATUS" >> "$LOG"
exit "$STATUS"
