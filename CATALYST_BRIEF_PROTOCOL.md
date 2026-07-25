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
./venv/bin/python -m unittest -v test_catalyst_brief.py test_official_calendar_pull.py
./venv/bin/python scripts/install_catalyst_brief_cron.py
```

`preview` is for diagnostics outside the official window and is never added to the official archive. The first 20 scored sessions are observation-only; report outcomes are evidence for later strategy research, not permission to change live thresholds.

## Known coverage boundary

The initial scheduled-event calendar covers official BLS releases. Fed, Treasury, ISM, and company-earnings calendars are not yet integrated and the PDF states that limitation explicitly.
