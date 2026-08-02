#!/usr/bin/env bash
set -u
export HOME="/home/heff"
export PATH="/home/heff/.openclaw/workspace/venv/bin:$PATH"
ROOT="/home/heff/.openclaw/workspace"
LOGDIR="$ROOT/logs"
mkdir -p "$LOGDIR"
OUT="$LOGDIR/compute_real_adv_wrapper.log"

# Refreshes data/real_adv.json (WSS's real-ADV standardizing scalar, added 2026-07-04) --
# a ~3s Alpaca daily-bars pull across the ~194-ticker universe, weekly is plenty since 30d
# ADV doesn't shift much day to day. Runs Sunday, market closed, box otherwise idle.
if pgrep -f 'mean_reversion_scanner.py|orb_scanner.py|continuous_search.py' >/dev/null 2>&1; then
  echo "$(date -u) another heavy job is running, deferring" >> "$OUT"
  exit 0
fi

echo "$(date -u) running compute_real_adv.py" >> "$OUT"
"$ROOT/venv/bin/python" "$ROOT/compute_real_adv.py" >> "$OUT" 2>&1
EXIT=$?
if [ "$EXIT" -ne 0 ]; then
  openclaw message send --channel telegram --target 7590346809 \
    --message "Weekly real-ADV refresh failed (exit $EXIT). Check $OUT." >/dev/null 2>&1
fi
exit $EXIT
