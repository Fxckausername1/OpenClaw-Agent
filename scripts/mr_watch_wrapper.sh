#!/usr/bin/env bash
# Fast trigger loop: runs every minute, polls ONLY names already in WATCH and
# fires the TRIGGER alert the moment the break happens (the */15 full scan still
# owns new-setup detection + lifecycle). Cheap: usually 0 names on watch -> instant.
set -u

export HOME="/home/heff"
export PATH="/home/heff/.openclaw/workspace/venv/bin:$PATH"

ROOT="/home/heff/.openclaw/workspace"
LOGDIR="$ROOT/logs"
mkdir -p "$LOGDIR"
OUT="$LOGDIR/mr_watch_$(date -u +%Y%m%d).log"   # one rolling log per day (no spam)
MSG="$ROOT/data/mr_watch_message_latest.txt"

# 2026-07-05 audit: this loop fires every minute with no wrapper-level overlap guard.
# The script's own internal lock is non-blocking for --watch-only (correctly skips a
# minute rather than queuing), but a slow run could still leave a second python process
# spun up (pandas/numpy/yfinance import cost) just to immediately lose that race. A
# separate lock name from mean_reversion_wrapper.sh's -- this only guards watch-only
# against ITSELF; full-scan-vs-watch-only serialization stays owned by the script's own
# fcntl lock, unchanged.
exec 9>/tmp/mr_watch.lock
flock -n 9 || exit 0

"$ROOT/venv/bin/python" "$ROOT/mean_reversion_scanner.py" --watch-only >> "$OUT" 2>&1
EXIT=$?

if [ "$EXIT" -ne 0 ]; then
  openclaw message send --channel telegram --target 7590346809 \
    --message "MR watch-loop failed (exit $EXIT). Check $OUT." >/dev/null 2>&1
  exit $EXIT
fi

# Only a fired TRIGGER writes the message file -> send it immediately.
if [ -s "$MSG" ]; then
  # Send once, then delete the message file. Without this the wrapper re-sent the same alert
  # every cron tick AFTER the close: the scanner returns early when the market is closed and
  # never clears MSG, so the last intraday alert got re-blasted for ~2hrs post-close. Delete
  # only on a SUCCESSFUL send so a transient openclaw failure retries next tick (no lost alert).
  if openclaw message send --channel telegram --target 7590346809 \
       --message "$(cat "$MSG")" >> "$OUT" 2>&1; then
    rm -f "$MSG"
  fi
  # open the paper position right away so the track record reflects the fast entry
  "$ROOT/venv/bin/python" "$ROOT/paper_eval.py" >> "$OUT" 2>&1
fi

exit 0
