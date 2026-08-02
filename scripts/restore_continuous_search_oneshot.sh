#!/usr/bin/env bash
# ONE-SHOT (self-removing): restores continuous_search_wrapper.sh's normal nightly cron,
# which was temporarily removed on 2026-07-08 so tonight's research-probe chain could have
# the single-core box uncontested. This runs 2026-07-09 12:00 UTC (8am ET) -- clear of
# tonight's testing, clear of tomorrow morning's other crons, comfortably before
# continuous_search's own 23:50 UTC fire time tomorrow night.
set -u
MARKER="restore_continuous_search_oneshot.sh"
[ "$(date -u +%Y-%m-%d)" = "2026-07-09" ] || exit 0

LINE="50 23 * * 1-5 /bin/bash /home/heff/.openclaw/workspace/scripts/continuous_search_wrapper.sh  # continuous equity strategy search (post-close, wide grid, incremental, holdout-gated)"

if ! crontab -l 2>/dev/null | grep -qF "continuous_search_wrapper.sh"; then
  (crontab -l 2>/dev/null; echo "$LINE") | crontab -
  echo "$(date -u) restored continuous_search cron" >> /home/heff/.openclaw/workspace/logs/restore_continuous_search.log
else
  echo "$(date -u) continuous_search cron already present, no action" >> /home/heff/.openclaw/workspace/logs/restore_continuous_search.log
fi

crontab -l 2>/dev/null | grep -vF "/scripts/$MARKER " | crontab -
