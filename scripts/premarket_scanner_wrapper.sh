#!/usr/bin/env bash
set -u
export HOME="/home/heff"
export PATH="/home/heff/.openclaw/workspace/venv/bin:$PATH"
ROOT="/home/heff/.openclaw/workspace"
LOGDIR="$ROOT/logs"
mkdir -p "$LOGDIR"
OUT="$LOGDIR/premarket_scanner_wrapper.log"
TODAY=$(TZ=America/New_York date +%Y-%m-%d)
OUTFILE="$ROOT/data/orb_premarket_${TODAY}.json"

# Already ran today (manually or via this cron) -- no-op. Also doubles as the
# DST-safety mechanism: this wrapper is scheduled at both the EDT and EST UTC
# equivalents of ~9:19 ET every day, and whichever one is "real" for the
# current season runs first and creates today's file; the other season's tick
# finds it already there and skips.
if [ -f "$OUTFILE" ]; then
  echo "$(date -u) already have $OUTFILE, skipping" >> "$OUT"
  exit 0
fi

# Full-universe fetch is a genuinely heavy job on this single-core box -- refuse
# to start if mean-reversion or ORB's own heavy scans are already mid-run, since
# two CPU-heavy jobs at once has caused real box lockups before.
#
# 2026-07-09 fix: orb_scanner.py/mean_reversion_scanner.py actually cycle every
# 2-5min continuously all session (not just "at :20" as the old comment assumed),
# so a single immediate pgrep check + bailing to the next cron minute was
# starving this entirely some days (both scanners can plausibly overlap for
# several minutes straight). Now polls for up to ~50s within THIS tick before
# giving up -- cheap (just sleeping, no CPU), and catches most transient
# overlaps without needing a lucky cron minute. The cron window itself was also
# widened (20-40 instead of 20-25) as a second layer of retry budget.
for i in 1 2 3 4 5; do
  if ! pgrep -f 'mean_reversion_scanner.py|orb_scanner.py' >/dev/null 2>&1; then
    break
  fi
  if [ "$i" -eq 5 ]; then
    echo "$(date -u) another heavy scanner still running after ${i}x10s polls, deferring to next tick" >> "$OUT"
    exit 0
  fi
  sleep 10
done

echo "$(date -u) running premarket_scanner.py" >> "$OUT"
"$ROOT/venv/bin/python" "$ROOT/premarket_scanner.py" >> "$OUT" 2>&1
EXIT=$?
if [ "$EXIT" -ne 0 ]; then
  openclaw message send --channel telegram --target 7590346809 \
    --message "Premarket scanner failed (exit $EXIT). Check $OUT." >/dev/null 2>&1
fi
exit $EXIT
