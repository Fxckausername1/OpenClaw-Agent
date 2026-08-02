---
name: groundwork
description: Groundwork — AI-augmented affordable-housing development advisory for small Atlanta value-add deals (8-15 unit multifamily + rehabs). A 6-agent stack that underwrites a deal, finds the funding stack, pulls HUD market data, audits construction pay applications, shortlists property managers, and drafts off-market owner outreach. Use whenever the user is working a small-MF Atlanta deal — feasibility, funding, draws, PM selection, or lead-gen. Every output is a DRAFT for expert (Rey's) review; dollar figures are ESTIMATES pending verification.
user-invocable: true
---

# Groundwork — deal advisory stack

Six deterministic, pure-stdlib agents. Run all from the workspace root with
`./venv/bin/python`. Each prints a human report; analytical ones also take
`--json` for piping. **Nothing here is client-ready until Rey reviews it** —
the human-in-the-loop verify step is the product, not an afterthought.

The niche is fixed: **8-15 unit multifamily + value-add rehabs of older Atlanta
stock.** Deals are judged on **return percentages** (Rey's rule), not absolute $.

## 1 · Underwriter + Money-Finder — `deal_snapshot.py`

Underwrites a deal (proforma → NOI → stabilized value → debt sized by
min(LTC/LTV/DSCR) → funding stack/gap → verdict), return-first, and flags the
funding/incentive stack.

```
./venv/bin/python deal_snapshot.py --sample d                 # one deal
./venv/bin/python deal_snapshot.py --sample d --html out.html # styled report
./venv/bin/python deal_snapshot.py --compare all --rank-by roe # rank deals by return
./venv/bin/python deal_snapshot.py --deal mydeal.json --lens flipper
```

`--lens partner` leads on development spread (deal quality); `--lens flipper`
leads on return-on-equity (the client's cash). Verdict bar = dev spread ≥100 bps
+ YoC floor + DSCR.

## 2 · Researcher — `gw_researcher.py`

Pulls HUD AMI income limits + Fair Market Rents for the Atlanta metro, computes
LIHTC-style max rents by bedroom (50/60/80% AMI), reality-checks a deal's
proposed rents, and surfaces the verified incentive/compliance set.

```
./venv/bin/python gw_researcher.py --market                   # AMI + FMR context
./venv/bin/python gw_researcher.py --market --zip 30312        # route to a ZIP's HUD area
./venv/bin/python gw_researcher.py --sample d --html r.html    # styled review doc
./venv/bin/python gw_researcher.py --deal mydeal.json --json   # feed the underwriter
```

`--zip` resolves a property ZIP to its HUD area via the USPS crosswalk and pulls
that ZIP's small-area FMR. Auto-routing is verified for Atlanta only (CBSA
12060); other metros print a loud WARNING rather than mislabel another area's
numbers. Add new metros to `KNOWN_METROS` as the practice expands.

- **Live data** when `HUD_API_TOKEN` is set in the env (free token at
  huduser.gov/portal/dataset/fmr-api.html). The source line reads `LIVE` then.
- **No token → bundled FY2025 fallback**, marked `BUNDLED ESTIMATE` — runs
  offline but must be verified before client use.
- DSIRE has no stable public API, so incentives are the hand-curated
  primary-source list (CBEEO, HTC, 179D/45L). Verify before crediting any of it.

## 3 · Build-Watcher — `gw_buildwatch.py`

Audits an AIA G702/G703 pay application (draw request): recomputes every line
and the summary independently, catching arithmetic errors, overbilling, bad
retainage, and front-loading. Owner's-rep work — supports Rey's signature, does
not replace it. Exits non-zero when discrepancies are found.

```
./venv/bin/python gw_buildwatch.py --sample clean
./venv/bin/python gw_buildwatch.py --sample flagged           # planted-error drill
./venv/bin/python gw_buildwatch.py --draw draw3.json --html audit.html
./venv/bin/python gw_buildwatch.py --file payapp.xlsx --html audit.html  # raw file
./venv/bin/python gw_buildwatch.py --file payapp.xlsx --retainage-pct 0.10 --previous-payments 200000
```

Input JSON: `contract_sum`, `change_orders`, `retainage_pct`,
`previous_payments`, and `line_items[]` with `scheduled_value`, `from_previous`,
`this_period`, `materials_stored` (stated `completed_to_date` /
`balance_to_finish` are checked if present).

**Raw files via `--file` (xlsx / csv / pdf):** `gw_parser.py` is the front-door —
it reads a real AIA **G703 continuation sheet** and maps it into the JSON above
(keyword-based column detection, tolerant of layout). G702 cover-page figures
(`--retainage-pct`, `--previous-payments`) are passed on the CLI since they
aren't on the G703; without them the per-line audit still runs fully, only the
G702-summary reconciliation is skipped. Make a synthetic fixture with
`./venv/bin/python gw_parser.py --make-sample test.xlsx [--clean]`. Needs
`openpyxl` (xlsx) and `pdfplumber` (pdf); csv is stdlib.

## 4 · PM-Scout — `gw_pmscout.py`

Scores/ranks third-party PM candidates for an 8-15-unit deal (unit-fit, AH
experience, fee competitiveness, market, tech) and shows the annual fee drag on
NOI, which flows back into the underwriter's `pm_fee_pct`.

```
./venv/bin/python gw_pmscout.py --sample --units 12 --html pm.html
./venv/bin/python gw_pmscout.py --candidates pms.json --units 10 --egi 250000
```

## 5 · Door-Opener — `gw_dooropener.py`

Scores off-market owner leads on sell-likelihood + fit, then generates
value-first outreach **DRAFTS**. **Sending requires explicit human approval** —
no auto-send, no scraping (you supply the leads). Distress signals are leads,
not leverage.

```
./venv/bin/python gw_dooropener.py --sample --top 2 --html leads.html
./venv/bin/python gw_dooropener.py --leads leads.json --top 3
./venv/bin/python gw_dooropener.py --scrape --limit 50 --top 5 --html leads.html  # auto-source
```

**Auto-sourcing via `--scrape` (`gw_scraper.py`):** pulls small-MF leads straight
from **Fulton County's open-data ArcGIS parcel layer** (free, public record, no
new dependency — stdlib `urllib`; no Apify/Playwright/paid API). Filters by
`LivUnits` (the 8-15 box), and derives `absentee_owner` by comparing the owner's
mailing street to the property's situs street. Run the scraper alone with
`./venv/bin/python gw_scraper.py --count` or `--limit N --out leads.json`.

Source covers **owner, mailing address, situs, unit count** — so it nails the
two highest-weight signals (absentee + size fit). `--scrape` then runs the
enrichment layer (below) to add what it can.

**Enrichment (`gw_enrich.py`, on by default; `--no-enrich` to skip).** Real
extraction for all five signals — three live from public APIs, two via scrapers
that the target sites bot-block from a datacenter IP (graceful, with warnings):

| Signal | Status |
|---|---|
| `last_sale_year` | **LIVE** — Fulton `Tyler_YearlySales` (valid sales 2018-2022, Price>0). ~23% of 8-15u parcels sold recently; rest long-held (None). Recently-sold rank lower → stratification. |
| `code_violations` | **LIVE** — Atlanta "Code Enforcement Data 2021-2023" FeatureServer (20.8k cases), matched by street #+name. True if any case. Atlanta city limits only. |
| `tired_condition` | **LIVE** — from matched CE cases: hazard/blight/junk/boarded/structural/abandoned description or the junk-trash flag. |
| `year_built` | **SCRAPE** — real qPublic (Schneider) parcel-detail scraper; 403-blocked from datacenter IP → warn + None. Activates via residential IP / `PROXIES`. |
| `tax_delinquent` | **SCRAPE** — real Fulton Tax-Commissioner scraper (Amount Due / Fi.Fa. / prior-year); 403-blocked → warn + None. Activates via `PROXIES`. |

Needs `requests` + `beautifulsoup4` (in requirements.txt). Each lead carries an
`_enrichment` provenance dict (`live(N case(s))` / `live(no recent sale)` /
`blocked/none` / `error:Type`) so a neutral default is never mistaken for "no
distress." Every signal is guarded — a slow/blocked source degrades that one
field, never the run. To activate the two scrapers, set `PROXIES` in
`gw_enrich.py` to a residential/proxy endpoint. DeKalb is a TODO in `SOURCES`.

## Deal Intake — `gw_intake.py` (start here for a new deal)

Guided CLI wizard that turns deal facts into the JSON the engine eats — no
hand-editing. Asks in plain language (address, units, asking price, rents, rehab
budget, cap rate, incentive context), validates and cleans every entry (money:
`$1,150,000`→`1150000`; rates: `6.5`→`0.065`), fills anything skipped with
`deal_snapshot.py`'s own `DEFAULTS` (imported, not copied), saves a timestamped
JSON to `deals/`, then builds the combined packet automatically.

```
./venv/bin/python gw_intake.py                 # wizard -> deals/<slug>_<ts>.json + .html
./venv/bin/python gw_intake.py --no-packet     # just save the JSON
```

Blank = take the default; end-of-input on a required field exits cleanly. Pure
stdlib (no install). This is the front door that *starts* the pipeline.

## Combined Deal Packet — `gw_packet.py` (the one doc for Rey)

Composes the three **feasibility-stage** agents for ONE deal into a single
branded HTML: (1) Snapshot verdict + return profile, (2) Researcher market
grounding + rent check (live HUD), (3) PM-Scout shortlist with fee drag computed
off that deal's own EGI. Numbers come from the canonical compute functions; the
packet only lays them out. Build-Watcher (per-draw) and Door-Opener
(acquisition) stay separate — they're different workflow stages.

```
./venv/bin/python gw_packet.py --sample d --html packet_d.html
./venv/bin/python gw_packet.py --deal mydeal.json --candidates pms.json --html packet.html
```

This is the artifact to put in front of Rey for a feasibility review.

## Review docs for Rey (how she sees the work)

Every agent takes `--html <path>` and writes a branded, self-contained HTML
report (shared shell: `gw_report.py`, same forest/sage/gold identity as the
Deal Snapshot). Rey never touches the box — the flow is:

1. Run the agent with `--html out.html` on the box.
2. Pull it down: `scp heff@<box>:~/.openclaw/workspace/out.html .` (or write
   into a synced folder), or deploy via Netlify drag-drop for a shareable link.
3. Rey opens the file / link and reviews. Every doc is badged **DRAFT** and
   footed with the ESTIMATE / verify disclaimer.

`gw_report.py` must sit alongside the agents (it's imported lazily, only when
`--html` is used — text and `--json` modes don't need it).

## Typical flow

0. `gw_intake.py` → enter deal facts, get `deals/<slug>_<ts>.json` + the packet.
1. `gw_researcher.py --deal d.json` → confirm rents/affordability + incentives.
2. `deal_snapshot.py --deal d.json --html` → underwrite + verdict + funding gap.
3. `gw_pmscout.py --egi <EGI>` → pick a PM; feed the fee % back into the deal.
4. (acquisition) `gw_dooropener.py` to source; (construction) `gw_buildwatch.py`
   on each draw.
5. **Rey reviews every figure before anything goes to a client.**

## Hard rules (carry into any generated material)

- Never put Rey's employer or degree in client-facing output.
- Keep the practice separate from Rey's W-2 (no overlapping clients/data).
- Rey-facing materials: confident, direct, sourced — no soft "no pressure"
  hype. (Cold *owner* outreach in Door-Opener is the exception — that register
  is standard there.)
- All $ are ESTIMATES; tax/incentive figures are `[verify]` pending primary
  sources. Not tax, legal, or investment advice.
