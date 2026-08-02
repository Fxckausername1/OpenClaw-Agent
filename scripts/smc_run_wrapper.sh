#!/bin/bash
# Consolidated SMC runner -- the ONLY SMC path after 2026-08-01.
#
# Replaces four separate legacy crons (detector / selector / executor /
# exit_manager) that each ran on their own boundary. Measured signal-to-submit
# latency under that design was 227.5-407.3s (median 352.85s) against a backtest
# assuming 3.0s. This runs detection -> selection -> gate -> submit back-to-back
# in one process, then supervises open positions continuously.
#
# ============================ SHADOW MODE ============================
# `--arm` is deliberately ABSENT. This performs one full signal cycle and then
# ~150s of position supervision, logging every decision and the measured latency
# legs (signal->quote, quote->decision, decision->ack) WITHOUT placing any order.
#
# Re-arming is a separate, approved act: add `--arm` here only after a clean
# full-session shadow run has been reviewed. The approved canary envelope is
# 1 contract / 1 concurrent position / $100 daily realized loss cap, set via the
# SMC_* env vars below (already applied so an arming edit does not also have to
# remember them).
# =====================================================================
cd /home/heff/.openclaw/workspace || exit 1
exec 9>/tmp/smc_run.lock
flock -n 9 || exit 0

# Canary risk envelope (heff's explicit choice 2026-08-01). These are read by
# smc.config.load_config(); they only ever TIGHTEN the built-in defaults.
export SMC_MAX_CONCURRENT_POSITIONS=1
export SMC_MAX_ENTRIES_PER_WINDOW=1
export SMC_MAX_DAILY_REALIZED_LOSS=100

# --supervise 150 keeps this tick alive just under the 3-minute cadence so open
# positions are watched at ~1s rather than only once per cron boundary. flock
# above guarantees ticks never overlap.
./venv/bin/python smc_run.py --once --supervise 150 \
    >> logs/smc_run.log 2>&1
