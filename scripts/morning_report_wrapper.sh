#!/usr/bin/env bash
set -u
WS="$HOME/.openclaw/workspace"; cd "$WS" || exit 1
"$WS/venv/bin/python" morning_report.py >> "$WS/logs/morning_report.log" 2>&1
