#!/usr/bin/env python3
"""gw_scraper.py — automated deal-sourcing front-door for Door-Opener.

Pulls small multifamily (8-15 unit) property leads for Atlanta straight from
county OPEN DATA and maps them into the lead dicts gw_dooropener.score() eats.

WHY THIS SOURCE (not Apify / Playwright / Zillow / a paid parcel API):
  Fulton County publishes its tax-assessor parcel layer as a public ArcGIS REST
  FeatureServer. It's the authoritative record, returns clean JSON, has no
  anti-bot wall, no ToS gray area (public record), and needs NO new dependency
  (stdlib urllib). A headless-browser scrape of Zillow/Realtor would be more
  fragile, slower, against ToS, and would miss owner-mailing data. So we query
  the county directly. Apify/Playwright stay the fallback only for sources that
  truly have no API.

WHAT THIS SOURCE GIVES (verified against the live layer):
  Owner name, owner MAILING address, situs (property) address, LivUnits (unit
  count — the 8-15 filter), land-use code, appraised value.
WHAT IT DOESN'T (these score neutral until another source is wired):
  year_built, last sale year, code violations, tax delinquency. Building age &
  sale history would come from the assessor's CAMA/sales export; violations from
  Atlanta 311 / code-enforcement open data; delinquency from the Tax Commissioner.

absentee_owner is DERIVED here: if the owner's mailing street != the property's
situs street, the owner doesn't live there -> absentee (the strongest off-market
signal). Everything downstream is still DRAFTS ONLY — see gw_dooropener.

Compliance: public-record data, single rate-limited API, no PII beyond what the
county already publishes. Respect each source's terms; sending stays human-gated.

Usage:
  ./venv/bin/python gw_scraper.py                         # write leads.json (Fulton 8-15u)
  ./venv/bin/python gw_scraper.py --min 8 --max 15 --limit 50 --out leads.json
  ./venv/bin/python gw_scraper.py --count                 # just how many match
"""
import argparse
import json
import re
import sys
from urllib import request, parse, error

# ---- verified source registry (add metros/counties here as the practice grows)
SOURCES = {
    "fulton": {
        "name": "Fulton County, GA (tax assessor parcels)",
        "url": "https://gismaps.fultoncountyga.gov/arcgispub2/rest/services/"
               "PropertyMapViewer/PropertyMapViewer/MapServer/11/query",
        "fields": {"pid": "ParcelID", "situs": "Address", "owner": "Owner",
                   "mail1": "OwnerAddr1", "mail2": "OwnerAddr2", "units": "LivUnits",
                   "lucode": "LUCode", "classcode": "ClassCode", "appr": "TotAppr"},
        "page": 1000,   # well under the layer's 2000 maxRecordCount
    },
    # "dekalb": {...}  # TODO: DeKalb publishes its own parcel service; different
    #                  # schema — add once verified (intown east of the Fulton line).
}

_SUFFIX = {"STREET": "ST", "AVENUE": "AVE", "ROAD": "RD", "DRIVE": "DR",
           "BOULEVARD": "BLVD", "LANE": "LN", "COURT": "CT", "PLACE": "PL",
           "TERRACE": "TER", "PARKWAY": "PKWY", "HIGHWAY": "HWY", "CIRCLE": "CIR",
           "NORTH": "N", "SOUTH": "S", "EAST": "E", "WEST": "W"}


def _norm_street(s):
    """Normalize a street address to a comparable core (number + name + suffix),
    dropping unit/suite tails and standardizing suffix words/directionals."""
    if not s:
        return ""
    s = re.sub(r"[^A-Za-z0-9 ]", " ", s.upper())
    s = re.sub(r"\b(STE|SUITE|APT|UNIT|FL|FLOOR|RM|ROOM|#|PO BOX|BOX)\b.*$", "", s)
    toks = [_SUFFIX.get(t, t) for t in s.split()]
    return " ".join(toks).strip()


def derive_absentee(situs, mailing_street):
    """True if owner mails elsewhere than the property sits. None if undeterminable."""
    ns, nm = _norm_street(situs), _norm_street(mailing_street)
    if not ns or not nm:
        return None
    return ns != nm


def _clean_name(n):
    """Title-case an owner name while keeping entity suffixes upper (LLC, INC...)."""
    if not n:
        return ""
    t = " ".join(w.capitalize() for w in n.split())
    return re.sub(r"\b(Llc|Inc|Lp|Llp|Lllp|Co|Corp|Ltd|Na)\b",
                  lambda m: m.group(1).upper(), t)


def _arcgis(url, params):
    q = parse.urlencode(params)
    try:
        with request.urlopen(f"{url}?{q}", timeout=30) as r:
            return json.loads(r.read().decode("utf-8"))
    except (error.URLError, TimeoutError) as e:
        sys.exit(f"County API request failed ({type(e).__name__}: {e}). "
                 f"Check connectivity / the source URL.")


def count_matches(source, min_units, max_units):
    F = source["fields"]
    d = _arcgis(source["url"], {
        "where": f"{F['units']}>={min_units} AND {F['units']}<={max_units}",
        "returnCountOnly": "true", "f": "json"})
    return d.get("count", 0)


def fetch_parcels(source, min_units, max_units, limit=None):
    F = source["url"], source["fields"]
    url, fld = F
    where = f"{fld['units']}>={min_units} AND {fld['units']}<={max_units}"
    out_fields = ",".join(fld[k] for k in ("pid", "situs", "owner", "mail1",
                                           "mail2", "units", "lucode", "classcode", "appr"))
    rows, offset, page = [], 0, source["page"]
    while True:
        d = _arcgis(url, {"where": where, "outFields": out_fields,
                          "returnGeometry": "false", "f": "json",
                          "resultOffset": offset, "resultRecordCount": page})
        feats = d.get("features", [])
        rows.extend(f["attributes"] for f in feats)
        if limit and len(rows) >= limit:
            return rows[:limit]
        if len(feats) < page or not d.get("exceededTransferLimit"):
            break
        offset += len(feats)
    return rows


def to_lead(attrs, fld):
    situs = (attrs.get(fld["situs"]) or "").strip()
    owner_raw = (attrs.get(fld["owner"]) or "").strip()
    mail1 = (attrs.get(fld["mail1"]) or "").strip()
    mail2 = (attrs.get(fld["mail2"]) or "").strip()
    units = attrs.get(fld["units"]) or 0
    absentee = derive_absentee(situs, mail1)
    return {
        "label": f"{situs.title()} · {units}-unit" if situs else f"Parcel {attrs.get(fld['pid'],'?')}",
        "address": situs.title(),
        "owner_name": _clean_name(owner_raw),
        "units": int(units),
        "absentee_owner": bool(absentee),       # None -> False (undeterminable)
        # --- not in this source; left neutral so scoring stays honest ---
        "year_built": None,
        "last_sale_year": None,
        "code_violations": False,
        "tax_delinquent": False,
        "tired_condition": False,
        # --- preserved context for the human reviewer ---
        "_owner_mailing": f"{mail1}, {mail2}".strip(", "),
        "_appraised_value": attrs.get(fld["appr"]),
        "_land_use_code": attrs.get(fld["lucode"]),
        "_parcel_id": attrs.get(fld["pid"]),
        "_source": "Fulton County open data",
        "_absentee_determinable": absentee is not None,
    }


def scrape(county="fulton", min_units=8, max_units=15, limit=None,
           out_path="leads.json", enrich=True, sleep=0.0):
    if county not in SOURCES:
        sys.exit(f"Unknown county '{county}'. Available: {', '.join(SOURCES)}")
    src = SOURCES[county]
    rows = fetch_parcels(src, min_units, max_units, limit)
    leads = [to_lead(a, src["fields"]) for a in rows]
    leads = [d for d in leads if d["address"]]            # drop record with no situs
    if enrich:                                            # only enrich the kept leads
        try:
            import gw_enrich
            gw_enrich.enrich_all(leads, sleep=sleep)
        except ImportError:
            pass                                          # enrichment optional; scrape still works
    if out_path:
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(leads, f, indent=2)
    return leads


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--county", default="fulton", choices=list(SOURCES))
    ap.add_argument("--min", type=int, default=8, dest="min_units")
    ap.add_argument("--max", type=int, default=15, dest="max_units")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--out", default="leads.json")
    ap.add_argument("--no-enrich", action="store_true", help="skip the enrichment layer")
    ap.add_argument("--sleep", type=float, default=0.0, help="seconds between enrichment calls")
    ap.add_argument("--count", action="store_true", help="just print the match count")
    a = ap.parse_args()
    src = SOURCES[a.county]
    if a.count:
        print(f"{count_matches(src, a.min_units, a.max_units)} parcels with "
              f"{a.min_units}-{a.max_units} units in {src['name']}")
        return
    leads = scrape(a.county, a.min_units, a.max_units, a.limit, a.out,
                   enrich=not a.no_enrich, sleep=a.sleep)
    absentee = sum(1 for d in leads if d["absentee_owner"])
    sold = sum(1 for d in leads if d.get("last_sale_year"))
    extra = "" if a.no_enrich else f", {sold} with a recent recorded sale"
    print(f"[scraped {len(leads)} leads -> {a.out}]  "
          f"({absentee} absentee-owned{extra})")


if __name__ == "__main__":
    main()
