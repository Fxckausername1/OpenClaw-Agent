#!/bin/bash
cd /home/heff/.openclaw/workspace || exit 1
exec 9>/tmp/live_heff_smc_detector.lock
flock -n 9 || exit 0
./venv/bin/python live_heff_smc_detector.py >> logs/live_heff_smc_detector_run.log 2>&1
