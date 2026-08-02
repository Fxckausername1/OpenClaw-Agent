#!/bin/bash
# Nightly continuous strategy search -- post-close, after the options nightly pipeline
# (which finishes ~23:10-23:20 UTC most nights). Independent of options data; pure equity
# wf_cache. Wide/loosened parameter grid (see continuous_search.py); the ONE thing kept
# strict is the locked 75/25 holdout carry check, same bar used everywhere else in this
# project. Incremental: each run only tests configs not already in the ledger, so this
# can run every night indefinitely without re-paying for old configs.
#
# promote_champion.py (added 2026-06-28): closes the loop -- if tonight's search produced
# a new holdout-confirmed champion, push it into data/live_params.json so the actually-live
# mean_reversion_scanner.py/orb_scanner.py pick it up. No-ops if the champion didn't change.
cd /home/heff/.openclaw/workspace || exit 1
exec 9>/tmp/continuous_search.lock
flock -n 9 || { echo "$(date -u) continuous_search already running, skip"; exit 0; }
# Fixed filename, truncated each run (2026-06-28: was >> append-forever into data/continuous_search.log,
# unbounded). Durable rotated/retained record now lives in logs/continuous_search.log via loguru inside
# continuous_search.py itself; this catch-all also picks up promote_champion.py's stdout (not on loguru).
./venv/bin/python -u continuous_search.py --budget-configs 25 > logs/continuous_search_wrapper.log 2>&1
# promote_champion.py DISABLED 2026-06-29 (heff's call, Phase 2 execution-safety lockdown):
# the equity universe just cut over from the bottom-up curated-99/wide500k builds to a
# top-down S&P-500-index-membership universe (see wide_universe.build_sp500_universe()) --
# continuous_search.py's ledger/champion/wf_comp were archived and reset for this cutover,
# so the next several nightly runs are this universe's FIRST passes, with zero track record
# yet of carrying cleanly. Auto-pushing a "carried" champion straight to live_params.json
# with no human review is too risky on a brand-new, unvalidated universe -- re-enable once a
# few nightly runs have been reviewed and the search is trusted again on this universe.
# ./venv/bin/python -u promote_champion.py >> logs/continuous_search_wrapper.log 2>&1
