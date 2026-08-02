#!/usr/bin/env bash
set -u
export HOME="/home/heff"
export PATH="/home/heff/.openclaw/workspace/venv/bin:$PATH"
ROOT="/home/heff/.openclaw/workspace"
LOGDIR="$ROOT/logs"
mkdir -p "$LOGDIR"
TS=$(date -u +"%Y%m%d_%H%M%S")
OUT="$LOGDIR/earnings_cache_${TS}.log"
"$ROOT/venv/bin/python" "$ROOT/build_earnings_cache.py" > "$OUT" 2>&1
exit $?
