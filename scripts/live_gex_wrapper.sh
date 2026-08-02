#!/usr/bin/env bash
set -u
export HOME="/home/heff"
export PATH="/home/heff/.openclaw/workspace/venv/bin:$PATH"
ROOT="/home/heff/.openclaw/workspace"
LOGDIR="$ROOT/logs"
mkdir -p "$LOGDIR"
OUT="$LOGDIR/live_gex_wrapper.log"

# Live GEX refresh: Alpaca's free indicative chain + free OI, NOT a paid feed -- see
# live_gex.py's docstring. Expanded 2026-07-03 from 6 tickers to the full ~180-symbol
# wide_universe (heff's direction: maximize forward GEX-history collection for a future
# GEX-regime-vs-ORB backtest), pushing real runtime to ~21min/run. Cron cadence switched
# 2026-07-04 (heff's direction) from every-30min to 3x/day fixed times, swapping schedules
# with the 0dte wrapper below -- OI is T-1-lagged and barely moves intraday anyway, and
# append_history() in live_gex.py only logs the FIRST clean read of each calendar day per
# ticker, so the higher-frequency 0dte read is where the value actually is now. flock still
# guards against a slow run overlapping the next tick regardless.
# Grew to 4x/day 2026-07-06 (heff's ask): added a ~8:45 ET premarket read -- there was no
# premarket coverage before (earliest read was 10:00 ET, 30min after open) despite nothing
# technical blocking it (OI freshness doesn't depend on pull time; the 0dte job already
# proves quotes are live by 9:00 ET). This also means append_history()'s FIRST-clean-read-
# of-the-day now captures a genuinely pre-open snapshot instead of a 10am one.
exec 9>/tmp/live_gex.lock
flock -n 9 || exit 0
"$ROOT/venv/bin/python" "$ROOT/live_gex.py" > "$OUT" 2>&1

# TEMPORARY bridge (2026-07-04): WSS/P(C)/regime data computed above can't be
# seen on the dashboard yet (Netlify freeze until 2026-07-08) -- send a
# compact Telegram digest instead, at this same cadence (now 4x/day). Self-expires
# after 2026-07-08 (see gex_telegram_digest.py's own EXPIRES check) once the
# dashboard push makes it redundant -- safe to leave in this wrapper
# permanently, it just no-ops past that date.
"$ROOT/venv/bin/python" "$ROOT/gex_telegram_digest.py" >> "$OUT" 2>&1
exit 0
