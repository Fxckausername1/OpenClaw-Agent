"""catalyst_news_pull.py -- free, no-paid-dependency "why did this move" attribution
pull for the tracked universe. Complements confluence_score.py's "what's unusual"
tally with real primary-source events -- closes the gap catalyst_alert.py explicitly
left open (it flags names worth a news check but deliberately never fetches news
itself, to avoid a new paid API dependency -- see build-tough-data-independence).

Sources (verified live 2026-07-04, all free, all keyless):
- SEC EDGAR 8-K firehose ("getcurrent" Atom feed) -- CIK-matched against the FULL
  tracked universe (194 tickers, same TICKERS as confluence_score.py; 181 of those
  actually have a CIK -- the remaining 13 are sector ETFs, which don't file 8-Ks, so
  that gap is expected, not a bug). One feed call covers every filer, so scoping to
  the whole universe costs nothing extra. Requires a descriptive User-Agent per SEC's
  fair-use policy -- unidentified requests get blocked, this is not optional.
  EDGAR_USER_AGENT below is a placeholder identifying string (SEC's own stated
  purpose is just "who to contact if something's wrong", not proof of identity) --
  swap in a real contact if heff wants one on file.
  KNOWN COVERAGE CAP, not yet fixed: the endpoint silently ignores a requested
  count above ~100 and just returns its own default -- confirmed live (asked for
  400, got 100). On a high-volume trading day, 100 most-recent filings ACROSS ALL OF
  EDGAR (not just our universe) could scroll past before a poll catches it, missing a
  tracked name entirely. The per-CIK submissions API (data.sec.gov/submissions/
  CIK##########.json, already proven working during research) would guarantee full
  coverage instead, at the cost of ~181 calls instead of 1 -- still well inside SEC's
  <=10 req/s limit, just not built here since this is the validation pass, not the
  production hardening pass.
- openFDA drug enforcement/recalls -- universe-wide (one date-filtered call), fuzzy
  company-name match against tracked tickers' legal names. Only ever fires for
  pharma-adjacent names; that's expected, not a bug.
- Federal Register full-text search -- SCOPED to today's confluence_score.json
  strong-confluence names only, NOT the full universe. One search call per company
  name is too heavy to run against all 194 every poll; scoping it to whatever
  confluence already flagged as unusual is the actual point (confluence says WHAT'S
  odd, this says WHY) rather than an arbitrary cost-cutting shortcut.

KNOWN, NOT FULLY FIXABLE LIMITATION -- Federal Register results specifically, marked
"source_confidence": "low" in the output (EDGAR and openFDA are both exact/structured
matches -- CIK and a dedicated company-name field respectively -- and stay "high"):
full-text search against a company's LEGAL NAME misfires whenever that name is also a
common English word or phrase, and this can't be pattern-excluded away the way the two
mechanical false-positive patterns below were. Confirmed live: Carnival Corp (CCL)
matched a Live Nation antitrust filing that mentions "Electric Daisy Carnival" (a music
festival), nothing to do with the cruise line -- there's no fixed grammatical pattern to
exclude here, "carnival" the word and "Carnival" the company are genuinely
indistinguishable to a keyword search. Treat Federal Register hits as "worth a 10-second
human glance," not confirmed-relevant, especially for tickers whose name is a common
noun. Two DIFFERENT, mechanical false-positive patterns WERE fixed (see
fetch_federal_register_for_company): FERC's boilerplate "...PDF and Microsoft Word
format..." footer, and "United States v. <Company>" legal-citation grammar (both hit
MSFT during testing) -- those are fixed because they're recognizable, fixed
constructions, unlike the Carnival case.

NOT wired here -- confirmed blocked from this box's IP (Akamai bot-protection, 401/403
on every UA tried) or no discoverable feed found: DOJ, defense.gov, FTC. Documented
here so a future session doesn't re-attempt the same dead ends; not silently dropped
from the source map.

Dedup: EDGAR accession numbers / FDA recall+event IDs / FR document numbers already
surfaced are cached in CATALYST_SEEN_PATH (rolling 14-day window) so re-running this
doesn't re-alert on the same filing every poll -- same spam-prevention discipline as
the WSS Telegram digest fix earlier this session.
"""
import json
import re
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

from put_call_ratio import TICKERS
from confluence_score import CONFLUENCE_JSON_PATH

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"

EDGAR_USER_AGENT = "TradingBotLocal-research contact@heff-tradingbot.local"
CIK_MAP_PATH = DATA / "company_tickers_cache.json"
CIK_MAP_MAX_AGE_DAYS = 7  # ticker<->CIK mapping changes rarely; no need to refetch every run

CATALYST_SEEN_PATH = DATA / "catalyst_seen_ids.json"
SEEN_WINDOW_DAYS = 14

CATALYST_OUT_PATH = DATA / "catalyst_news.json"
CONVICTION_OUT_PATH = DATA / "confluence_with_conviction.json"

FED_REGISTER_LOOKBACK_DAYS = 3


def _fetch_json(url, headers=None, timeout=15):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def _fetch_text(url, headers=None, timeout=15):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode()


def load_cik_map():
    """ticker -> (cik_int, company_title), restricted to TICKERS. Cached to disk --
    SEC's own company_tickers.json is ~800KB and this mapping barely ever changes, so
    refetching every poll would be pure waste (and needless load on SEC's server)."""
    if CIK_MAP_PATH.exists():
        age_days = (time.time() - CIK_MAP_PATH.stat().st_mtime) / 86400
        if age_days < CIK_MAP_MAX_AGE_DAYS:
            raw = json.loads(CIK_MAP_PATH.read_text())
            return {t: tuple(v) for t, v in raw.items()}

    data = _fetch_json(
        "https://www.sec.gov/files/company_tickers.json",
        headers={"User-Agent": EDGAR_USER_AGENT},
    )
    by_ticker = {}
    tracked = set(TICKERS)
    for row in data.values():
        t = row.get("ticker")
        if t in tracked:
            by_ticker[t] = (int(row["cik_str"]), row.get("title", ""))

    DATA.mkdir(parents=True, exist_ok=True)
    CIK_MAP_PATH.write_text(json.dumps({t: list(v) for t, v in by_ticker.items()}))
    return by_ticker


def fetch_edgar_8k(limit=200):
    """Recent 8-K filings across ALL EDGAR filers (not just our universe) -- filtering
    happens after the fetch, against the CIK map. One call, cheap, covers everyone."""
    url = (
        "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=8-K"
        f"&company=&dateb=&owner=include&count={limit}&output=atom"
    )
    xml_text = _fetch_text(url, headers={"User-Agent": EDGAR_USER_AGENT})
    ns = {"a": "http://www.w3.org/2005/Atom"}
    root = ET.fromstring(xml_text)

    rows = []
    for entry in root.findall("a:entry", ns):
        title = (entry.findtext("a:title", default="", namespaces=ns) or "").strip()
        link_el = entry.find("a:link", ns)
        href = link_el.get("href") if link_el is not None else ""
        summary = entry.findtext("a:summary", default="", namespaces=ns) or ""

        cik_match = re.search(r"/data/(\d+)/", href)
        if not cik_match:
            continue
        cik = int(cik_match.group(1))

        filed_match = re.search(r"Filed:</b>\s*([\d-]+)", summary)
        accno_match = re.search(r"AccNo:</b>\s*([\d-]+)", summary)
        items = re.findall(r"Item ([\d.]+):", summary)

        company = re.sub(r"^8-K\s*-\s*", "", title)
        company = re.sub(r"\s*\(\d+\)\s*\(Filer\)\s*$", "", company)

        rows.append({
            "cik": cik,
            "company": company,
            "filed": filed_match.group(1) if filed_match else None,
            "accno": accno_match.group(1) if accno_match else None,
            "items": items,
            "link": href,
        })
    return rows


def fetch_fda_recalls(days=7):
    """Recent drug enforcement reports (recalls) -- universe-wide, one call. Matched
    against tracked company names by the caller (fuzzy substring, see _company_matches)."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y%m%d")
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    # Lucene range syntax wants a literal space around TO -- quote() the WHOLE query
    # after building it with real spaces, don't hand-insert "+" yourself first (that
    # double-encodes into %2B and openFDA 500s on it instead of parsing the range).
    q = urllib.parse.quote(f"report_date:[{since} TO {today}]")
    url = f"https://api.fda.gov/drug/enforcement.json?search={q}&limit=100"
    try:
        data = _fetch_json(url)
    except urllib.error.HTTPError as e:
        if e.code == 404:  # openFDA 404s on a query with zero matches -- not an error
            return []
        raise
    return data.get("results", [])


def _clean_company_name(name):
    """Strips common legal-entity suffixes and EDGAR's '/DE'-style state-of-
    incorporation tag so 'APPLIED MATERIALS INC /DE' and 'Applied Materials, Inc.'
    both reduce to 'applied materials' for substring matching."""
    n = re.sub(r"/[A-Z]{2}$", "", name.strip()).lower()  # trailing "/DE" etc.
    n = n.replace(",", "")
    for suffix in (" corporation", " corp.", " corp", " inc.", " inc", " co.", " co",
                   " ltd.", " ltd", " plc", " llc", " l.l.c.", " holdings", " group"):
        n = n.replace(suffix, "")
    return n.strip()


def _company_matches(candidate_name, tracked_name):
    """Substring match, guarded against short/generic cleaned names (same
    MIN_SEARCHABLE_NAME_LEN risk as the Federal Register audit note below -- a 3-4
    char cleaned name like 'at&t' would substring-match almost anything by
    coincidence in a large recall-firm field)."""
    a, b = _clean_company_name(candidate_name), _clean_company_name(tracked_name)
    if len(a) < MIN_SEARCHABLE_NAME_LEN or len(b) < MIN_SEARCHABLE_NAME_LEN:
        return False
    return a in b or b in a


# Real audit finding (2026-07-04): Federal Register's full-text search does loose
# word-overlap matching, not phrase matching, on an unquoted term -- "APPLIED
# MATERIALS INC /DE" pulled back unrelated notices that merely contained the word
# "materials" anywhere (home health, hazardous materials, controlled substances).
# Quoting the term forces phrase matching and returns genuinely relevant results
# (real FTZ applications / HSR filings naming Applied Materials specifically) --
# BUT short/common names still misfire even quoted: "AT&T" matched a document whose
# only "hit" was the fragments "at" and "t" from an unrelated email address wrapped
# across a line. Guarded two ways: (1) skip the lookup entirely for cleaned names
# under MIN_SEARCHABLE_NAME_LEN chars (catches AT&T-style abbreviations), (2) require
# the cleaned name to actually appear as a contiguous substring in the returned
# title+excerpt before accepting a match, regardless of what the API itself ranked --
# a hard client-side confirmation rather than trusting server-side relevance alone.
MIN_SEARCHABLE_NAME_LEN = 6


def fetch_federal_register_for_company(company_name, days=FED_REGISTER_LOOKBACK_DAYS):
    """Full-text search scoped to one company name, last N days. Deliberately only
    called for today's confluence strong-confluence names (see module docstring) --
    one call per tracked ticker would be excessive against the full 194-name universe."""
    cleaned = _clean_company_name(company_name)
    if len(cleaned) < MIN_SEARCHABLE_NAME_LEN:
        return []

    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    params = urllib.parse.urlencode({
        "conditions[term]": f'"{cleaned}"',
        "conditions[publication_date][gte]": since,
        "per_page": 5,
        "order": "newest",
    })
    url = f"https://www.federalregister.gov/api/v1/documents.json?{params}"
    data = _fetch_json(url)
    results = data.get("results", [])

    # client-side confirmation (see the audit note above) -- don't trust the API's
    # own relevance ranking alone; require the cleaned name to actually appear as a
    # contiguous phrase, whitespace-normalized, in what came back. Real second-order
    # finding: even a genuine phrase match can still be pure noise for a ubiquitous
    # name -- "Microsoft" hit FERC boilerplate ("...available in PDF and Microsoft
    # Word format...") and a 1995 antitrust case cited as precedent in an unrelated
    # Live Nation matter ("United States v. Microsoft Corp., 56 F.3d..."). Both are
    # mechanical, recognizable patterns (a fixed file-format phrase; legal-citation
    # grammar), not genuinely ambiguous judgment calls -- excluded specifically,
    # not via a broad/fragile keyword blacklist.
    confirmed = []
    for doc in results:
        raw = f"{doc.get('title', '')} {doc.get('excerpts', '')}"
        # the API wraps matched terms in <span class="match">...</span>, sometimes
        # splitting mid-word ("<span class=\"match\">Microso</span>ft") -- strip tags
        # BEFORE any substring check, or both the confirmation and the exclusion
        # checks below silently fail to see the word they're looking for.
        raw = re.sub(r"<[^>]+>", "", raw)
        haystack = re.sub(r"\s+", " ", raw.lower())
        if cleaned not in haystack:
            continue
        if f"{cleaned} word format" in haystack:
            continue
        if re.search(rf"\bv\.?\s+{re.escape(cleaned)}\b", haystack):
            continue
        confirmed.append(doc)
    return confirmed


def load_seen_ids():
    if not CATALYST_SEEN_PATH.exists():
        return {}
    raw = json.loads(CATALYST_SEEN_PATH.read_text())
    cutoff = (datetime.now(timezone.utc) - timedelta(days=SEEN_WINDOW_DAYS)).isoformat()
    return {k: v for k, v in raw.items() if v > cutoff}


def save_seen_ids(seen):
    DATA.mkdir(parents=True, exist_ok=True)
    CATALYST_SEEN_PATH.write_text(json.dumps(seen))


def load_confluence_tickers():
    """Today's strong-confluence names, for scoping the Federal Register search.
    Returns {} gracefully if confluence hasn't run yet / is stale-unavailable --
    Federal Register just gets skipped that run, not treated as an error."""
    if not CONFLUENCE_JSON_PATH.exists():
        return []
    data = json.loads(CONFLUENCE_JSON_PATH.read_text())
    return [r["ticker"] for r in data.get("rows", [])]


def compute_catalysts():
    cik_map = load_cik_map()
    cik_to_ticker = {cik: t for t, (cik, _title) in cik_map.items()}
    name_by_ticker = {t: title for t, (_cik, title) in cik_map.items()}

    seen = load_seen_ids()
    now_iso = datetime.now(timezone.utc).isoformat()
    by_ticker = {t: [] for t in TICKERS}

    # --- EDGAR 8-K, universe-wide ---
    for row in fetch_edgar_8k():
        ticker = cik_to_ticker.get(row["cik"])
        if not ticker:
            continue
        seen_id = f"edgar:{row['accno']}"
        if seen_id in seen:
            continue
        seen[seen_id] = now_iso
        by_ticker[ticker].append({
            "source": "EDGAR 8-K", "source_confidence": "high",
            "headline": f"{row['company']} -- Item(s) {', '.join(row['items']) or 'n/a'}",
            "link": row["link"], "filed": row["filed"],
        })

    # --- openFDA recalls, universe-wide, fuzzy name match ---
    for rec in fetch_fda_recalls():
        firm = rec.get("recalling_firm", "")
        for t, title in name_by_ticker.items():
            if _company_matches(firm, title):
                seen_id = f"fda:{rec.get('recall_number')}:{rec.get('event_id')}"
                if seen_id in seen:
                    continue
                seen[seen_id] = now_iso
                by_ticker[t].append({
                    "source": "openFDA recall", "source_confidence": "high",
                    "headline": f"{firm} -- {rec.get('reason_for_recall', '')[:140]}",
                    "link": f"https://www.accessdata.fda.gov/scripts/ires/index.cfm?query={rec.get('recall_number', '')}",
                    "filed": rec.get("report_date"),
                })
                break  # one match is enough, don't also test the reversed-name edge case

    # --- Federal Register, scoped to today's strong-confluence names only ---
    for t in load_confluence_tickers():
        title = name_by_ticker.get(t)
        if not title:
            continue
        try:
            for doc in fetch_federal_register_for_company(title):
                seen_id = f"fr:{doc.get('document_number')}"
                if seen_id in seen:
                    continue
                seen[seen_id] = now_iso
                by_ticker[t].append({
                    "source": "Federal Register", "source_confidence": "low",
                    "headline": doc.get("title"),
                    "link": doc.get("html_url"), "filed": doc.get("publication_date"),
                })
        except Exception as e:
            print(f"  [warn] Federal Register lookup failed for {t}: {e!r}")

    save_seen_ids(seen)
    return {t: items for t, items in by_ticker.items() if items}


def save_catalysts(new_by_ticker):
    """Merges newly-found events into the EXISTING catalyst_news.json rather than
    overwriting it -- by_ticker from compute_catalysts() only contains events not
    already in the seen-cache (see load_seen_ids), so a real catalyst found on a
    PRIOR run would otherwise silently vanish from the output the moment a later run
    passes without a fresh hit for that ticker, which would have made the conviction-
    tagging feature wrong (a filing from 2 days ago is still a real reason, not
    stale). Prunes entries older than SEEN_WINDOW_DAYS by their own 'filed' date so
    this doesn't grow unbounded."""
    existing = {}
    if CATALYST_OUT_PATH.exists():
        existing = json.loads(CATALYST_OUT_PATH.read_text()).get("tickers", {})

    merged = {t: list(events) for t, events in existing.items()}
    for t, events in new_by_ticker.items():
        merged.setdefault(t, []).extend(events)

    cutoff = (datetime.now(timezone.utc) - timedelta(days=SEEN_WINDOW_DAYS)).date().isoformat()
    pruned = {}
    for t, events in merged.items():
        kept = [e for e in events if (e.get("filed") or "9999") >= cutoff]
        if kept:
            pruned[t] = kept

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "tickers": pruned,
    }
    DATA.mkdir(parents=True, exist_ok=True)
    CATALYST_OUT_PATH.write_text(json.dumps(payload, indent=2))
    return CATALYST_OUT_PATH


def load_confluence_rows():
    """Full confluence rows (not just tickers -- need lean/n_bull/n_bear for the
    conviction tag to mean anything), same graceful-empty behavior as
    load_confluence_tickers() if confluence hasn't run yet."""
    if not CONFLUENCE_JSON_PATH.exists():
        return []
    data = json.loads(CONFLUENCE_JSON_PATH.read_text())
    return data.get("rows", [])


def compute_conviction():
    """Cross-references confluence_score.json's strong-confluence tickers against
    catalyst_news.json's HIGH-confidence events only (EDGAR/openFDA -- Federal
    Register is deliberately excluded here, matching heff's explicit decision to keep
    it out of anything dashboard-facing given its structural false-positive risk for
    dictionary-word company names, e.g. Carnival/CCL).

    This is NOT a new directional vote alongside Ghost Wall/put-call/sweep/etc -- an
    8-K's mere existence isn't bullish or bearish, and treating "a filing exists" as a
    vote would repeat the exact raw-sign mistake already caught twice building
    confluence_score.py itself. It's a CONFIDENCE tag on confluence's EXISTING lean:
    "high" when a real, verifiable public event exists alongside the flow signal,
    "flow-only" when the signal has no findable public reason yet -- not wrong, just
    a different risk profile (pure positioning/sector sympathy, or informed money
    moving ahead of news that isn't public yet).
    """
    rows = load_confluence_rows()
    if not rows:
        return []

    catalysts_by_ticker = {}
    if CATALYST_OUT_PATH.exists():
        catalysts_by_ticker = json.loads(CATALYST_OUT_PATH.read_text()).get("tickers", {})

    tagged = []
    for r in rows:
        events = [e for e in catalysts_by_ticker.get(r["ticker"], [])
                  if e.get("source_confidence") == "high"]
        tagged.append({**r, "conviction": "high" if events else "flow-only", "catalyst_events": events})
    return tagged


def save_conviction(tagged_rows):
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "rows": tagged_rows,
    }
    DATA.mkdir(parents=True, exist_ok=True)
    CONVICTION_OUT_PATH.write_text(json.dumps(payload, indent=2))
    return CONVICTION_OUT_PATH


def main():
    by_ticker = compute_catalysts()
    out_path = save_catalysts(by_ticker)
    n_events = sum(len(v) for v in by_ticker.values())
    print(f"wrote {n_events} new catalyst events across {len(by_ticker)} tickers -> {out_path}")
    for t, events in sorted(by_ticker.items()):
        for e in events:
            print(f"  {t:6} [{e['source']:16}] {e['filed']}  {e['headline'][:90]}")

    tagged = compute_conviction()
    conviction_path = save_conviction(tagged)
    n_high = sum(1 for r in tagged if r["conviction"] == "high")
    print(f"\nconviction: {n_high}/{len(tagged)} strong-confluence tickers have a "
          f"high-confidence catalyst -> {conviction_path}")
    for r in tagged:
        if r["conviction"] == "high":
            print(f"  {r['ticker']:6} {r['lean']:8} HIGH CONVICTION -- {len(r['catalyst_events'])} event(s)")


if __name__ == "__main__":
    main()
