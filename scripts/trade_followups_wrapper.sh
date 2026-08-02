#!/usr/bin/env bash
# Weekday pre-market: ping Telegram with open trading follow-ups (things to hit Claude for).
set -u
export HOME="/home/heff"
export PATH="/home/heff/.openclaw/workspace/venv/bin:$PATH"
ROOT="/home/heff/.openclaw/workspace"
LOGDIR="$ROOT/logs"
mkdir -p "$LOGDIR"
OUT="$LOGDIR/trade_followups_$(date -u +%Y%m%d).log"
"$ROOT/venv/bin/python" "$ROOT/trade_followups.py" >> "$OUT" 2>&1
exit 0
