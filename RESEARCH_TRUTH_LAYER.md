# Research Truth Layer

This layer is intentionally disconnected from live trading. Nothing here is imported by
the scanners, executor, dashboard snapshot, or cron.

## Baseline

- Freeze: `freeze_20260712T235744Z`
- Existing 2024-2026 history is classified as development data because its holdout has
  informed repeated strategy decisions.
- Forward observations beginning 2026-07-13 are candidates for the new frozen cohort.
- No parameter changes should be made during the parity rebuild.

## Commands

Create a new immutable system manifest:

```bash
./venv/bin/python strategy_freeze.py --label "reason for freeze"
```

Append one experiment before running it:

```bash
./venv/bin/python research_registry.py add \
  --name "experiment name" \
  --hypothesis "falsifiable statement" \
  --strategy MR \
  --stage planned \
  --parameters '{}' \
  --data '{}' \
  --execution-model '{}' \
  --cost-model '{}'
```

Validate the immutable registry:

```bash
./venv/bin/python research_registry.py verify
```

Rebuild the research-only intent ledger from existing runtime logs:

```bash
./venv/bin/python build_intent_ledger.py
```

## Data products

- `data/research/experiment_registry.jsonl`: append-only experiment history
- `data/research/freezes/`: immutable strategy/config manifests
- `data/research/intent_ledgers/`: timestamped, reproducible signal-to-fill snapshots

## Required workflow

1. Register a falsifiable hypothesis before running it.
2. Record every candidate, including failures.
3. Never overwrite or delete an experiment record.
4. Keep development, rolling test, sealed OOS, and forward-paper results distinct.
5. Do not promote a parameter based on the forward cohort being used to judge it.
6. Treat strategy, execution, portfolio selection, and risk overlays as separate experiments.

## Next engineering milestone

Build a parity harness that replays historical symbol-days through the exact live decision
functions and compares trigger time, entry order, fill/no-fill, stop, target, and exit against
the event-driven simulator. Live strategy behavior remains frozen until parity is proven.
