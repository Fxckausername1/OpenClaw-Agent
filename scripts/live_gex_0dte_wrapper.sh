#!/usr/bin/env bash
set -u
export HOME="/home/heff"
export PATH="/home/heff/.openclaw/workspace/venv/bin:$PATH"
ROOT="/home/heff/.openclaw/workspace"
LOGDIR="$ROOT/logs"
mkdir -p "$LOGDIR"
OUT="$LOGDIR/live_gex_0dte_wrapper.log"

# 0DTE GEX refresh (2026-07-03, heff's direction -- he day-trades SPY 0DTEs and needs the
# gamma picture for contracts expiring TODAY, not the ~30 DTE monthly proxy the main
# live_gex_wrapper.sh cron computes). Completely independent of that wrapper: own lock, own
# output file (data/live_gex_0dte_snapshot.json), own cadence -- so this never waits on the
# ~21min full-universe sweep and never contends with it for a write. Cadence switched
# 2026-07-04 (heff's direction) from 3x/day fixed times to every 30min, swapping schedules
# with the full-sweep wrapper above -- unlike T-1-lagged monthly OI, a 0DTE picture changes
# meaningfully within the trading day and this is meant to inform live position decisions,
# not just build a slow-moving history series. ~13s/run, trivial at this cadence.
exec 9>/tmp/live_gex_0dte.lock
flock -n 9 || exit 0
"$ROOT/venv/bin/python" "$ROOT/live_gex.py" --0dte > "$OUT" 2>&1
exit 0
