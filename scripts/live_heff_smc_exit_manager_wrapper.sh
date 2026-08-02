#!/bin/bash
cd /home/heff/.openclaw/workspace || exit 1
exec 9>/tmp/live_heff_smc_exit_manager.lock
flock -n 9 || exit 0
# DISARMED 2026-07-31: the legacy manager bypasses the repaired durable SMC
# lifecycle and still contains the old retry/indicative-quote behavior. There
# are no QQQ positions or open orders at containment time. Keep observation
# output, but never let this legacy cron path submit an order.
./venv/bin/python live_heff_smc_exit_manager.py >> logs/live_heff_smc_exit_manager_run.log 2>&1
