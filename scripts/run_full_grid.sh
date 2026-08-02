#!/bin/bash
# One-shot full-grid runner for continuous_search.py. Two properties that matter:
#
# 1) DETACHED: launched via nohup+setsid with stdin/stdout/stderr fully redirected away
#    from any terminal/SSH session, so it survives the laptop closing, sleeping, or losing
#    network entirely. It is reparented to init on the box, independent of any client.
#
# 2) MARKET-HOURS AWARE: continuous_search.py itself now checks the clock before EVERY
#    individual config (not just between batches -- a batch of 50 can take 5+ hours at the
#    observed ~6min/config pace, so a coarser per-batch check could let a run that started
#    just before the open plow through the whole trading day). This wrapper's own check
#    between invocations is now just a cheap first-pass filter.
#
# Incremental: continuous_search.py already skips anything in the ledger, so stopping and
# restarting this (or letting the nightly cron also fire) never re-does work or corrupts
# anything -- worst case is a few redundant minutes if both happen to race on the same config.
cd /home/heff/.openclaw/workspace || exit 1
exec 9>/tmp/full_grid_run.lock
flock -n 9 || { echo "$(date -u) full grid run already in progress, exiting"; exit 0; }
LOG=data/full_grid_run.log
{
echo "=== full grid run started $(date -u) ==="
while true; do
  hour=$(date -u +%H)
  dow=$(date -u +%u)   # 1=Mon .. 7=Sun
  if [ "$dow" -le 5 ] && [ "$hour" -ge 13 ] && [ "$hour" -lt 21 ]; then
    echo "$(date -u) market hours -- pausing 15min"
    sleep 900
    continue
  fi
  out=$(./venv/bin/python -u continuous_search.py --budget-configs 50 2>&1)
  echo "$out"
  if echo "$out" | grep -q "grid exhausted"; then
    echo "=== full grid run COMPLETE $(date -u) ==="
    break
  fi
done
} >> "$LOG" 2>&1
