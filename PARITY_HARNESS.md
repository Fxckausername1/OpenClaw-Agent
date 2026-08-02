# Offline Parity Harness

Run:

```bash
./venv/bin/python parity_harness.py
```

The harness has no network or order-submission path. It checks:

- frozen runtime hashes;
- recorded trigger-to-order payload equivalence;
- fill versus never-filled counterfactual outcomes;
- live/backtest cadence, breakout, universe, and entry-bar contracts.

Reports are written to `data/research/parity_reports/`.

## Initial result

Report `parity_20260713T001306Z` found:

- 80/80 recorded order payloads matched their triggers exactly;
- MR live cadence (5 minutes) differs from historical sampling (15 minutes);
- ORB live uses close-confirmed breaks while historical generators use wick touches;
- MR live uses the wide universe while its simple backtest uses the S&P 100;
- entry-bar handling differs across ORB simulators;
- never-filled intents have materially higher counterfactual paper outcomes than filled intents.

The equivalence hypothesis is rejected. Runtime behavior remains frozen until these
contracts are unified and replay-tested.
