# Signal-Quality Research Milestone

This milestone keeps the canonical MR and ORB intents frozen and tests whether
point-in-time signal-quality features can isolate a durable subset. All candidates use
the cleaner next-five-minute-bar-open/EOD execution reference, pessimistic stop-first
ordering, and 6/12/20/30 bp friction.

## Data boundary

Only seven true premarket snapshots are available, which is not a valid historical
sample. The replay therefore uses prior regular-session close to current regular-session
open as an overnight-gap proxy. It does not claim to test premarket volume or liquidity.

Market breadth is the fraction of cached symbols above their same-day opening price at
the signal timestamp. Sector breadth applies the same rule to mapped sector members and
requires at least three observations. Both are computed using only data available when
the signal closes.

## MR hypotheses

- close reclaim beyond the frozen trigger boundary;
- aligned signal-bar close location of at least 70%;
- exhaustion score: at least two of |z| >= 2, RSI <= 25/>=75, and |VWAP deviation| >= 2%
  observed in the prior 12 bars;
- signal volume at least 1.25 times the prior-20-bar median;
- breadth washout and five-minute turn: <=40% then +3 points for longs, symmetric for shorts;
- one combined candidate using reclaim, location, exhaustion, and breadth turn.

The stricter three-of-three exhaustion candidate is a robustness check and is excluded
from walk-forward selection.

## ORB hypotheses

- direction-aligned overnight gaps;
- direction-aligned first-15-minute opening-drive efficiency;
- market or sector breadth confirmation at 55%;
- gap plus drive, and one combined gap/drive/market/sector candidate.

Gap thresholds of 0.2% and 0.5%, plus a 0.3 opening-drive threshold, are neighborhood
checks excluded from walk-forward selection. The primary gap and drive thresholds are
0.3% and 0.5%.

## Gate

The same anchored folds apply: 2024 trains for 2025, then 2024-2025 trains for 2026.
Selection uses only primary candidates and maximizes the training 6 bp daily-block-
bootstrap lower bound with at least 200 intents and 60 days. Passage requires positive
means in both test years, at least 150 combined OOS days, and a positive combined lower
95% bound. Discovery contamination still forces a future forward sample before any live
promotion.
