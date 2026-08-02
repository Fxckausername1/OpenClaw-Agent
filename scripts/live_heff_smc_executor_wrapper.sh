#!/bin/bash
cd /home/heff/.openclaw/workspace || exit 1
exec 9>/tmp/live_heff_smc_executor.lock
flock -n 9 || exit 0
# ============================================================================
# DISARMED 2026-07-31 (P0 containment, heffs explicit instruction).
#
# `--arm` deliberately REMOVED. This wrapper now runs the executor in DRY RUN:
# it still consumes selector decisions and logs exactly what it WOULD submit,
# so shadow signal collection and latency telemetry keep flowing, but it
# places NO broker orders.
#
# Root cause for the disarm (2026-07-31 real paper losses, -$101 over 10
# trades): two real defects, plus systemic issues under repair --
#   1. open_positions.json was keyed by OCC, so 3 signals on the same contract
#      silently overwrote each others tracking records, orphaning a real open
#      position that then sat unmanaged at -57.6% until caught by hand.
#   2. Urgent (STOP) exits priced a limit exactly at a LAGGING indicative bid,
#      so in a fast move the order rested unfilled and chased the market down
#      for 10 minutes, realising ~3x the intended -20% stop.
#
# DO NOT re-add `--arm` until the P0 repair is complete AND heff has explicitly
# approved a canary validation run. Re-arming is a deliberate, approved act.
# ============================================================================
./venv/bin/python live_heff_smc_executor.py >> logs/live_heff_smc_executor_run.log 2>&1
