#!/usr/bin/env python3
"""gw_researcher.py — Groundwork's Researcher agent.

Pulls the market context a Deal Snapshot needs and surfaces the incentive /
compliance landscape for a small Atlanta value-add deal:

  • HUD AMI income limits + Fair Market Rents (FMR) for the metro
  • LIHTC-style max rents by bedroom at 50/60/80% AMI (standard 1.5 persons/BR)
  • A reality check of a deal's PROPOSED rents against FMR and the AMI bands
  • The verified GA / Atlanta incentive + compliance set (CBEEO, HTC, 179D/45L)

Data sources, in order of preference:
  1. LIVE HUD USER API  — used automatically if HUD_API_TOKEN is set in the env.
     (free token: https://www.huduser.gov/portal/dataset/fmr-api.html)
  2. BUNDLED fallback   — FY2025 Atlanta-Sandy Springs-Alpharetta MSA figures,
     marked [verify]. Lets the agent run offline; NOT a substitute for the pull.

DSIRE has no stable public API anymore, so the incentive set is the verified
hand-curated list from Groundwork's primary-source research (IRS OBBB FAQ,
envigilance CBEEO, GA DCA/SHPO). Every $ is an ESTIMATE pending verification.

Usage:
  ./venv/bin/python gw_researcher.py --market                 # AMI + FMR context
  ./venv/bin/python gw_researcher.py --deal mydeal.json       # check a deal's rents
  ./venv/bin/python gw_researcher.py --sample d               # check a sample deal
  ./venv/bin/python gw_researcher.py --deal d.json --json     # machine-readable
"""
import argparse
import json
import os
import sys
from pathlib import Path
from urllib import request, error

# Atlanta-Sandy Springs-Alpharetta, GA HUD Metro FMR Area.
# HUD entity id for the FMR/IL APIs (CBSA-based metro code). Default = Atlanta;
# --zip resolves a property's CBSA via the USPS crosswalk and overrides this.
ATLANTA_ENTITY_ID = "METRO12060M12060"
ATLANTA_LABEL = "Atlanta-Sandy Springs-Roswell, GA HUD Metro FMR Area"

# ---- BUNDLED FALLBACK (FY2025, [verify] before any client use) ----------------
# 4-person 100% AMI for the Atlanta MSA, and FMR by bedroom. These are
# approximate and exist only so the agent runs without a token. The live pull
# OVERWRITES all of this when HUD_API_TOKEN is present.
FALLBACK = {
    "year": 2025,
    "source": "BUNDLED ESTIMATE — verify against huduser.gov",
    "median_family_income": 98_200,          # 4-person 100% AMI, Atlanta MSA (approx)
    "fmr": {"0": 1_440, "1": 1_540, "2": 1_760, "3": 2_180, "4": 2_560},
}

# Persons-per-unit HUD convention for LIHTC max-rent math: 1.5 persons per bedroom
# (a studio = 1 person). Income for a household size is adjusted off the 4-person AMI.
HH_SIZE_BY_BR = {0: 1, 1: 2, 2: 3, 3: 5, 4: 6}
# HUD family-size income adjustment factors relative to the 4-person figure.
SIZE_ADJ = {1: 0.70, 2: 0.80, 3: 0.90, 4: 1.00, 5: 1.08, 6: 1.16}

INCENTIVES = [
    ("CBEEO benchmarking", "verify",
     "Atlanta CBEEO: commercial/multifamily >=25,000 sf must benchmark energy "
     "annually + ASHRAE Level II audit every 10 yrs. Non-compliance: warning -> "
     "$1,000/yr fine + public disclosure. (envigilance.com)"),
    ("Historic Tax Credits", "verify",
     "GA State HTC = 25% of QRE (income-producing cap $300k) STACKS with the "
     "federal 20% HTC for certified historic rehabs. Verify certifiability w/ GA SHPO."),
    ("179D (commercial energy)", "verify",
     "Up to ~$5.81/sf 2025. ENDS for property whose construction begins after "
     "6/30/2026. Already-qualified projects can still claim (retroactive)."),
    ("45L (residential energy)", "verify",
     "ENDS for homes acquired after 6/30/2026. Already-qualified homes can still "
     "claim (retroactive)."),
    ("Funding stack", "verify",
     "At 8-15 units LIHTC rarely pencils -> small-balance/CDFI/local-bank debt + "
     "soft money (HOME, local housing trust, CDBG) + HTC + 179D/45L + owner equity."),
]


def _hud_get(path, token):
    req = request.Request(
        f"https://www.huduser.gov/hudapi/public/{path}",
        headers={"Authorization": f"Bearer {token}"},
    )
    with request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _load_env_file():
    """Populate os.environ from a workspace .env (dependency-free), for keys not
    already set in the real environment. Lets HUD_API_TOKEN live in .env without
    needing a shell export. Real env vars always win (setdefault)."""
    envp = Path(__file__).resolve().parent / ".env"
    if not envp.exists():
        return
    for line in envp.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


# HUD FMR/IL entity ids can't be reliably built from a raw CBSA code (multi-
# division metros like LA/Chicago use a different M-suffix), so we only
# auto-route CBSAs we've verified. Groundwork is Atlanta-only; add metros here
# as the practice expands rather than guessing an entity id.
KNOWN_METROS = {"12060": ("METRO12060M12060", "Atlanta")}


def resolve_area(zip_code, token):
    """Resolve a 5-digit ZIP to a verified HUD FMR/IL entity id via the USPS
    crosswalk (zip-cbsa, type=3).

    Returns (entity_id_or_None, note). Only returns an entity for CBSAs in
    KNOWN_METROS; for anything else it returns None with a loud note so the
    caller does NOT silently present another metro's numbers as this ZIP's.
    """
    try:
        cb = _hud_get(f"usps?type=3&query={zip_code}", token)["data"]["results"]
        if cb and cb[0].get("geoid"):
            cbsa = cb[0]["geoid"]
            if cbsa in KNOWN_METROS:
                entity, name = KNOWN_METROS[cbsa]
                return entity, f"ZIP {zip_code} -> CBSA {cbsa} ({name})"
            return None, (f"WARNING: ZIP {zip_code} is in CBSA {cbsa}, OUTSIDE Groundwork's "
                          f"Atlanta coverage — figures below are the Atlanta default, NOT this ZIP")
    except (error.URLError, KeyError, ValueError, TimeoutError):
        pass
    return None, f"WARNING: ZIP {zip_code} did not resolve — using Atlanta default, NOT this ZIP"


def _extract_fmr(basicdata, zip_code=None):
    """Pull the right FMR row. Metro returns a list whose first row is MSA-level;
    when a ZIP is given, prefer that ZIP's small-area row if present."""
    if not isinstance(basicdata, list):
        return basicdata, "area-level"
    if zip_code:
        for row in basicdata:
            if str(row.get("zip_code")) == str(zip_code):
                return row, f"small-area FMR for ZIP {zip_code}"
    return basicdata[0], "MSA-level"


def fetch_market(year=2025, zip_code=None):
    """Return {year, source, median_family_income, fmr{br:rent}, area_name, ...}.

    Live pull if HUD_API_TOKEN is set; otherwise the bundled fallback. A ZIP
    routes the pull to that property's HUD area via the USPS crosswalk.
    """
    _load_env_file()
    token = os.environ.get("HUD_API_TOKEN")
    if not token:
        fb = dict(FALLBACK)
        fb["area_name"] = ATLANTA_LABEL
        return fb
    entity, route_note = ATLANTA_ENTITY_ID, None
    if zip_code:
        resolved, route_note = resolve_area(zip_code, token)
        if resolved:
            entity = resolved
    try:
        il = _hud_get(f"il/data/{entity}?year={year}", token)
        fmr = _hud_get(f"fmr/data/{entity}?year={year}", token)
        mfi = il["data"]["median_income"]
        f, fmr_note = _extract_fmr(fmr["data"]["basicdata"], zip_code)
        src = "LIVE — HUD USER API (huduser.gov)"
        if route_note:
            src += f" [{route_note}; {fmr_note}]"
        return {
            "year": year, "source": src,
            "area_name": fmr["data"].get("area_name", ATLANTA_LABEL),
            "median_family_income": int(mfi),
            "fmr": {
                "0": int(f["Efficiency"]), "1": int(f["One-Bedroom"]),
                "2": int(f["Two-Bedroom"]), "3": int(f["Three-Bedroom"]),
                "4": int(f["Four-Bedroom"]),
            },
        }
    except (error.URLError, KeyError, ValueError, TimeoutError) as e:
        fb = dict(FALLBACK)
        fb["area_name"] = ATLANTA_LABEL
        fb["source"] = f"BUNDLED ESTIMATE — live pull failed ({type(e).__name__}); verify"
        return fb


def max_rent_for(br, mfi, ami_pct):
    """LIHTC-style max gross rent for a unit at a given AMI %.

    Income for the unit's household size (1.5 persons/BR) at ami_pct of AMI,
    times 30%, divided by 12. (Utility allowance NOT netted — gross rent.)
    """
    hh = HH_SIZE_BY_BR[br]
    income = mfi * SIZE_ADJ[hh] * ami_pct
    return income * 0.30 / 12.0


def build_context(year=2025, zip_code=None):
    m = fetch_market(year, zip_code)
    mfi = m["median_family_income"]
    bands = {}
    for br in range(5):
        bands[br] = {
            "fmr": m["fmr"][str(br)],
            "max_rent_50": round(max_rent_for(br, mfi, 0.50)),
            "max_rent_60": round(max_rent_for(br, mfi, 0.60)),
            "max_rent_80": round(max_rent_for(br, mfi, 0.80)),
        }
    return {"metro": m.get("area_name", ATLANTA_LABEL), "market": m, "rent_bands": bands,
            "incentives": [{"name": n, "status": s, "note": t} for n, s, t in INCENTIVES]}


def check_deal(deal, ctx):
    """Sanity-check a deal's proposed avg rent against FMR and AMI bands.

    Returns flags + a suggested affordability read. The deal carries one avg
    rent (the Snapshot's input granularity), so we benchmark against the 2BR
    band as the typical small-MF mix unless 'typical_bedrooms' is given.
    """
    br = int(deal.get("typical_bedrooms", 2))
    rent = deal["avg_rent_monthly"]
    band = ctx["rent_bands"][br]
    flags = []
    if rent > band["fmr"]:
        flags.append(f"Proposed rent {rent:.0f} is ABOVE the {br}BR FMR "
                     f"({band['fmr']}) — fine for market-rate, but voucher/PBRA "
                     f"tenants cap near FMR; confirm the rent roll's payer mix.")
    if rent <= band["max_rent_60"]:
        flags.append(f"Proposed rent {rent:.0f} sits at/under the 60% AMI max "
                     f"({band['max_rent_60']}) — naturally affordable; may unlock "
                     f"soft money even without an LIHTC LURA.")
    elif rent <= band["max_rent_80"]:
        flags.append(f"Proposed rent {rent:.0f} is between 60% and 80% AMI "
                     f"({band['max_rent_60']}-{band['max_rent_80']}) — workforce band.")
    else:
        flags.append(f"Proposed rent {rent:.0f} exceeds the 80% AMI max "
                     f"({band['max_rent_80']}) — market-rate; soft money unlikely "
                     f"to apply without an affordability covenant.")
    sqft = deal.get("sqft")
    if sqft and sqft >= 25_000:
        flags.append(f"CBEEO applies (~{sqft:,} sf): budget annual benchmarking + "
                     f"a Level II audit; non-compliance = $1,000/yr + disclosure.")
    if deal.get("historic") or (deal.get("year_built") and deal["year_built"] < 1936):
        flags.append("Pre-1936 / flagged historic — HTC candidate (fed 20% + GA 25%); "
                     "verify certifiability with GA SHPO before crediting it in the stack.")
    return {"benchmark_bedrooms": br, "band": band, "flags": flags}


def format_market(ctx):
    m = ctx["market"]
    L = ["=" * 72,
         "  GROUNDWORK · RESEARCHER — Market & incentive context  [DRAFT]",
         f"  {ctx['metro']}",
         f"  FY{m['year']} · 4-person AMI {m['median_family_income']:,}",
         f"  Data: {m['source']}",
         "=" * 72,
         "\n  FAIR MARKET RENT  vs  LIHTC-STYLE MAX RENT (gross, 1.5 persons/BR)",
         f"  {'Unit':<8}{'FMR':>10}{'50% AMI':>12}{'60% AMI':>12}{'80% AMI':>12}",
         "  " + "-" * 54]
    names = {0: "Studio", 1: "1 BR", 2: "2 BR", 3: "3 BR", 4: "4 BR"}
    for br in range(5):
        b = ctx["rent_bands"][br]
        L.append(f"  {names[br]:<8}{b['fmr']:>10,}{b['max_rent_50']:>12,}"
                 f"{b['max_rent_60']:>12,}{b['max_rent_80']:>12,}")
    L.append("\n  INCENTIVES & COMPLIANCE  [verify — primary sources]")
    for inc in ctx["incentives"]:
        L.append(f"     • {inc['name']}: {inc['note']}")
    L.append("\n  " + "-" * 68)
    L.append("  Max rents are gross (no utility allowance netted). All figures are")
    L.append("  ESTIMATES unless the source line above reads LIVE. Verify before use.")
    L.append("=" * 72)
    return "\n".join(L)


def format_deal_check(deal, ctx, chk):
    L = [format_market(ctx),
         "\n" + "=" * 72,
         f"  RENT REALITY CHECK · {deal.get('name', '(unnamed)')}",
         f"  Proposed avg rent {deal['avg_rent_monthly']:.0f}/mo "
         f"(benchmarked as {chk['benchmark_bedrooms']}BR)",
         "=" * 72]
    for f in chk["flags"]:
        L.append(f"     • {f}")
    L.append("=" * 72)
    return "\n".join(L)


def render_html(ctx, deal=None, chk=None):
    import gw_report as R
    m = ctx["market"]
    live = m["source"].startswith("LIVE")
    names = {0: "Studio", 1: "1 BR", 2: "2 BR", 3: "3 BR", 4: "4 BR"}
    rows = []
    for br in range(5):
        b = ctx["rent_bands"][br]
        rows.append([(names[br], "nm"), f"${b['fmr']:,}", f"${b['max_rent_50']:,}",
                     f"${b['max_rent_60']:,}", f"${b['max_rent_80']:,}"])
    headers = [("Unit", "l"), ("FMR", "r"), ("50% AMI", "r"), ("60% AMI", "r"), ("80% AMI", "r")]
    body = R.section("Fair Market Rent vs LIHTC-style max rent (gross, 1.5 persons/BR)",
                     R.table(headers, rows))
    body += R.section("Incentives & compliance — verify against primary sources",
                      R.ul([f"{i['name']}: {i['note']}" for i in ctx["incentives"]]))
    if deal and chk:
        body = R.section(f"Rent reality check · {deal.get('name','(unnamed)')} "
                         f"(${deal['avg_rent_monthly']:,.0f}/mo, benchmarked {chk['benchmark_bedrooms']}BR)",
                         R.ul(chk["flags"])) + body
    badge = ("LIVE HUD DATA", "green") if live else ("BUNDLED ESTIMATE — verify", "amber")
    kpis = [("4-person AMI", f"${m['median_family_income']:,}"), ("FY", m["year"]),
            ("Source", "HUD API" if live else "fallback")]
    return R.page("GROUNDWORK · RESEARCHER", "Market & incentive context",
                  ctx["metro"], body, badge=badge, kpis=kpis,
                  foot="Max rents are gross (no utility allowance netted). " + R.DEFAULT_FOOT)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--market", action="store_true", help="print AMI/FMR context only")
    ap.add_argument("--deal", help="path to a deal JSON file to rent-check")
    ap.add_argument("--sample", help="sample key from deal_snapshot.py (a-e)")
    ap.add_argument("--zip", dest="zip_code", help="property ZIP — routes data to its HUD area")
    ap.add_argument("--year", type=int, default=2025)
    ap.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    ap.add_argument("--html", help="write a styled HTML review doc to this path")
    a = ap.parse_args()

    deal = None
    if a.deal:
        try:
            deal = json.loads(Path(a.deal).read_text())
        except FileNotFoundError:
            sys.exit(f"Error: deal file not found: {a.deal}")
    elif a.sample:
        try:
            from deal_snapshot import SAMPLES
        except ImportError:
            sys.exit("deal_snapshot.py not importable from here; run from workspace root.")
        deal = SAMPLES[a.sample]

    zip_code = a.zip_code or (deal.get("zip") if deal else None)
    ctx = build_context(a.year, zip_code)

    if deal:
        chk = check_deal(deal, ctx)
        if a.json:
            print(json.dumps({"context": ctx, "check": chk}, indent=2))
        else:
            print(format_deal_check(deal, ctx, chk))
    else:
        if a.json:
            print(json.dumps(ctx, indent=2))
        else:
            print(format_market(ctx))

    if a.html:
        import gw_report
        Path(a.html).write_text(render_html(ctx, deal, chk if deal else None), encoding="utf-8")
        print(f"\n[HTML review doc -> {a.html}]")


if __name__ == "__main__":
    main()
