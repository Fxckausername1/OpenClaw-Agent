#!/usr/bin/env bash
# Backtest A (regime-ORB) + B (time-of-day). BACKTEST ONLY, no deploy. Sequential.
set -u
WS="$HOME/.openclaw/workspace"; cd "$WS" || exit 1
mkdir -p logs
PY="$WS/venv/bin/python"; OC="/usr/bin/openclaw"
LOG="$WS/logs/AB_$(date -u +%Y%m%d_%H%M).log"
{
  echo "=== Backtests A+B start $(date -u) ==="
  echo "--- A: walkforward — baseline vs regime-ORB (20/50, 9/21) ---"
  "$PY" walkforward_search.py
  echo; echo "--- B: time-of-day expectancy slicing ---"
  "$PY" timeofday_backtest.py
  echo "=== done $(date -u) ==="
} >> "$LOG" 2>&1
"$OC" message send --channel telegram --target 7590346809 --message "$(printf '🧪 Backtests A+B done (BACKTEST ONLY, nothing deployed).\nTail:\n%s\n\nFull: %s' "$(tail -n 22 "$LOG")" "$LOG")" >/dev/null 2>&1 || true
