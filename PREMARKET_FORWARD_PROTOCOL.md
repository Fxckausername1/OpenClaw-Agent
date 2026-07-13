# Sealed Premarket Forward Protocol

Version: `premarket-forward-2026-07-13.2`

## Purpose

Collect point-in-time premarket evidence without changing the live MR/ORB scanners or
using future outcomes to rewrite the original observation. The archive is research-only.

## Provider decision

Alpaca is the collection provider. The box already uses its authenticated market-data
API, and account probes confirmed:

- live IEX historical-bar and snapshot access;
- historical SIP bar access when the requested end is at least 15 minutes old;
- provider request IDs on every successful response.

QuantConnect credentials also authenticate and LEAN supports extended-market-hours
subscriptions, but using it would add a second deployment and still require a live data
provider. It is not needed for this collector.

Official references:

- <https://docs.alpaca.markets/us/v1.4.2/reference/stockbars>
- <https://docs.alpaca.markets/us/reference/stocksnapshots-1>
- <https://docs.alpaca.markets/us/docs/market-data-faq>
- <https://docs.alpaca.markets/us/v1.4.2/docs/real-time-stock-pricing-data>
- <https://www.quantconnect.com/docs/v2/writing-algorithms/securities/market-hours>
- <https://www.quantconnect.com/docs/v2/writing-algorithms/live-trading/data-providers>

## Two-stage collection

### 1. Pre-open IEX capture

- Target: 09:15 ET on weekdays.
- Window: 04:00 ET through the latest completed minute.
- Data: one-minute IEX bars plus current IEX snapshots, including quote/trade and prior
  daily-bar fields when the provider supplies them.
- Role: sealed point-in-time forward evidence. This file is available before the open.

The wrapper accepts execution only from 09:10 through 09:19 ET. Cron contains both UTC
DST possibilities, and the ET gate ensures only the correct one can run.

### 2. Post-close SIP backfill

- Target: 16:30 ET on weekdays.
- Window: 04:00-09:29:59 ET plus trailing SIP daily bars for prior-close reconstruction.
- Role: comprehensive research/audit data. It is explicitly marked unavailable for the
  pre-open decision and cannot replace or modify the IEX capture.

Post-close timing prevents the heavier SIP download from competing with live scanners.

## Immutability

Each mode writes a JSONL data file and JSON manifest beneath:

`data/research/premarket_forward/YYYY-MM-DD/`

The collector:

- writes with atomic hard-link creation and refuses overwrite;
- records SHA-256 of the data, universe, collector, and this protocol;
- records collection time, requested window, feed, universe, counts, and Alpaca request IDs;
- verifies the data hash immediately after sealing;
- treats a lone data or manifest file as an error requiring manual audit;
- makes repeated runs idempotent only when the existing pair verifies.

## Pre-registered future questions

The archive may evaluate, without changing the frozen historical result:

1. ORB direction-aligned gap thresholds of 0.3% and 0.5%;
2. premarket relative volume versus the same symbol/feed trailing baseline;
3. premarket VWAP position, range compression, and late-session lid quality;
4. market and sector premarket confirmation;
5. spread, quote depth, and missing-bar coverage as tradeability gates.

## Fixed forward gate

These thresholds were locked before the first scheduled pre-open capture. Promotion
requires all of them:

- at least 120 sealed market days and 250 eligible observations;
- positive 6 bp expectancy in both chronological halves;
- a positive daily-block-bootstrap lower 95% bound at 6 bp;
- nonnegative mean expectancy at 12 bp;
- quote coverage of at least 90%;
- missing rate no greater than 10% for every required feature;
- a genuinely future sample disjoint from the historical discovery data.

No live rule may be derived from an unsealed same-day file. Historical discovery remains
separate from future forward evaluation. Failure of any gate rejects live promotion.

## Operations

- `premarket_forward_collector.py --mode capture`
- `premarket_forward_collector.py --mode sip-backfill`
- `premarket_forward_collector.py --mode verify`
- Wrapper log: `logs/premarket_forward_collector.log`

The collector never imports the scanner, executor, portfolio gate, dashboard, or Netlify
code and never submits orders.
