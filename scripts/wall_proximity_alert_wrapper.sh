#!/usr/bin/env bash
set -u
export HOME="/home/heff"
export PATH="/home/heff/.openclaw/workspace/venv/bin:$PATH"
ROOT="/home/heff/.openclaw/workspace"
LOGDIR="$ROOT/logs"
mkdir -p "$LOGDIR"
OUT="$LOGDIR/wall_proximity_alert.log"

# Wall-proximity Telegram alert (2026-07-04, heff's explicit ask): checks live price
# vs. call/put walls (data/live_gex_snapshot.json, refreshed 3x/day by
# live_gex_wrapper.sh) for the full tracked universe every 5min during RTH. Own
# script is I/O-bound (one batched Alpaca snapshot call), not a "heavy job" by this
# box's single-core rule -- only guards against overlapping ITSELF (flock), not
# against the scanner/GEX jobs the way premarket_scanner.py does.
exec 9>/tmp/wall_proximity_alert.lock
flock -n 9 || exit 0

"$ROOT/venv/bin/python" "$ROOT/wall_proximity_alert.py" >> "$OUT" 2>&1
exit 0
