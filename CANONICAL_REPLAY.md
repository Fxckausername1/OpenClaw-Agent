# Canonical Frozen-Live Replay

The canonical replay is an offline research path. It is not imported by scanners, the
executor, dashboard publishing, or cron.

## Contract

- MR evaluates every closed five-minute bar, matching the live full-scan cadence.
- ORB requires a close beyond the 09:30-09:45 opening range.
- ORB applies the current range, VWAP, volume, and sector gates.
- Orders become active on the bar after the signal closes.
- Entries use the deployed boundary-limit mechanic.
- Untouched limits are counted as zero-R intents, not silently removed.
- Same-bar stop/target ambiguity resolves stop-first.
- Results are reported at the intent level under 6/12/20/30bp cost scenarios.

## Commands

```bash
./venv/bin/python -m unittest -v test_canonical_contracts.py
./venv/bin/python canonical_replay.py --symbols "AAPL,MSFT"
./venv/bin/python canonical_analysis.py
```

For the current live universe, pass symbols from `data/wide_universe.json`; do not run the
unfiltered `wf_cache` directory because it contains research-only instruments.

## Frozen development result

Run `canonical_20260713T021006Z` covered 176 current-universe symbols with cached history.

- MR: 10,891 intents / 510 days; gross -0.053R, 6bp net -0.167R, fill rate 86.1%.
- ORB: 1,639 intents / 383 days; gross -0.042R, 6bp net -0.173R, fill rate 84.4%.
- Daily-block-bootstrap 95% mean-R intervals were below zero for both strategies overall.
- MR was negative in 2024, 2025, and 2026 and on both sides.
- ORB was negative in 2024 and 2025; 2026 alone was inconclusive, not positive-confirmed.

The frozen implementation is rejected for live-money promotion. This does not prove that
MR or ORB as strategy families cannot work; it proves the deployed decision/fill contract
does not inherit the legacy backtest claims.

## Known limitations

- Current/static universe rather than point-in-time membership.
- Five-minute OHLCV rather than quote/order-book data.
- Touch-at-limit fills do not model queue position.
- Portfolio admission and cut/choke replacement overlays are not yet applied.
