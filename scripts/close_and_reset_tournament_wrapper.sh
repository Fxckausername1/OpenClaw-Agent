#!/usr/bin/env bash
set -u
export HOME="/home/heff"
export PATH="/home/heff/.openclaw/workspace/venv/bin:$PATH"
ROOT="/home/heff/.openclaw/workspace"
LOGDIR="$ROOT/logs"
mkdir -p "$LOGDIR"
OUT="$LOGDIR/close_and_reset_tournament_$(date -u +%Y%m%d_%H%M).log"

echo "$(date -u) running close_and_reset_tournament.py (one-shot, market-open retry)" >> "$OUT"
"$ROOT/venv/bin/python" "$ROOT/close_and_reset_tournament.py" >> "$OUT" 2>&1
EXIT=$?
echo "$(date -u) exit=$EXIT" >> "$OUT"

# one-shot: remove this exact cron line from the crontab regardless of outcome (success or
# a second market-open failure) -- don't silently retry forever on a future day.
crontab -l | grep -v 'close_and_reset_tournament_wrapper.sh' | crontab -

if [ "$EXIT" -ne 0 ]; then
  openclaw message send --channel telegram --target 7590346809 \
    --message "Options tournament close+reset FAILED again at market open (exit $EXIT). Check $OUT -- may need a manual close." >/dev/null 2>&1
else
  openclaw message send --channel telegram --target 7590346809 \
    --message "Options tournament closed the open O spread and reset to a clean slate (Beta(1,1), 0 trades, ledger wiped, backup kept). Fresh start." >/dev/null 2>&1
fi
exit 0
