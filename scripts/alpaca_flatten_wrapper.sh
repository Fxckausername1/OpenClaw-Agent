#!/usr/bin/env bash
# EOD: flatten open positions (ORB rides to the close). Run ~15:58 ET = 19:58 UTC.
set -u
WS="$HOME/.openclaw/workspace"; cd "$WS" || exit 1
"$WS/venv/bin/python" alpaca_executor.py --arm --flatten >> "$WS/logs/alpaca_flatten.log" 2>&1
