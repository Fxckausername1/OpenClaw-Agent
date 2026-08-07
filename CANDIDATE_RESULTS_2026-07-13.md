# Candidate Results — 2026-07-13

Run: `candidate_20260713T033739Z`

The 17-member candidate family tested whether correcting adverse fill selection could
recover the frozen MR or ORB signals. All candidates used cached five-minute bars,
pessimistic stop-first ordering, and 6/12/20/30 bp friction converted into R using the
actual stop distance.

## Full-sample result at 6 bp

| Strategy | Frozen boundary limit | Best broad candidate | Bootstrap verdict |
|---|---:|---:|---|
| MR | -0.167R | next-open/EOD: -0.062R | inconclusive, CI [-0.128R, +0.007R] |
| ORB | -0.173R | next-open/EOD: -0.057R | inconclusive, CI [-0.163R, +0.049R] |

Next-open entry removed a meaningful portion of the loss, confirming adverse selection
in the delayed boundary limit. It did not create positive expectancy. Fixed 1R/1.5R
targets, gap guards, risk floors, time filters, and prior-day SPY regime filters were
also negative overall.

## Anchored historical walk-forward

Selection used training data only: the maximum daily-block-bootstrap lower 95% bound at
6 bp among candidates with at least 200 intents and 60 days.

| Strategy | 2024 train → 2025 test | 2024-25 train → 2026 test | Combined selected OOS |
|---|---:|---:|---:|
| MR | -0.090R | -0.034R | -0.060R, 367 days, CI [-0.128R, +0.014R] |
| ORB | -0.143R | -0.042R | -0.121R, 312 days, CI [-0.189R, -0.052R] |

Both promotion gates failed. MR was inconclusive but negative in both test folds; ORB
was negative-confirmed in combined out-of-sample evidence. The immutable registry entry
is `exp_20260713T034211Z_2169bf48`.

## Decision

Reject this execution candidate family. Do not change live behavior.

The next research family should change signal quality rather than execution mechanics:

1. condition MR on cross-sectional market breadth and an explicit exhaustion/reclaim;
2. condition ORB on premarket gap, opening-drive quality, and market/sector confirmation;
3. add point-in-time universe membership before interpreting any positive survivor;
4. keep next-open/EOD as the cleaner execution reference, but require a genuinely future
   forward sample before any live promotion.
