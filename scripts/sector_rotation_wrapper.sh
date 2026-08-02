#!/usr/bin/env bash
set -u
export HOME="/home/heff"
export PATH="/home/heff/.openclaw/workspace/venv/bin:$PATH"
ROOT="/home/heff/.openclaw/workspace"
LOGDIR="$ROOT/logs"
mkdir -p "$LOGDIR"
OUT="$LOGDIR/sector_rotation_wrapper.log"

# Cheap (12 tickers, daily bars only) -- refreshes data/sector_rotation.csv so the live
# sector-hot tag on ORB/MR/premarket/continuation triggers doesn't go stale. Does NOT
# rebuild data/sector_map.json (ticker->ETF assignments barely change; refresh that by
# hand with `sector_rotation.py --build-map` occasionally, not worth a daily cron).
"$ROOT/venv/bin/python" "$ROOT/sector_rotation.py" --build-etf-bars --build-signal > "$OUT" 2>&1
exit 0
