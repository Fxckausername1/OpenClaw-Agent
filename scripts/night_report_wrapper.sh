#!/usr/bin/env bash
set -u
WS="$HOME/.openclaw/workspace"; cd "$WS" || exit 1
"$WS/venv/bin/python" night_report.py >> "$WS/logs/night_report.log" 2>&1
