#!/usr/bin/env bash
set -u
export HOME="/home/heff"
export PATH="/home/heff/.openclaw/workspace/venv/bin:$PATH"
ROOT="/home/heff/.openclaw/workspace"
LOGDIR="$ROOT/logs"
mkdir -p "$LOGDIR"
OUT="$LOGDIR/rader_watchlist_alert.log"

# Same-day Telegram alert for today's Rader Report watchlist (ARM/NVDA/NOW/MSFT),
# heff's explicit ask 2026-07-07. I/O-bound (one batched Alpaca snapshot call),
# not a "heavy job" by this box's single-core rule. Self-expires via the script's
# own WATCHLIST_DATE check; the cron's day-of-month/month fields below are a second,
# independent expiry so this never needs manual cleanup.
exec 9>/tmp/rader_watchlist_alert.lock
flock -n 9 || exit 0

"$ROOT/venv/bin/python" "$ROOT/rader_watchlist_alert.py" >> "$OUT" 2>&1
exit 0
