#!/usr/bin/env bash
# ONE-SHOT: price-based risk-off gate BACKTEST ONLY. No live deploy, no sentiment impl.
# Fires 2026-06-18 21:50 UTC (post-close, after paper EOD). Self-removes from crontab.
set -u
WS="$HOME/.openclaw/workspace"
cd "$WS" || exit 1
mkdir -p logs
PY="$WS/venv/bin/python"
OC="/usr/bin/openclaw"
LOG="$WS/logs/wf_riskoff_$(date -u +%Y%m%d_%H%M).log"
# one-shot self-clean: drop this job from crontab so it never repeats
crontab -l 2>/dev/null | grep -v 'riskoff_backtest_wrapper.sh' | crontab - 2>/dev/null || true
{
  echo "=== risk-off PRICE-gate backtest (BACKTEST ONLY) start $(date -u) ==="
  echo "--- 1) build market proxy from wf_cache ---"
  "$PY" walkforward_search.py --build-proxy
  echo "--- 2) walkforward: baseline vs risk-off long-veto sweep (locked holdout) ---"
  "$PY" walkforward_search.py
  echo "--- 3) 2026-06-17 case replay ---"
  "$PY" validate_riskoff_20260617.py
  echo "=== done $(date -u) ==="
} >> "$LOG" 2>&1
"$OC" message send --channel telegram --target 7590346809 --message "$(printf '🧪 Risk-off PRICE-gate backtest done (BACKTEST ONLY — nothing deployed).\n\n6/17 replay tail:\n%s\n\nFull log: %s' "$(tail -n 16 "$LOG")" "$LOG")" >/dev/null 2>&1 || true
