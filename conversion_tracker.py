#!/usr/bin/env python3
"""Conversion tracker for the Pentagon Studios artist-leads CRM.

Reads the Google Sheet and turns the free-text Status column into a funnel:
  New -> Contacted -> Replied -> Negotiating -> Closed-Won / Closed-Lost
then breaks conversion down by Lead Source, Genre, and Fit-Score band so heff
can see WHICH leads actually convert and feed that back into scoring/triage.

Writes a Telegram-ready digest to data/conversion_report_latest.txt and prints
it. The daily wrapper sends it. This is read-only against the sheet.

CRM schema (0-indexed, A:N): 0 Artist Name | 1 City | 2 Genre | 3 IG/TikTok |
  4 Contact | 5 Song Link | 6 Followers | 7 Lead Source | 8 Fit Score |
  9 Status | 10 Outreach Angle | 11 Draft Msg | 12 Follow-up Date | 13 Notes

Run --selftest to exercise the funnel logic without touching the sheet.
Config (env or data/*.txt): GOOGLE_SA_KEY_FILE | sheet_id.txt | sheet_tab.txt
"""
import os
import sys
import json
import subprocess
from pathlib import Path
from collections import Counter, OrderedDict

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
SHEETS_MJS = ROOT / "skills" / "google-sheets-agent" / "scripts" / "sheets.mjs"
KEY_FILE = os.environ.get("GOOGLE_SA_KEY_FILE") or str(ROOT / "credentials" / "gcp_sa.json")
REPORT_PATH = DATA / "conversion_report_latest.txt"

# Ordered funnel stages. Each lead is classified into exactly one stage by
# matching its Status text against these keyword sets, checked TOP-DOWN so the
# most-advanced matching stage wins (a "signed" lead that also says "contacted"
# still counts as Won). Blank status -> New.
STAGES = OrderedDict([
    ("Closed-Won",   ["sign", "won", "paid", "deposit", "booked", "client", "closed-won", "deal"]),
    ("Closed-Lost",  ["dead", "declin", "not interest", "lost", "pass", "reject",
                      "no longer", "ghost", "unresponsive", "spam", "closed-lost"]),
    ("Negotiating",  ["negotiat", "call ", "meeting", "pricing", "quote", "sent offer",
                      "proposal", "follow", "warm"]),
    ("Replied",      ["repl", "responded", "interested", "dm back", "answered", "convo"]),
    ("Contacted",    ["contact", "dm'd", "dmed", "dm sent", "sent dm", "reached",
                      "messaged", "outreach", "pitched"]),
])
NEW_STAGE = "New"
ORDER = ["New", "Contacted", "Replied", "Negotiating", "Closed-Won", "Closed-Lost"]


def cfg(env_key, file_name, default=None):
    v = os.environ.get(env_key)
    if v:
        return v.strip()
    p = DATA / file_name
    if p.exists():
        return p.read_text().strip()
    return default


def node(args):
    env = dict(os.environ)
    env["GOOGLE_SA_KEY_FILE"] = KEY_FILE
    r = subprocess.run(["node", str(SHEETS_MJS), *args],
                       capture_output=True, text=True, env=env, timeout=120)
    if r.returncode != 0:
        raise RuntimeError(f"sheets.mjs {args[0]} failed: {r.stderr.strip()[:300]}")
    return r.stdout


def classify(status):
    s = (status or "").strip().lower()
    if not s:
        return NEW_STAGE
    for stage, kws in STAGES.items():
        if any(k in s for k in kws):
            return stage
    # Non-blank but unrecognized status -> treat as Contacted (heff touched it).
    return "Contacted"


def score_band(raw):
    try:
        v = int(float(raw))
    except (TypeError, ValueError):
        return "no score"
    if v >= 80:
        return "80-100"
    if v >= 60:
        return "60-79"
    if v >= 40:
        return "40-59"
    return "<40"


def analyze(rows):
    """rows = sheet values incl header. Returns dict of funnel stats."""
    leads = []
    for row in rows[1:]:
        row = row + [""] * (14 - len(row))
        if not (row[0] or row[3]).strip():
            continue  # skip fully empty rows
        leads.append({
            "genre": (row[2] or "").strip() or "(unknown)",
            "source": (row[7] or "").strip() or "(unknown)",
            "score": row[8],
            "stage": classify(row[9]),
        })
    total = len(leads)
    stage_counts = Counter(L["stage"] for L in leads)

    def funnel_by(key, band=False):
        out = {}
        for L in leads:
            k = score_band(L["score"]) if band else L[key]
            d = out.setdefault(k, Counter())
            d["total"] += 1
            d[L["stage"]] += 1
        return out

    return {
        "total": total,
        "stage_counts": stage_counts,
        "by_source": funnel_by("source"),
        "by_genre": funnel_by("genre"),
        "by_score": funnel_by(None, band=True),
        "contacted_plus": sum(stage_counts[s] for s in
                              ["Contacted", "Replied", "Negotiating", "Closed-Won", "Closed-Lost"]),
        "replied_plus": sum(stage_counts[s] for s in
                            ["Replied", "Negotiating", "Closed-Won"]),
        "won": stage_counts["Closed-Won"],
    }


def pct(n, d):
    return f"{(100*n/d):.0f}%" if d else "—"


def render(a):
    sc = a["stage_counts"]
    t = a["total"]
    contacted = a["contacted_plus"]
    L = [f"\U0001F4CA Pentagon funnel — {t} leads in CRM"]
    L.append("")
    # Main funnel with stage-to-stage conversion
    L.append(f"New {sc['New']}  →  Contacted {contacted}  →  "
             f"Replied {a['replied_plus']}  →  Won {a['won']}")
    L.append(f"  Contact rate: {pct(contacted, t)} of all leads")
    L.append(f"  Reply rate:   {pct(a['replied_plus'], contacted)} of contacted")
    L.append(f"  Close rate:   {pct(a['won'], a['replied_plus'])} of replied")
    if sc["Closed-Lost"]:
        L.append(f"  Dead/lost:    {sc['Closed-Lost']}")

    # Only show segment breakdowns once there's outreach data to compare.
    if contacted == 0:
        L.append("")
        L.append("No leads marked Contacted yet — fill the Status column in the "
                 "sheet (e.g. 'DM'd', 'replied', 'signed') and this report starts "
                 "showing what converts.")
        return "\n".join(L)

    def seg_block(title, d, min_total=1):
        rows = [(k, v) for k, v in d.items() if v["total"] >= min_total]
        if len(rows) < 2:
            return []
        rows.sort(key=lambda kv: -(kv[1]["Replied"] + kv[1]["Negotiating"] +
                                   kv[1]["Closed-Won"]))
        out = ["", title]
        for k, v in rows[:6]:
            warm = v["Replied"] + v["Negotiating"] + v["Closed-Won"]
            cont = v["total"] - v["New"]
            out.append(f"  {k}: {v['total']} leads · {cont} contacted · "
                       f"{warm} warm+ · {v['Closed-Won']} won")
        return out

    L += seg_block("By source:", a["by_source"])
    L += seg_block("By genre:", a["by_genre"])
    L += seg_block("By fit-score:", a["by_score"])
    return "\n".join(L)


def selftest():
    hdr = ["Artist Name", "City", "Genre", "IG/TikTok", "Contact", "Song", "Foll",
           "Lead Source", "Fit Score", "Status", "Angle", "Msg", "FU", "Notes"]
    rows = [hdr,
        ["A", "", "Rap", "@a", "", "", "", "IG hashtag scan", "85", "DM'd", "", "", "", ""],
        ["B", "", "Rap", "@b", "", "", "", "IG hashtag scan", "70", "Replied - interested", "", "", "", ""],
        ["C", "", "R&B", "@c", "", "", "", "Seed followers", "90", "Signed!", "", "", "", ""],
        ["D", "", "R&B", "@d", "", "", "", "Seed followers", "55", "dead - not interested", "", "", "", ""],
        ["E", "", "Rap", "@e", "", "", "", "IG hashtag scan", "40", "", "", "", "", ""],
        ["F", "", "Rap", "@f", "", "", "", "IG hashtag scan", "65", "negotiating pricing", "", "", "", ""],
    ]
    a = analyze(rows)
    assert a["total"] == 6, a["total"]
    assert a["stage_counts"]["New"] == 1, a["stage_counts"]
    assert a["stage_counts"]["Closed-Won"] == 1, a["stage_counts"]
    assert a["stage_counts"]["Closed-Lost"] == 1, a["stage_counts"]
    assert a["stage_counts"]["Replied"] == 1, a["stage_counts"]
    assert a["stage_counts"]["Negotiating"] == 1, a["stage_counts"]
    assert a["stage_counts"]["Contacted"] == 1, a["stage_counts"]
    assert a["contacted_plus"] == 5, a["contacted_plus"]
    assert a["won"] == 1
    assert score_band("85") == "80-100" and score_band("") == "no score"
    assert classify("") == "New" and classify("random note") == "Contacted"
    print("selftest OK")
    print(render(a))


def main():
    if "--selftest" in sys.argv:
        selftest(); return
    sid = cfg("SHEET_ID", "sheet_id.txt")
    tab = cfg("SHEET_TAB", "sheet_tab.txt", "Sheet1")
    if not sid or not Path(KEY_FILE).exists():
        print("CRM not configured; skipping."); return
    data = json.loads(node(["read", sid, f"'{tab}'!A:N"]) or "{}")
    rows = data.get("values") or []
    a = analyze(rows)
    report = render(a)
    REPORT_PATH.write_text(report)
    print(report)


if __name__ == "__main__":
    main()
