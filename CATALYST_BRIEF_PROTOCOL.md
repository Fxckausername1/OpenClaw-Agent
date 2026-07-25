# BOT_NEXUS Catalyst Brief Protocol

The Catalyst Brief is a deterministic, advisory report generated on the VPS for every regular U.S. trading session. It always evaluates SPY and QQQ. It cannot place orders or change strategy settings.

## Daily cadence (America/New_York)

- 08:36 — refresh the independent BLS calendar and free EDGAR/openFDA catalyst feed.
- 09:52 — run the official build after the first 15-minute opening range plus a five-minute confirmation bar are available. The PDF is normally published by 09:55.
- 10:06 — write the live narrative-hinge status.
- 16:10 — score the session after the close.

The wrappers enforce the Eastern-time windows themselves, so the paired UTC cron hours remain safe across daylight-saving changes.

## Inputs and hard freshness gates

- Monthly SPY/QQQ GEX: 45 minutes.
- Same-day SPY/QQQ GEX: 6 minutes.
- Alpaca IEX one-minute bars: opening range through 09:50.
- Sector rotation CSV: contextual evidence only.
- SEC EDGAR 8-K and openFDA events: 30 hours.
- Official BLS calendar: 12 hours.

Missing or stale required evidence lowers the brief to `INSUFFICIENT EVIDENCE`; it is never silently replaced with an invented claim.

## Interpretation

- Monthly and 0DTE GEX both negative: expansion / ORB-compatible.
- Monthly and 0DTE GEX both positive: pinning / MR-compatible.
- Tenors disagree: cross-tenor conflict / selective.
- SPY and QQQ disagree: cross-index divergence / selective.
- Required evidence unavailable: stand aside.

The report contains one answerable thesis question, a trigger and constraint, an executive conclusion, a narrative hinge, supporting and opposing evidence, a known-calendar section, three daily scenario paths, and a source/freshness appendix.

## Meaningfulness standard (schema v2)

A repeated regime label is not allowed to stand alone. Every report compares itself with the prior immutable brief and records:

- monthly and 0DTE GEX magnitude changes;
- opening-range location and width changes;
- the prior session's fixed post-close score;
- today's early leader and the index withholding confirmation;
- ranked macro headlines with publication time, source URL, market mechanism, and an observed cross-asset check.

Macro discovery uses Finnhub general-market news, Yahoo Finance news search, and official Federal Reserve monetary-policy/speech RSS. The macro pull runs at 08:36 and again immediately before the 09:52 build. News is secondary context and cannot override price/GEX. A headline is explicitly marked confirmed, unconfirmed, or contradicted using TLT, UUP, USO, IWM, SOXX, SPY, and QQQ opening-session moves.

The PDF header reports core-data and macro-data quality separately. A fresh BLS file does not imply complete macro coverage.

## Manual SPY/QQQ options decision layer (schema v3)

This layer is for Heff's discretionary options trading only. It is isolated from MR,
ORB, scanners, executors, strategy settings, and order placement.

For SPY and QQQ it records:

- completed 1-minute EMA 9/20 alignment and position versus VWAP;
- prior-session SMA 20/50/200 context;
- prior-day, premarket, and opening-range reference levels; premarket levels use
  the completed consolidated SIP window and require at least 10 one-minute bars;
- a strict liquidity-sweep proxy: a low may only produce a bullish sell-side sweep
  after penetration, reclaim close, and next-bar hold; a high may only produce a
  bearish buy-side sweep after penetration, rejection close, and next-bar hold;
- five fixed underlying votes and mandatory SPY/QQQ directional alignment;
- exact call-watch, put-watch, and invalidation levels.

A contract is surfaced only when both indices align and core data is fresh. The
research screen uses the user's fixed $400 budget, $0.20-$0.30 ask band, and +25%
premium target rounded up to the next whole cent. It reports contract count, debit,
spread, spread as a percentage of midpoint, T-1 open interest, Greeks, estimated
full-spread cost as a share of gross target, and a constant-IV estimate of the
underlying move required to reach the target.

The initial contract screen is observation-only and rejects quotes older than 120
seconds, spreads over $0.02 or 10% of midpoint, absolute delta below 0.10, or estimated
full-spread friction over 35% of gross target. These are fixed research filters, not
validated edge. Alpaca's free option feed is indicative rather than executable NBBO,
so every surfaced candidate says `VERIFY LIVE`; the live broker quote remains the
only acceptable execution reference.

News and macro remain in the report. They provide mechanism and event-risk context,
but they cannot create a call/put watch or override price, GEX, SPY/QQQ alignment, or
contract-quality rejection.

## Outputs

Official immutable archives live under:

`data/catalyst_briefs/YYYY/MM/YYYY-MM-DD/`

Each session includes `brief.json`, `brief.pdf`, `manifest.json`, and later `status.json` and `score.json`. The manifest hashes the report, source packet, and generator. Official files refuse overwrite.

The publisher copies only report artifacts to the private `trading-dashboard-snapshot` repository under `briefs/` and updates `briefs/index.json`. The Netlify dashboard retrieves those files server-side through an authenticated, path-restricted function.

## Operations

```bash
cd /home/heff/.openclaw/workspace
./venv/bin/python catalyst_brief.py --mode preview
./venv/bin/python catalyst_brief.py --mode verify
./venv/bin/python -m unittest -v test_catalyst_brief.py test_macro_news_pull.py test_official_calendar_pull.py test_manual_options_brief.py
./venv/bin/python scripts/install_catalyst_brief_cron.py
```

`preview` is for diagnostics outside the official window and is never added to the official archive. The first 20 scored sessions are observation-only; report outcomes are evidence for later strategy research, not permission to change live thresholds.

## Known coverage boundary

The initial scheduled-event calendar covers official BLS releases. Fed, Treasury, ISM, and company-earnings calendars are not yet integrated and the PDF states that limitation explicitly.
