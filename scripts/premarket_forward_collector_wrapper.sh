#!/usr/bin/env bash
set -euo pipefail

export HOME="/home/heff"
export PATH="/home/heff/.openclaw/workspace/venv/bin:$PATH"
ROOT="/home/heff/.openclaw/workspace"
MODE="${1:-}"
LOG="$ROOT/logs/premarket_forward_collector.log"
EVAL_LOG="$ROOT/logs/premarket_forward_evaluator.log"
NOW_ET="$(TZ=America/New_York date +%H%M)"

case "$MODE" in
  capture)
    if ((10#$NOW_ET < 910 || 10#$NOW_ET >= 920)); then
      exit 0
    fi
    ;;
  sip-backfill)
    if ((10#$NOW_ET < 1625 || 10#$NOW_ET >= 1640)); then
      exit 0
    fi
    ;;
  verify)
    ;;
  *)
    echo "usage: $0 capture|sip-backfill|verify" >&2
    exit 2
    ;;
esac

mkdir -p "$ROOT/logs"
exec 9>"/tmp/premarket_forward_${MODE}.lock"
if ! flock -n 9; then
  echo "$(date -u +%FT%TZ) mode=$MODE skipped: lock busy" >> "$LOG"
  exit 0
fi

echo "$(date -u +%FT%TZ) mode=$MODE start" >> "$LOG"
if "$ROOT/venv/bin/python" "$ROOT/premarket_forward_collector.py" --mode "$MODE" >> "$LOG" 2>&1; then
  if [[ "$MODE" == "sip-backfill" ]]; then
    echo "$(date -u +%FT%TZ) evaluator start" >> "$EVAL_LOG"
    if "$ROOT/venv/bin/python" "$ROOT/premarket_forward_evaluator.py" --mode update >> "$EVAL_LOG" 2>&1 &&
       "$ROOT/venv/bin/python" "$ROOT/premarket_forward_evaluator.py" --mode verify >> "$EVAL_LOG" 2>&1; then
      echo "$(date -u +%FT%TZ) evaluator complete" >> "$EVAL_LOG"
    else
      status=$?
      echo "$(date -u +%FT%TZ) evaluator failed status=$status" >> "$EVAL_LOG"
      exit "$status"
    fi
  fi
  echo "$(date -u +%FT%TZ) mode=$MODE complete" >> "$LOG"
else
  status=$?
  echo "$(date -u +%FT%TZ) mode=$MODE failed status=$status" >> "$LOG"
  exit "$status"
fi
