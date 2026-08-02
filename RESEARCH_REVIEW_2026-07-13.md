# Trading-Bot Research Review — 2026-07-13

## Why this work was necessary

The bot had promising legacy backtests, a live paper execution path, and a polished
dashboard, but those parts did not yet share one provable definition of a trade. A good-
looking backtest is unsafe when its signal timing, universe, fill assumptions, or costs
differ from production. The work therefore started by preserving live behavior and
building an independent evidence chain before changing any strategy.

## What was built

### Research truth layer

The strategy freeze records hashes of live runtime files. The deterministic intent
ledger reconstructs 420 unique trade IDs and separates setup, approval, submission,
fill, non-fill, and close states. The append-only registry now records accepted and
rejected hypotheses without rewriting history.

Why: without immutable inputs and trade lineage, later results cannot prove which bot
version or execution state they measured.

### Live/backtest parity harness

Recorded trigger-to-order payloads matched exactly for all 80 submitted orders, proving
that the recorded execution mapping itself was reproducible. Four simulator-contract
differences remained:

1. MR live cadence was five minutes while a legacy backtest sampled 15 minutes;
2. ORB live required a close break while a backtest used wick touches;
3. MR universes differed;
4. ORB entry-bar handling was inconsistent.

Why: a strategy cannot inherit an old Sharpe ratio when the old simulator traded a
different event.

### Canonical replay and promotion gate

The canonical offline contract uses closed five-minute bars, close-confirmed ORB breaks,
next-bar order activation, pessimistic stop-first ambiguity, intent-level non-fills, and
6/12/20/30 bp cost scenarios.

At 6 bp:

- MR: -0.167R per intent over 10,891 intents / 510 days;
- ORB: -0.173R per intent over 1,639 intents / 383 days.

Daily block-bootstrap intervals confirmed the frozen implementations were not eligible
for promotion.

### Execution-candidate milestone

The intent ledger exposed adverse fill selection: delayed boundary limits missed many
continuations and disproportionately filled pullbacks. Seventeen pre-registered variants
tested next-bar-open entries, EOD/fixed-R exits, gap guards, risk floors, time filters,
and prior-day regimes.

Best broad next-open/EOD results at 6 bp:

- MR: -0.062R;
- ORB: -0.057R.

Execution realism reduced the loss but did not create an edge. Training-selected OOS
paths were -0.060R for MR and -0.121R for ORB. Both families were rejected.

### Signal-quality milestone

Eighteen candidates tested MR reclaim/exhaustion/volume/breadth and ORB overnight gap,
opening drive, market breadth, and sector breadth. Point-in-time breadth covered all
12,530 canonical intents after correcting and rerunning a predecessor-timestamp defect.

Training-selected OOS results at 6 bp:

- MR: -0.011R across 368 days, confidence interval crossing zero;
- ORB: -0.049R across 298 days, confidence interval crossing zero.

The tempting ORB gap >=0.3% slice was +0.028R full-sample but unstable:

- 2024: -0.353R;
- 2025: +0.193R;
- 2026: -0.241R.

It also turned negative at 12 bp. The correct conclusion was regime instability, not a
new live edge.

## Main findings

1. The frozen MR and ORB implementations do not possess cost-adjusted historical edge
   under their actual contracts.
2. Delayed boundary limits introduce adverse selection, but execution redesign alone is
   insufficient.
3. MR reclaim/location filters nearly remove the loss but do not establish positive
   expectancy.
4. ORB gap behavior is worth observing prospectively, but historical yearly instability
   forbids promotion.
5. Current static-universe and five-minute-bar limitations remain material.
6. Only seven true premarket snapshots existed, so prior premarket conclusions lacked a
   valid forward sample.

## Safety outcome

No live scanner, executor, portfolio gate, cron trading cadence, dashboard, or Netlify
connection was changed by the research milestones. Two research-only collector entries
were added without altering any trading schedule. The complete suite now passes 23/23
tests, the sealed archive verifies 11,442 records, and all 10 frozen runtime hashes remain
unchanged.

## Next step

The sealed Alpaca premarket protocol now collects raw point-in-time evidence before the
open and comprehensive SIP audit data after the close. This replaces further historical
filter mining with a genuinely future sample and creates the evidence needed to decide
whether premarket gap, relative volume, VWAP/lid, breadth, spreads, and depth add a stable
ORB edge.

Weekday captures are scheduled for 09:15 ET, with comprehensive SIP backfill at 16:30 ET.
The fixed promotion gate is defined in `PREMARKET_FORWARD_PROTOCOL.md`.
