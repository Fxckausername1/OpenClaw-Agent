#!/usr/bin/env bash
# AUTONOMOUS Alpaca PAPER executor — one cycle. Self-gates: alpaca_executor refuses to ARM
# while the kill-switch is engaged, and skips when the market is closed. In crontab
# (*/2 13-20 * * 1-5) since the 2026-06-24 autonomous-paper activation; kill-switch is the
# live on/off control, not this script's presence in cron.
set -u
WS="$HOME/.openclaw/workspace"; cd "$WS" || exit 1

# The UTC cron range covers both daylight- and standard-time sessions. Gate locally
# before starting Python, while retaining the post-close window used by EOD flattening.
NOW_ET=$(TZ=America/New_York date +%H:%M)
if [[ "$NOW_ET" < "09:29" || "$NOW_ET" > "16:10" ]]; then
  exit 0
fi
# 2026-07-05 audit: this is the one wrapper that actually PLACES orders and had no overlap
# guard -- a slow Alpaca response on one tick could let the next */2 tick pile up on top of
# it. Non-blocking: if a previous run is still in flight, skip this tick rather than queue.
exec 9>/tmp/alpaca_executor.lock
flock -n 9 || exit 0
# Fixed filename, truncated each run (2026-06-28: was >> append-forever, unbounded single-
# file growth). Durable rotated/retained record now lives in logs/executor.log via loguru
# inside alpaca_executor.py itself.
"$WS/venv/bin/python" alpaca_executor.py --arm > "$WS/logs/alpaca_executor_wrapper.log" 2>&1
