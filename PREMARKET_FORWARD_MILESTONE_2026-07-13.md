# Premarket Forward Evaluation Milestone — 2026-07-13

## Outcome

The bot now has an automated research-only engine that answers two separate questions:

1. How many valid scored market days and candidate observations have been collected?
2. Has any pre-registered ORB premarket candidate passed every promotion gate?

It does not change signals, place orders, alter the dashboard publisher, or touch Netlify.

## First-capture quality finding

The first sealed 09:15 ET capture succeeded under protocol v3 with 269 records across 192
symbols. Its audit exposed an important limitation in the free live IEX feed:

- snapshots existed for all 192 requested symbols;
- only 32 of 180 stocks had any IEX premarket minute bar;
- only 6 stocks had enough bars for a late-30-minute return;
- only 117 stocks had a valid IEX spread.

That coverage could not satisfy the already-locked 90% quote and 10% missingness gates.
Leaving it unchanged would have spent months collecting evidence that could never pass.

## Corrective protocol locked before scoring

Protocol v4 keeps live IEX decision context and adds data available before the open:

- delayed-SIP snapshots for consolidated trade and quote coverage;
- historical SIP minute bars ending 16 minutes before capture;
- mandatory SPY and 11 sector-ETF references;
- a full-session SIP archive after the close for deterministic outcomes.

A full-universe delayed-SIP snapshot probe returned valid quotes for 180/180 stocks. A
two-symbol bar probe returned 555 premarket bars for AAPL and SPY. The July 13 v3 capture
remains immutable initialization evidence but is excluded from the homogeneous scored
sample. The official scored sample begins July 14.

## What is scored

The evaluator rebuilds the canonical close-confirmed ORB event from sealed post-close
one-minute SIP bars, aggregates them to five-minute bars, and applies next-bar-open/EOD
execution with stop-first ambiguity and 6/12 bp costs.

The pre-registered family measures:

- direction-aligned gaps of 0.3% and 0.5%;
- gap plus aligned premarket VWAP/late-session structure and a 15 bp spread cap;
- gap plus 1.5x premarket relative volume after a 20-day baseline exists;
- gap plus SPY and sector confirmation;
- a combined candidate requiring every condition.

Every threshold is fixed in `PREMARKET_FORWARD_EVALUATION.md`. Future changes require a
new protocol version and new disjoint sample.

## When enough data exists

Starting with the July 14 scored session, the planned checkpoints are approximately:

- 20 scored days: August 10, 2026 — data-quality review only;
- 60 scored days: October 6, 2026 - preliminary stability review;
- 120 scored days: approximately December 31, 2026 — day gate can pass;
- 250 eligible observations for the same candidate — observation gate can pass.

After 20 days, the status engine calculates each candidate's actual observations per
day and projects its separate 250-observation date. The true earliest review date is the
later of that candidate's day-gate and observation-gate dates.

Even after both sample gates are met, promotion remains blocked unless both chronological
halves are positive at 6 bp, the daily bootstrap lower bound is positive at 6 bp, mean
expectancy is nonnegative at 12 bp, quote coverage is at least 90%, and every required
feature has no more than 10% missingness.

## Automated status

After each weekday post-close SIP archive, the wrapper automatically rebuilds and verifies:

- `data/research/premarket_forward/evaluation/status.json`
- `data/research/premarket_forward/evaluation/STATUS.txt`
- `data/research/premarket_forward/evaluation/observations.jsonl`

The current status is correctly `0/120` because no protocol-v4 capture/outcome pair has
completed yet. Observation-date projection intentionally remains unavailable until the
20-day arrival rate is known.
