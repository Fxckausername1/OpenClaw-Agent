#!/usr/bin/env bash
set -u
export HOME="/home/heff"
export PATH="/home/heff/.openclaw/workspace/venv/bin:$PATH"
ROOT="/home/heff/.openclaw/workspace"
LOGDIR="$ROOT/logs"
mkdir -p "$LOGDIR"
OUT="$LOGDIR/wall_alert_scoring.log"

# Daily wall-proximity-alert accuracy cross-check (2026-07-06, heff's ask): scores
# every wall_proximity_alert.py "WALL ALERT" against real subsequent price action,
# appends new events to data/wall_alert_ledger.jsonl (idempotent -- keyed by
# ticker/date/time/wall_type, safe to re-run), and rewrites
# data/wall_alert_accuracy_summary.json across the FULL accumulated history. Runs
# once daily after close so the 60min lookforward window has real data for even the
# day's last alerts. Own lock -- cheap (a handful of batched bar fetches), not a
# "heavy job" by this box's single-core rule.
exec 9>/tmp/wall_alert_scoring.lock
flock -n 9 || exit 0

"$ROOT/venv/bin/python" "$ROOT/wall_alert_scoring.py" >> "$OUT" 2>&1
exit 0
