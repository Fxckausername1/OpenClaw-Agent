#!/usr/bin/env bash
set -u
export HOME="/home/heff"
export PATH="/home/heff/.openclaw/workspace/venv/bin:$PATH"
ROOT="/home/heff/.openclaw/workspace"
LOGDIR="$ROOT/logs"
mkdir -p "$LOGDIR"
OUT="$LOGDIR/uw_confluence_refresh_wrapper.log"

# Daily post-close UW data refresh + confluence score (2026-07-04) -- feeds
# data/confluence_score.json, which dashboard_snapshot.py picks up, AND sends
# a Telegram confluence alert via catalyst_alert.py (added as the final step
# once this pipeline itself was scheduled -- catalyst_alert.py was built
# earlier the same day but deliberately left unscheduled until its data
# dependency had a real cron). catalyst_news_pull.py added same day, later
# session -- free EDGAR/openFDA "why did this move" attribution pull + the
# confidence-tag cross-reference (data/confluence_with_conviction.json),
# runs right after confluence_score.py since both the Federal Register
# scoping and the conviction tagging depend on that day's confluence output.
# Runs 21:30 UTC weekdays: after the RTH scanner window (13-21 UTC) closes,
# well before continuous_search's 23:50 UTC CPU-heavy backtest grid. Whole
# sequence takes ~10-11min (mostly network-bound API calls, not CPU), so
# still guards against colliding with a slow-running heavy job per the
# box's single-core "never run 2 heavy jobs concurrently" rule.
exec 9>/tmp/uw_confluence_refresh.lock
flock -n 9 || exit 0

if pgrep -f 'mean_reversion_scanner.py|orb_scanner.py|continuous_search.py' >/dev/null 2>&1; then
  echo "$(date -u) another heavy job is running, deferring" >> "$OUT"
  exit 0
fi

echo "$(date -u) starting UW refresh + confluence score" >> "$OUT"
cd "$ROOT" || exit 1

for script in uw_historical_pull.py pull_sweep_alerts.py pull_iv_rank.py pull_short_interest.py confluence_score.py catalyst_news_pull.py catalyst_alert.py; do
  echo "$(date -u) running $script" >> "$OUT"
  "$ROOT/venv/bin/python" "$ROOT/$script" >> "$OUT" 2>&1
  EXIT=$?
  if [ "$EXIT" -ne 0 ]; then
    echo "$(date -u) $script failed (exit $EXIT)" >> "$OUT"
    openclaw message send --channel telegram --target 7590346809 \
      --message "UW confluence refresh: $script failed (exit $EXIT). Check $OUT." >/dev/null 2>&1
    exit $EXIT
  fi
done

echo "$(date -u) UW refresh + confluence score complete" >> "$OUT"
exit 0
