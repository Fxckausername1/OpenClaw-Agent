#!/usr/bin/env bash
# ONE-SHOT (self-removing): post-close standalone holdout backtest of gen_close_drift,
# the never-before-tested MOC/closing-hour edge (built orthogonal-by-design to ORB/mean-rev).
# Scheduled 30min after regime_probe_oneshot.sh so the two research jobs run sequentially,
# not concurrently, on this single-core box. Defensively waits for BOTH the standard live
# scanners AND the regime probe (by script name) to be clear before starting.
set -u
export HOME="/home/heff"
ROOT="/home/heff/.openclaw/workspace"
LOG="$ROOT/logs/close_drift_probe.log"
MARKER="close_drift_probe_oneshot.sh"

[ "$(date -u +%Y-%m-%d)" = "2026-07-08" ] || exit 0

exec 9>/tmp/close_drift_probe_oneshot.lock
flock -n 9 || exit 0

for i in $(seq 1 42); do
  if pgrep -f "mean_reversion_scanner.py|orb_scanner.py|continuous_search.py|paper_eval.py|alpaca_executor.py|run_tournament|regime_holdout_probe.py|backtest_close_drift.py|backtest_gex_regime.py|backtest_vwap_rev.py|backtest_vol_reversion.py|backtest_overnight_gap.py|backtest_microstructure.py|backtest_earnings_reversion.py|backtest_charm_regime.py|backtest_exit_holdmode.py" >/dev/null 2>&1; then
    sleep 10
  else
    break
  fi
done

echo "==== close_drift holdout probe $(date -u) ====" >> "$LOG"
cd "$ROOT" || exit 1
nice -n 19 ionice -c3 ./venv/bin/python $ROOT/backtest_close_drift.py >> "$LOG" 2>&1
RC=$?

SUMMARY=$(grep -A1 "^  close_drift:" "$LOG" | tail -20)
openclaw message send --channel telegram --target 7590346809 \
  --message "close_drift holdout probe done (rc=$RC):
$SUMMARY" >/dev/null 2>&1

crontab -l 2>/dev/null | grep -vF "/scripts/$MARKER " | crontab -
echo "==== done rc=$RC, cron line removed $(date -u) ====" >> "$LOG"
