#!/usr/bin/env bash
# ONE-SHOT (self-removing): holdout test of gen_charm regime hypothesis (backtest_charm_regime.py,
# built 2026-07-03, never executed until tonight). Part of the 2026-07-08 research chain.
set -u
export HOME="/home/heff"
ROOT="/home/heff/.openclaw/workspace"
LOG="$ROOT/logs/charm_regime_probe.log"
MARKER="charm_regime_probe_oneshot.sh"

[ "$(date -u +%Y-%m-%d)" = "2026-07-08" ] || exit 0
exec 9>/tmp/charm_regime_probe_oneshot.lock
flock -n 9 || exit 0

# SHARED cross-wrapper lock (2026-07-08 fix): the per-script lock above only stops
# THIS script from double-firing against itself -- it does not stop two DIFFERENT
# wrappers from both finishing their busy-poll loop at the same instant and launching
# concurrently (this actually happened tonight: close_drift + a regime_ok retry ran
# simultaneously, load average briefly hit 21). This blocking (non "-n") flock forces
# every wrapper in the chain through a single turnstile -- only one heavy python phase
# runs system-wide at a time, regardless of poll-loop timing coincidences.
exec 8>/tmp/research_chain_shared.lock
flock 8

for i in $(seq 1 120); do
  if pgrep -f "mean_reversion_scanner.py|orb_scanner.py|continuous_search.py|paper_eval.py|alpaca_executor.py|run_tournament|regime_holdout_probe.py|backtest_close_drift.py|backtest_gex_regime.py|backtest_vwap_rev.py|backtest_vol_reversion.py|backtest_overnight_gap.py|backtest_microstructure.py|backtest_earnings_reversion.py|backtest_exit_holdmode.py" >/dev/null 2>&1; then
    sleep 10
  else
    break
  fi
done

echo "==== charm_regime holdout probe $(date -u) ====" >> "$LOG"
cd "$ROOT" || exit 1
nice -n 19 ionice -c3 ./venv/bin/python "$ROOT/backtest_charm_regime.py" >> "$LOG" 2>&1
RC=$?
SUMMARY=$(tail -25 "$LOG")
openclaw message send --channel telegram --target 7590346809 \
  --message "charm_regime holdout probe done (rc=$RC):
$SUMMARY" >/dev/null 2>&1
crontab -l 2>/dev/null | grep -vF "/scripts/$MARKER " | crontab -
echo "==== done rc=$RC, cron line removed $(date -u) ====" >> "$LOG"
