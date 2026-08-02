#!/bin/bash
# ONE-SHOT: VIX term-structure walkforward backtest, run after close, then self-delete.
cd /home/heff/.openclaw/workspace
./venv/bin/python walkforward_search.py > data/vix_wf_run.log 2>&1
# remove our own cron line so this never recurs
crontab -l 2>/dev/null | grep -v 'vix_oneshot.sh' | crontab -
