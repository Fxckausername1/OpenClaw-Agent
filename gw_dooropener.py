#!/usr/bin/env python3
"""gw_dooropener.py — Groundwork's Door-Opener agent.

Off-market lead-gen for the acquisition side: score property/owner leads on how
likely they are to sell (and to fit Groundwork's 8-15-unit value-add box), then
generate a confident, value-first outreach DRAFT for the top fits. Cloned from
the studio's outreach pipeline pattern, retuned for small-MF owners.

HARD RULE (same as the studio outreach scaffold): this generates DRAFTS ONLY.
Sending requires explicit human approval. No auto-send, no scraping here — you
feed it leads; it prioritizes and drafts.

Motivation/fit score (0-100, transparent weights in WEIGHTS):
  • absentee owner        — out-of-area owners sell more readily
  • long hold             — owned >15 yrs, low basis, tired of managing
  • building age          — older stock = value-add + possible historic
  • distress signals      — code violations / tax delinquency (handle w/ care)
  • size fit              — units land in the 8-15 box
  • condition             — deferred-maintenance / "tired" flag

Usage:
  ./venv/bin/python gw_dooropener.py --sample
  ./venv/bin/python gw_dooropener.py --leads leads.json --top 3
  ./venv/bin/python gw_dooropener.py --leads leads.json --json
"""
import argparse
import json
import sys
from datetime import date
from pathlib import Path

WEIGHTS = {"absentee": 0.20, "long_hold": 0.20, "age": 0.15,
           "distress": 0.15, "size_fit": 0.20, "condition": 0.10}
THIS_YEAR = date.today().year


def _size_fit(units):
    if 8 <= units <= 15:
        return 1.0
    if units < 8:
        return max(0.0, 1.0 - (8 - units) / 6.0)
    return max(0.0, 1.0 - (units - 15) / 10.0)


def _hold_score(last_sale_year):
    if not last_sale_year:
        return 0.5
    yrs = THIS_YEAR - last_sale_year
    return min(1.0, max(0.0, (yrs - 5) / 20.0))   # ramps 5->25 yrs held


def _age_score(year_built):
    if not year_built:
        return 0.5
    age = THIS_YEAR - year_built
    return min(1.0, max(0.0, (age - 20) / 60.0))  # ramps 20->80 yrs old


def score(lead):
    parts = {
        "absentee": 1.0 if lead.get("absentee_owner") else 0.2,
        "long_hold": _hold_score(lead.get("last_sale_year")),
        "age": _age_score(lead.get("year_built")),
        "distress": min(1.0, 0.6 * bool(lead.get("code_violations"))
                        + 0.6 * bool(lead.get("tax_delinquent"))),
        "size_fit": _size_fit(lead.get("units", 0)),
        "condition": 1.0 if lead.get("tired_condition") else 0.3,
    }
    total = sum(WEIGHTS[k] * parts[k] for k in WEIGHTS) * 100
    return total, parts


def reasons(lead, parts):
    out = []
    if lead.get("absentee_owner"):
        out.append("absentee owner")
    yrs = THIS_YEAR - lead["last_sale_year"] if lead.get("last_sale_year") else None
    if yrs and yrs >= 15:
        out.append(f"held ~{yrs} yrs (likely low basis)")
    if lead.get("year_built"):
        out.append(f"built {lead['year_built']}")
    if lead.get("code_violations"):
        out.append("open code violations")
    if lead.get("tax_delinquent"):
        out.append("tax-delinquent")
    if 8 <= lead.get("units", 0) <= 15:
        out.append(f"{lead['units']} units — in the box")
    if lead.get("tired_condition"):
        out.append("deferred maintenance")
    return out


# ---- outreach DRAFTS (value-first, confident, professional; one ask) -------
def draft_letter(lead):
    addr = lead.get("address", "your property")
    units = lead.get("units", "")
    name = lead.get("owner_name", "")
    greet = f"Dear {name}," if name else "Hello,"
    return (
        f"{greet}\n\n"
        f"I work with a small Atlanta development group that focuses on "
        f"thoughtfully renovating older {units}-unit buildings like {addr} and "
        f"keeping them in service as quality housing for the neighborhood.\n\n"
        f"We're not brokers and there's no listing involved — we buy directly, "
        f"close on your timeline, and handle the building's deferred items "
        f"ourselves. If you've ever thought about stepping back from managing it, "
        f"I'd welcome a short, no-obligation conversation about what a direct "
        f"sale could look like.\n\n"
        f"Would a brief call next week work? You can reach me at [phone] or "
        f"[email].\n\n"
        f"Warm regards,\n[Name] · Groundwork"
    )


def draft_text(lead):
    addr = lead.get("address", "your building")
    name_insert = f" {lead['owner_name']}" if lead.get("owner_name") else ""
    return (f"Hi{name_insert} — I'm with a "
            f"small Atlanta group that renovates and holds older multifamily. We'd buy "
            f"{addr} directly (no broker, your timeline). Open to a quick no-pressure "
            f"call about a possible direct sale? — [Name], Groundwork")


def format_report(rows, top):
    L = ["=" * 76,
         "  GROUNDWORK · DOOR-OPENER — off-market lead shortlist  [DRAFT]",
         "  Scored on sell-likelihood + fit. DRAFTS ONLY — approve before sending.",
         "=" * 76,
         f"  {'#':<3}{'Lead':<30}{'Score':>7}  Signals",
         "  " + "-" * 74]
    for i, r in enumerate(rows, 1):
        sig = ", ".join(r["reasons"]) or "—"
        L.append(f"  {i:<3}{r['label'][:29]:<30}{r['score']:>6.0f}  {sig}")
    L.append("  " + "-" * 74)
    L.append(f"\n  TOP {min(top, len(rows))} — OUTREACH DRAFTS (review, fill [brackets], then send manually)")
    for r in rows[:top]:
        L.append("\n  " + "=" * 72)
        L.append(f"  {r['label']}  (score {r['score']:.0f})")
        L.append("  " + "-" * 72)
        L.append("  [LETTER]")
        for ln in r["letter"].splitlines():
            L.append("    " + ln)
        L.append("\n  [TEXT/DM]")
        L.append("    " + r["text"])
    L.append("\n  " + "-" * 72)
    L.append("  Compliance: respect DNC/CAN-SPAM and any local solicitation rules.")
    L.append("  Distress signals (violations/delinquency) are leads, not leverage —")
    L.append("  lead with the value-first message. Sending stays a human decision.")
    L.append("=" * 76)
    return "\n".join(L)


SAMPLE = [  # synthetic leads — clearly not real owners
    {"label": "412 Mauldin St · 10-unit", "address": "412 Mauldin St", "owner_name": "",
     "units": 10, "year_built": 1929, "last_sale_year": 1998, "absentee_owner": True,
     "code_violations": True, "tax_delinquent": False, "tired_condition": True},
    {"label": "88 Westview Dr · 12-unit", "address": "88 Westview Dr", "owner_name": "",
     "units": 12, "year_built": 1965, "last_sale_year": 2019, "absentee_owner": False,
     "code_violations": False, "tax_delinquent": False, "tired_condition": False},
    {"label": "1500 Hank Aaron · 14-unit", "address": "1500 Hank Aaron Dr", "owner_name": "",
     "units": 14, "year_built": 1941, "last_sale_year": 1991, "absentee_owner": True,
     "code_violations": False, "tax_delinquent": True, "tired_condition": True},
    {"label": "23 Racine St · 6-unit", "address": "23 Racine St", "owner_name": "",
     "units": 6, "year_built": 1978, "last_sale_year": 2008, "absentee_owner": False,
     "code_violations": False, "tax_delinquent": False, "tired_condition": False},
]


def render_html(rows, top):
    import gw_report as R
    from html import escape
    trows = [[i, (r["label"], "nm"), f"{r['score']:.0f}", (", ".join(r["reasons"]) or "—", "l")]
             for i, r in enumerate(rows, 1)]
    body = R.section("Off-market lead shortlist — scored on sell-likelihood + fit",
                     R.table([("#", "r"), ("Lead", "l"), ("Score", "r"), ("Signals", "l")],
                             trows, top1=True))
    cards = ""
    for r in rows[:top]:
        cards += (f'<div class="draftcard"><h3>{escape(r["label"])} '
                  f'(score {r["score"]:.0f})</h3>'
                  f'<div class="lbl">Letter</div><pre>{escape(r["letter"])}</pre>'
                  f'<div class="lbl">Text / DM</div><pre>{escape(r["text"])}</pre></div>')
    body += R.section(f"Top {min(top, len(rows))} — outreach DRAFTS "
                      f"(review, fill [brackets], then send manually)", cards)
    body += R.section("Before sending",
                      R.ul(["Sending stays a human decision — these are drafts only.",
                            "Respect DNC / CAN-SPAM and local solicitation rules.",
                            "Distress signals (violations/delinquency) are leads, not leverage — "
                            "lead with the value-first message."]))
    return R.page("GROUNDWORK · DOOR-OPENER", "Off-market lead shortlist",
                  "DRAFTS ONLY — approve before any outreach is sent", body,
                  badge=("DRAFTS — DO NOT AUTO-SEND", "amber"),
                  foot="Outreach drafts for human review and manual sending. " + R.DEFAULT_FOOT)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--leads", help="path to leads JSON (list)")
    ap.add_argument("--scrape", action="store_true",
                    help="auto-source Atlanta leads from county records, then score")
    ap.add_argument("--county", default="fulton", help="county source for --scrape")
    ap.add_argument("--min-units", type=int, default=8)
    ap.add_argument("--max-units", type=int, default=15)
    ap.add_argument("--limit", type=int, default=None, help="cap scraped leads")
    ap.add_argument("--no-enrich", action="store_true", help="skip enrichment on --scrape")
    ap.add_argument("--out", default="leads.json", help="where --scrape writes the lead list")
    ap.add_argument("--sample", action="store_true")
    ap.add_argument("--top", type=int, default=2, help="how many drafts to generate")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--html", help="write a styled HTML review doc to this path")
    a = ap.parse_args()
    if a.scrape:
        try:
            import gw_scraper
        except ImportError:
            sys.exit("gw_scraper.py must sit alongside gw_dooropener.py to use --scrape.")
        leads = gw_scraper.scrape(county=a.county, min_units=a.min_units,
                                  max_units=a.max_units, limit=a.limit, out_path=a.out,
                                  enrich=not a.no_enrich)
        if not leads:
            sys.exit("Scrape returned no leads — widen --min/--max or check the source.")
        if not a.json:
            absentee = sum(1 for d in leads if d.get("absentee_owner"))
            print(f"[scraped {len(leads)} leads -> {a.out} · {absentee} absentee-owned]\n")
    elif a.leads:
        try:
            leads = json.loads(Path(a.leads).read_text())
        except FileNotFoundError:
            raise SystemExit(f"Error: leads file not found: {a.leads}")
    elif a.sample:
        leads = SAMPLE
    else:
        ap.error("give --scrape, --leads <file.json>, or --sample")
    rows = []
    for ld in leads:
        s, parts = score(ld)
        rows.append({"label": ld.get("label", ld.get("address", "(lead)")),
                     "score": s, "parts": parts, "reasons": reasons(ld, parts),
                     "letter": draft_letter(ld), "text": draft_text(ld)})
    rows.sort(key=lambda r: r["score"], reverse=True)
    if a.json:
        print(json.dumps([{k: r[k] for k in ("label", "score", "parts", "reasons", "letter", "text")}
                          for r in rows], indent=2))
    else:
        print(format_report(rows, a.top))
    if a.html:
        Path(a.html).write_text(render_html(rows, a.top), encoding="utf-8")
        print(f"\n[HTML review doc -> {a.html}]")


if __name__ == "__main__":
    main()
