# Premarket Forward Evaluation Contract

Version: `premarket-evaluation-2026-07-13.2`

This contract was fixed before the first scored post-close session. It converts sealed
premarket captures into deterministic ORB observations without changing live trading.

## Source separation

- Decision features use only the sealed 09:15 ET combined IEX/delayed-SIP capture.
- Outcomes use only the sealed post-close SIP archive.
- Regular-session bars are never allowed into that day's pre-open feature calculations.
- A day counts only when both manifests verify and the capture uses protocol
  `premarket-forward-2026-07-13.4`.
- The July 10 SIP-only archive and July 13 protocol-v3 IEX-only capture are retained as
  initialization evidence but do not count toward the homogeneous scored sample.

## Fixed premarket features

All calculations use observations available by the capture timestamp.

- Prior close: `prevDailyBar.c` in the sealed Alpaca snapshot.
- Last price: latest sealed IEX trade, falling back to IEX then delayed-SIP bars.
- Overnight gap: `last_price / prior_close - 1`.
- Premarket volume: consolidated SIP minute volume from 04:00 ET through the fixed
  delayed cutoff.
- Premarket VWAP: volume-weighted average of the delayed consolidated SIP bars.
- VWAP position: `last_price / premarket_vwap - 1`.
- Premarket range: `(highest_high - lowest_low) / prior_close`.
- Late available 30-minute return: last close divided by first open during the final 30
  minutes ending at the delayed-SIP cutoff.
- Late range ratio: that 30-minute range divided by the full delayed premarket range.
- Spread: `(ask - bid) / midpoint * 10,000` from the delayed-SIP snapshot quote.
- Relative volume: current premarket volume divided by the median of the same symbol's
  previous 20 sealed captures. It remains unavailable until 20 prior days exist.
- Market confirmation: SPY overnight-gap direction.
- Sector confirmation: overnight-gap direction of the symbol's mapped sector ETF.

SPY and the 11 sector ETFs are mandatory reference instruments in every capture.

## Fixed signal and outcome

The base event is the canonical close-confirmed five-minute ORB contract with the live
opening-range, range-size, VWAP, volume, and price gates. The historical sector-hot gate
is disabled here so point-in-time sector confirmation can be tested explicitly.

Execution uses the cleaner pre-registered reference established by the execution
milestone:

- enter at the next five-minute bar open after the ORB signal;
- stop at the opposite side of the opening range;
- resolve same-bar ambiguity stop-first;
- exit remaining positions at the final regular-session close;
- subtract 6 bp primary and 12 bp stress round-trip costs, converted to R using actual
  stop distance.

An eligible observation is one valid next-open/EOD outcome for one candidate, symbol,
and session. Invalid-gap or missing-outcome events do not count.

## Pre-registered candidate family

`orb_forward_baseline` is context-only and cannot pass promotion.

Promotion-eligible candidates:

1. `orb_pm_gap003`: direction-aligned gap of at least 0.3%; primary hypothesis.
2. `orb_pm_gap005`: direction-aligned gap of at least 0.5%; robustness threshold.
3. `orb_pm_gap003_structure`: 0.3% gap, aligned VWAP position, aligned final-30-minute
   return, and spread no greater than 15 bp.
4. `orb_pm_gap003_rvol150`: 0.3% gap and premarket relative volume at least 1.5.
5. `orb_pm_gap003_market_sector`: 0.3% gap with aligned SPY and sector-ETF gaps.
6. `orb_pm_combined`: all structure, relative-volume, market, and sector conditions.

Long alignment requires a positive value; short alignment is the exact sign-symmetric
negative condition. No thresholds may be altered within this forward sample. Any future
candidate starts a new version and a new disjoint sample.

## Readiness schedule

- 20 sealed scored market days: data-quality review only.
- 60 days: preliminary stability review; no promotion decision.
- 120 days: day gate can pass.
- 250 eligible observations per candidate: observation gate can pass.

The readiness report shows collected days, days remaining, the estimated 120-day date,
observations and observations remaining for every candidate, quote coverage, missingness,
and individual promotion checks. Observation-date projection begins only after 20 days,
when an actual arrival rate exists.

The fixed promotion gate requires all of the following for the same candidate:

- at least 120 sealed scored market days;
- at least 250 eligible observations;
- positive mean at 6 bp in both chronological halves;
- positive daily-block-bootstrap lower 95% bound at 6 bp;
- nonnegative mean at 12 bp;
- at least 90% quote coverage;
- no more than 10% missingness for every required feature;
- a genuinely future sample disjoint from historical discovery.

Failure of any check blocks live promotion. Passing makes the candidate eligible for
human review; it does not automatically change or place orders.

## Commands and output

- `premarket_forward_evaluator.py --mode update`
- `premarket_forward_evaluator.py --mode status`
- `premarket_forward_evaluator.py --mode verify`
- Machine status: `data/research/premarket_forward/evaluation/status.json`
- One-line status: `data/research/premarket_forward/evaluation/STATUS.txt`
- Derived observations: `data/research/premarket_forward/evaluation/observations.jsonl`

The outputs are deterministic derivatives of immutable sealed sources. They can be
rebuilt, while the source capture and SIP archives remain non-overwritable.
