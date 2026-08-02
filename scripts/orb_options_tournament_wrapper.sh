#!/bin/bash
# Live options tournament tick, paired with orb_scanner.py's / mean_reversion_scanner.py's
# cadence. Per trigger: scans new ORB triggers -> options_orchestrator.run_tournament (builds
# S1-S10 ORB arms -> EV filter -> MILP gate 3-total/2-per-side/$100 cap -> Thompson-sample pick ->
# submit PAPER + ledger record), THEN scans new MR triggers the same way (mr_tournament_bridge.py,
# ADDED 2026-07-05 to close a gap where the 5 MR-tagged strategies S4/S5/S6/S8/S10 had never once
# received a trigger since nothing read data/mr_triggers_<date>.jsonl -- run_tournament itself was
# always signal-agnostic). Both bridges use independent processed-trade-id tracking files so a
# bug in one can't cross-contaminate the other's idempotency state. Then runs the synthetic-IOC
# exit sweep over ALL open option positions (TP/SL on live quotes), signal-agnostic regardless of
# whether a trade came from ORB or MR.
# Real money untouched -- everything here is --arm (paper), never --live.
cd /home/heff/.openclaw/workspace || exit 1
exec 9>/tmp/orb_options_tournament.lock
flock -n 9 || exit 0
./venv/bin/python -u orb_tournament_bridge.py --arm >> data/orb_tournament.log 2>&1
./venv/bin/python -u mr_tournament_bridge.py --arm >> data/orb_tournament.log 2>&1
# --reconcile ADDED 2026-06-29: the PENDING->OPEN entry-fill transition. record_open writes a
# trade as PENDING; process_exits (the --exits sweep below) only manages OPEN/PARTIAL_CLOSE. This
# step was NEVER wired into the tick, so every trade stayed stuck at PENDING forever and the exit
# engine silently ignored it (found 2026-06-29 with 2 live spreads sitting unmanaged, one already
# past its stop). Must run BETWEEN entry (bridge) and exit sweep so a fill is adopted same-tick.
./venv/bin/python -u options_eval.py --reconcile >> data/orb_tournament.log 2>&1
# --check-assignments ADDED 2026-07-02: reconcile_expirations() (the EOD 18:30 ET job) can only
# ever catch assignment on legs near their OWN expiry -- a short leg assigned WEEKS before its
# expiry never enters that check at all, no matter how often it's run. This runs every tick
# instead (any open/partial trade, no expiry-proximity gate) so a freshly-assigned stock position
# gets liquidated same-tick rather than sitting unmanaged -- and miscounted by the equity book,
# see alpaca_executor.py's _assignment_pending_symbols() -- for up to a day. Must run BEFORE the
# exit sweep so an assigned trade is already resolved before process_exits tries to manage it.
./venv/bin/python -u options_eval.py --check-assignments --arm >> data/orb_tournament.log 2>&1
./venv/bin/python -u options_orchestrator.py --exits --arm >> data/orb_tournament.log 2>&1
