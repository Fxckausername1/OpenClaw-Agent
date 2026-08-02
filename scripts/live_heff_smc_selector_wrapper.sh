#!/bin/bash
cd /home/heff/.openclaw/workspace || exit 1
exec 9>/tmp/live_heff_smc_selector.lock
flock -n 9 || exit 0
./venv/bin/python live_heff_smc_selector.py >> logs/live_heff_smc_selector_run.log 2>&1
