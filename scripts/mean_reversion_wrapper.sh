#!/usr/bin/env bash
set -u

export HOME="/home/heff"
export PATH="/home/heff/.openclaw/workspace/venv/bin:/home/heff/.openclaw/workspace/stockfish/bin:$PATH"

ROOT="/home/heff/.openclaw/workspace"
LOGDIR="$ROOT/logs"
mkdir -p "$LOGDIR"
# Fixed filename, truncated each run (2026-06-28: was per-invocation timestamped, which
# left 3,722 unbounded files behind). The durable, rotated/retained record now lives in
# logs/scanner_mr.log via loguru inside mean_reversion_scanner.py itself -- this file is
# just a catch-all for anything that happens before the logger initializes (e.g. a crash
# on import) or for paper_eval.py's own stdout, which isn't on loguru.
OUT="$LOGDIR/mean_reversion_wrapper.log"
MSG="$ROOT/data/mr_message_latest.txt"

# Yield to premarket_scanner's once-a-day, time-critical run (2026-07-09 fix):
# this scanner cycles every 2-5min all session and can skip a single tick for
# free, but premarket_scanner only gets one shot and was being starved by this
# scanner running continuously right through its 9:20 ET window.
NOW_ET=$(TZ=America/New_York date +%H:%M)
if [[ "$NOW_ET" > "09:17" && "$NOW_ET" < "09:23" ]]; then
  exit 0
fi

# Cron is expressed in UTC and intentionally spans both daylight- and standard-time
# market hours. Gate in New York time before importing Python so the unused half of that
# UTC window does not launch a scanner every few minutes or flood closed-market logs.
if [[ "$NOW_ET" < "09:30" || "$NOW_ET" > "16:10" ]]; then
  exit 0
fi

# 2026-07-05 audit: no wrapper-level overlap guard existed. The script's own internal
# fcntl lock (mean_reversion_scanner.py) BLOCKS a second full-scan process rather than
# skipping it, so a slow run could otherwise leave two python processes (each with
# pandas/numpy already loaded) queued on a single-core box. Non-blocking here so the
# later tick just skips instead of piling up a second process.
exec 9>/tmp/mean_reversion_scanner.lock
flock -n 9 || exit 0

if [ -f "$ROOT/venv/bin/activate" ]; then
  # shellcheck source=/dev/null
  source "$ROOT/venv/bin/activate"
fi

"$ROOT/venv/bin/python" "$ROOT/mean_reversion_scanner.py" --once > "$OUT" 2>&1
EXIT=$?

if [ "$EXIT" -ne 0 ]; then
  openclaw message send --channel telegram --target 7590346809 \
    --message "Mean reversion scanner failed (exit $EXIT). Check $OUT on the server." >/dev/null 2>&1
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

# Paper-trade logger: open new triggers + evaluate/close open positions.
"$ROOT/venv/bin/python" "$ROOT/paper_eval.py" >> "$OUT" 2>&1

exit 0
