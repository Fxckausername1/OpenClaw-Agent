# GROUNDWORK — Full Project Handoff
_Last updated 2026-06-17_

## 1. What & who
**Groundwork** is an AI-augmented **affordable-housing development advisory** practice in **Atlanta**. Fronted by **Rey** (Demond's partner) as the credentialed expert; **Demond** builds the systems, **Rey reviews and signs off**. The model is human-in-the-loop on purpose — AI does the grunt research/drafting, Rey's judgment + signature is the product (the opposite of "automate everything"). **Not client-facing until ~Aug 2026**; now is build/validation runway.

## 2. People & hard rules (carry into everything)
- **Never** put Rey's employer (**Mercy Housing Southeast**) or her **degree** in any client-facing material.
- **Conflict-of-interest:** Rey has a W-2 in this exact field. The practice stays fully separate from her day job (own clients/time, nothing overlapping). She checks her employment/COI policy before taking clients. Don't ingest her employer's data/pipeline.
- **Rey-facing materials:** confident, direct, sourced — **no soft "no pressure / just take a look" hype**. (Cold *owner* outreach in Door-Opener is the one place that register is standard and fine.)
- **Everything is a DRAFT / ESTIMATE until Rey verifies.** That human-verify gate is the product.

## 3. The niche (Rey's call)
Small-scale: **8–15 unit multifamily + value-add rehabs** of older Atlanta stock. Implications baked into the engine:
- At 8–15 units LIHTC rarely pencils → capital stack leans small-balance/CDFI/local-bank debt + soft money (HOME, housing trust, CDBG) + Historic Tax Credits + 179D/45L + owner equity.
- Rehab makes Rey's owner's-rep skills (G702/G703 pay-app & draw review) central.
- 8–15 units needs third-party PM → proformas bake in PM fees + reserves.
- **Decision metric:** deals chosen on **return %**, not absolute $. Lens depends on seat — partner → development spread (deal quality); investor/flipper → return-on-equity (their cash).

## 4. Positioning & funnel
Practice = affordable-housing development advisory. Wedge = free one-page **Deal Snapshot** → paid feasibility/underwriting ($1.5–4k) → project engagement ($5–20k) → retainer. Targets: emerging/small developers, nonprofits, faith orgs with land.

## 5. The pipeline (end to end)
```
ENTRY (a deal appears)
 ├─ inbound: prospect brings it  ──┐
 └─ outbound: gw_scraper finds it ─┤
                                   ▼
gw_intake  (type facts → deals/<slug>_<ts>.json)
                                   ▼
gw_packet  (Snapshot + Researcher + PM-Scout → one HTML)
                                   ▼
   ★ REY VERIFIES ★   ← mandatory gate, the product (NOT yet done)
                                   ▼
client-final  (strip DRAFT/ESTIMATE — NOT BUILT) → deliver → convert
Construction stage: gw_buildwatch audits each G702/G703 draw.
Acquisition stage:  gw_dooropener scores leads + drafts outreach (drafts only).
```

## 6. The tech stack — 12 files (`~/.openclaw/workspace/`, mirror in `C:\Users\Antonio Howard\groundwork-agents\`)
Run from workspace root with `./venv/bin/python`. Pure stdlib except where noted.

| File | Role | Key commands |
|---|---|---|
| `deal_snapshot.py` | **Underwriter + Money-Finder.** Proforma→NOI→value→debt (min LTC/LTV/DSCR)→funding gap→verdict; return-first; `--compare` ranks; `--lens partner/flipper`; `--html`. | `--sample d --html out.html` · `--compare all --rank-by roe` |
| `gw_researcher.py` | **Researcher.** Live HUD AMI/FMR, LIHTC max-rent bands, rent reality-check, incentive set. `--zip` routes via USPS crosswalk. | `--market --zip 30312` · `--sample d --html r.html` |
| `gw_buildwatch.py` | **Build-Watcher.** Audits AIA G702/G703 pay apps (overbilling, retainage, front-loading). `--file` ingests xlsx/csv/pdf. | `--file payapp.xlsx --html a.html` · `--sample flagged` |
| `gw_pmscout.py` | **PM-Scout.** Scores/ranks third-party PMs; fee drag on NOI. | `--sample --units 12 --html pm.html` |
| `gw_dooropener.py` | **Door-Opener.** Scores off-market leads + outreach DRAFTS (no auto-send). `--scrape` auto-sources + enriches. | `--scrape --limit 50 --top 5 --html l.html` |
| `gw_scraper.py` | **Deal-sourcing.** Pulls 8–15u parcels from Fulton open-data ArcGIS; derives absentee_owner. | `--count` · `--limit 50 --out leads.json` |
| `gw_enrich.py` | **Enrichment.** Adds sale year (live), code violations + tired_condition (live), year_built + tax_delinquent (scrapers, IP-blocked). | (used via `--scrape`) |
| `gw_intake.py` | **Deal-Intake wizard.** Guided CLI → validated `deals/<slug>_<ts>.json` → auto-builds packet. | `gw_intake.py` · `--no-packet` |
| `gw_packet.py` | **Combined Deal Packet.** Snapshot + Researcher + PM-Scout in ONE Rey-facing HTML. | `--sample d --html packet.html` |
| `gw_report.py` | Shared HTML shell (brand palette, sections, tables) for all `--html` output. | (imported) |
| `skills/groundwork/SKILL.md` | OpenClaw skill manifest documenting the whole stack. | (invocable) |
| `HANDOFF.md` | This document. | — |

## 7. Data sources & enrichment status (all verified live)
- **HUD AMI/FMR + Income Limits** — LIVE via HUD USER API. Token in box `.env` (`HUD_API_TOKEN`, chmod 600, gitignored), loaded by a dependency-free `.env` reader in `gw_researcher.py`. FY2025 Atlanta 4-person AMI $114,200. Token scopes: FMR + Income Limits + ZIP Crosswalk + CHAS.
- **Fulton parcels** (8–15u, owner, mailing, situs, units) — LIVE, Fulton PropertyMapViewer ArcGIS (free, public record, stdlib urllib).
- **`last_sale_year`** — LIVE, Fulton `Tyler_YearlySales` (valid sales 2018–2022). ~23% of 8–15u parcels sold recently.
- **`code_violations` + `tired_condition`** — LIVE, Atlanta "Code Enforcement Data 2021-2023" FeatureServer (20.8k cases, Atlanta city limits only), matched by street #+name.
- **`year_built`** (qPublic scraper) + **`tax_delinquent`** (Tax Commissioner scraper) — real `requests`+`bs4` code, but both sites **403-block the datacenter IP** → graceful None + warning. Activate by setting `PROXIES` in `gw_enrich.py` to a residential proxy.
- DSIRE has no live API → incentives are a hand-curated verified set.

## 8. Infrastructure
- **Box:** `ssh heff@165.227.221.54`, workspace `~/.openclaw/workspace/`, python `./venv/bin/python`. Reliable remote write: `scp` from local.
- **Deploy (Rey-facing):** agents write branded HTML → `scp` down (or Netlify drag-drop, app.netlify.com/drop) → Rey opens. She never touches the box. Site: single-file `C:\Users\Antonio Howard\sustainable-advisory-site\index.html` (already deployed).
- **Dependencies (pip, in `requirements.txt`):** `openpyxl`, `pdfplumber` (Build-Watcher file ingest), `requests`, `beautifulsoup4` (enrichment scrapers). Everything else stdlib.
- **Local copies:** source in `groundwork-agents\`; generated docs in `groundwork-deals\agent-docs\`; intake outputs in `deals/`.

## 9. Verified facts (primary sources)
- **179D** (commercial energy, up to ~$5.81/sf 2025): ends for property whose construction begins **after 6/30/2026**.
- **45L** (homes): ends for homes acquired **after 6/30/2026**. Both: already-qualified can still claim (incl. retroactive).
- **Atlanta CBEEO:** commercial/multifamily ≥25,000 sf must benchmark yearly + ASHRAE Level II audit/10yr; non-compliance → $1,000/yr fine + public disclosure.
- **GA Historic Tax Credit:** 25% of QRE (income-producing cap $300k); stacks with federal 20%.
- Launch is post-6/30/26 → build on evergreen pillars (CBEEO, retroactive 179D, Historic credits, feasibility), don't anchor on the expiring credits.

## 10. Accuracy caveats (verify before any client use)
- All Snapshot/packet $ figures are **ESTIMATES** pending Rey's verification — mandatory before client-facing.
- Not independently confirmed: exact 179D per-point scaling, "ENERGY STAR 55" disclosure trigger, the "8.5-year" GA tax freeze.
- Code-enforcement data is a **2021–2023 snapshot** (recency approximate); covers **Atlanta city limits only**.
- Default value-add bar = **development spread ≥100 bps** — confirm with Rey it's right for her market.

## 11. Status — built vs not
**Built & verified:** all 6 agents + scraper/enrichment/intake/packet; live HUD (zip-routed); Build-Watcher file ingestion (xlsx/csv/pdf); Door-Opener auto-sourcing + enrichment; combined packet; HTML review docs for every agent; registered as OpenClaw skill.
**Not built:** client-final `--verified` render (strip DRAFT/ESTIMATE after sign-off); deal delivery/follow-up tracking; DeKalb scraper source; live `year_built`/`tax_delinquent` (need a proxy or paid aggregator); enrichment-based ranking beyond sale-recency.

## 12. Open decisions / next steps
1. **★ Get the stack in front of Rey** — run the audit-game (planted-error Adair Park Snapshot + flagged Build-Watcher draw), let her poke holes. Her reactions steer everything. **This is the gate; it hasn't happened.**
2. Confirm with Rey: 100 bps value-add bar; default lens (spread).
3. Build **client-final `--verified` mode** (highest-value next build — unblocks sending).
4. Decide on a **paid parcel/lien aggregator** (ATTOM/Regrid) only once outreach converts — replaces both blocked scrapers + adds year_built.
5. Add **DeKalb** to `gw_scraper.py` `SOURCES`.

## Demo artifacts (in `groundwork-deals\`)
- `groundwork_grant-park.html` (clean reference), `groundwork_adair-park.html` (**5 planted errors** for Rey's audit game), `groundwork_deal-comparison.html`.
- `agent-docs\` — sample HTML from every agent + two combined packets + a file-sourced Build-Watcher audit + an enriched Door-Opener run.
