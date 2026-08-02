#!/usr/bin/env bash
set -u
export HOME="/home/heff"
export PATH="/home/heff/.openclaw/workspace/venv/bin:$PATH"
ROOT="/home/heff/.openclaw/workspace"
LOGDIR="$ROOT/logs"
mkdir -p "$LOGDIR"
# Fixed filename, truncated each run (2026-06-28: was per-invocation timestamped --
# unbounded sprawl). Durable rotated/retained record now lives in logs/scanner_orb.log
# via loguru inside orb_scanner.py itself.
OUT="$LOGDIR/orb_wrapper.log"
MSG="$ROOT/data/orb_message_latest.txt"

# Yield to premarket_scanner's once-a-day, time-critical run (2026-07-09 fix):
# this scanner cycles every 2-5min all session and can skip a single tick for
# free, but premarket_scanner only gets one shot and was being starved by this
# scanner running continuously right through its 9:20 ET window.
NOW_ET=$(TZ=America/New_York date +%H:%M)
if [[ "$NOW_ET" > "09:17" && "$NOW_ET" < "09:23" ]]; then
  exit 0
fi

# Cron covers both EDT and EST in UTC. Avoid launching the Python stack during the
# inactive side of that window; keep ten minutes after the close for final evaluation.
if [[ "$NOW_ET" < "09:30" || "$NOW_ET" > "16:10" ]]; then
  exit 0
fi

# 2026-07-05 audit: no overlap guard existed for this */2 job. Non-blocking so a slow
# run just lets the next tick skip instead of piling up a second process.
exec 9>/tmp/orb_scanner.lock
flock -n 9 || exit 0

"$ROOT/venv/bin/python" "$ROOT/orb_scanner.py" > "$OUT" 2>&1
EXIT=$?
if [ "$EXIT" -ne 0 ]; then
  openclaw message send --channel telegram --target 7590346809 \
    --message "ORB scanner failed (exit $EXIT). Check $OUT." >/dev/null 2>&1
  exit $EXIT
fi
if [ -s "$MSG" ]; then
  # Send once, then delete the message file. Without this the wrapper re-sent the same alert
  # every cron tick AFTER the close: the scanner returns early when the market is closed and
  # never clears MSG, so the last intraday alert got re-blasted for ~2hrs post-close. Delete
  # only on a SUCCESSFUL send so a transient openclaw failure retries next tick (no lost alert).
  if openclaw message send --channel telegram --target 7590346809 \
       --message "$(cat "$MSG")" >> "$OUT" 2>&1; then
    rm -f "$MSG"
  fi
fi
"$ROOT/venv/bin/python" "$ROOT/orb_paper_eval.py" >> "$OUT" 2>&1
exit 0
