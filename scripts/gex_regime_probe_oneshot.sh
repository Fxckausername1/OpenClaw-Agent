#!/usr/bin/env bash
# ONE-SHOT (self-removing): first-ever historical test of gating MR/ORB by REAL net_gex
# regime (AMD/BAC/CSCO/F/INTC/PFE, the 6 names with already-paid-for daily options history).
# Scheduled last of tonight's 3 research jobs (regime_ok, close_drift, this one) so all run
# sequentially on this single-core box, never concurrently. Waits for all 3 job-names to be
# clear before starting.
set -u
export HOME="/home/heff"
ROOT="/home/heff/.openclaw/workspace"
LOG="$ROOT/logs/gex_regime_probe.log"
MARKER="gex_regime_probe_oneshot.sh"

[ "$(date -u +%Y-%m-%d)" = "2026-07-08" ] || exit 0

exec 9>/tmp/gex_regime_probe_oneshot.lock
flock -n 9 || exit 0

# SHARED cross-wrapper lock (2026-07-08 fix, retrofitted -- this wrapper was mid-flight
# during the original patch pass and got skipped, then failed silently on its first retry).
exec 8>/tmp/research_chain_shared.lock
flock 8

for i in $(seq 1 60); do
  if pgrep -f "mean_reversion_scanner.py|orb_scanner.py|continuous_search.py|paper_eval.py|alpaca_executor.py|run_tournament|regime_holdout_probe.py|backtest_close_drift.py|backtest_gex_regime.py|backtest_vwap_rev.py|backtest_vol_reversion.py|backtest_overnight_gap.py|backtest_microstructure.py|backtest_earnings_reversion.py|backtest_charm_regime.py|backtest_exit_holdmode.py" >/dev/null 2>&1; then
    sleep 10
  else
    break
  fi
done

echo "==== gex_regime holdout probe $(date -u) ====" >> "$LOG"
cd "$ROOT" || exit 1
nice -n 19 ionice -c3 ./venv/bin/python $ROOT/backtest_gex_regime.py >> "$LOG" 2>&1
RC=$?

SUMMARY=$(grep -A1 "^  MR \|^  ORB " "$LOG" | grep -v "^--$" | tail -24)
openclaw message send --channel telegram --target 7590346809 \
  --message "GEX-regime holdout probe done (rc=$RC):
$SUMMARY" >/dev/null 2>&1

crontab -l 2>/dev/null | grep -vF "/scripts/$MARKER " | crontab -
echo "==== done rc=$RC, cron line removed $(date -u) ====" >> "$LOG"
