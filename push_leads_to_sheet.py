#!/usr/bin/env python3
"""Push artist leads into the user's existing Google Sheet CRM via the
google-sheets-agent skill (sheets.mjs, service-account auth).

Maps pipeline leads into the user's OWN 14-column schema (do not change the
order without updating the sheet header):
  Artist Name | City Area | Genre | IG/TikTok | Email/Phone/Booking Contact |
  Best Song Link | Follower/Listener Counts | Lead Source | Fit Score |
  Status | Outreach Angle | Draft Message | Follow-up Date | Notes

Only NEW artists are appended (dedup by IG handle in the IG/TikTok column), so
re-running never duplicates and the user's Status / Follow-up / Notes edits are
never overwritten.

Config (file or env):
  Service-account key : env GOOGLE_SA_KEY_FILE or credentials/gcp_sa.json
  Spreadsheet id      : env SHEET_ID or data/sheet_id.txt
  Tab name            : env SHEET_TAB or data/sheet_tab.txt (default 'Sheet1')
Exits 0 quietly if key or sheet id is missing.
"""
import os
import re
import csv
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
SHEETS_MJS = ROOT / "skills" / "google-sheets-agent" / "scripts" / "sheets.mjs"
MASTER = DATA / "leads_master.csv"
KEY_FILE = os.environ.get("GOOGLE_SA_KEY_FILE") or str(ROOT / "credentials" / "gcp_sa.json")

HANDLE_RE = re.compile(r"instagram\.com/([A-Za-z0-9_.]+)")
AT_RE = re.compile(r"@([A-Za-z0-9_.]+)")


def cfg(env_key, file_name, default=None):
    v = os.environ.get(env_key)
    if v:
        return v.strip()
    p = DATA / file_name
    if p.exists():
        return p.read_text().strip()
    return default


def node(args, stdin=None):
    env = dict(os.environ)
    env["GOOGLE_SA_KEY_FILE"] = KEY_FILE
    r = subprocess.run(["node", str(SHEETS_MJS), *args],
                       input=stdin, capture_output=True, text=True, env=env, timeout=120)
    if r.returncode != 0:
        raise RuntimeError(f"sheets.mjs {args[0]} failed: {r.stderr.strip()[:300]}")
    return r.stdout


def handle_of(cell):
    c = (cell or "").strip().lower()
    m = HANDLE_RE.search(c) or AT_RE.search(c)
    return m.group(1) if m else c


def to_row(r):
    """Map a pipeline lead dict to the user's 14-column schema."""
    verified = str(r.get("verified", "")).lower() in ("true", "1", "yes")
    return [
        r.get("fullName", ""),                       # Artist Name
        "",                                          # City Area (unknown)
        r.get("category", ""),                       # Genre (category as proxy)
        r.get("profileUrl", ""),                     # IG/TikTok
        r.get("email", ""),                          # Email/Phone/Booking Contact
        r.get("externalUrl", ""),                    # Best Song Link
        r.get("followers", ""),                      # Follower/Listener Counts
        r.get("source") or "Unknown (pre-migration)",  # Lead Source
        r.get("score", ""),                          # Fit Score
        "",                                          # Status (user-edited)
        "Free sample mix",                           # Outreach Angle
        r.get("drafted_dm", ""),                     # Draft Message
        "",                                          # Follow-up Date (user-edited)
        ("; ".join(x for x in [r.get("audio", ""), ("verified" if verified else "")] if x)),  # Notes
    ]


def main():
    sid = cfg("SHEET_ID", "sheet_id.txt")
    tab = cfg("SHEET_TAB", "sheet_tab.txt", "Sheet1")
    if not sid or not Path(KEY_FILE).exists():
        print("Google Sheet CRM not configured (need SA key + sheet id); skipping.")
        return
    rng = f"'{tab}'!A:N"

    try:
        data = json.loads(node(["read", sid, rng]) or "{}")
        existing = data.get("values") or []
    except Exception as e:
        print(f"read failed: {e}")
        existing = []

    seen = {handle_of(row[3]) for row in existing[1:] if len(row) > 3 and row[3]}

    if not MASTER.exists():
        print("No leads_master.csv yet; nothing to push.")
        return

    payload, added = [], 0
    with MASTER.open() as f:
        for r in csv.DictReader(f):
            h = (r.get("handle") or "").strip().lower()
            if not h or h in seen:
                continue
            seen.add(h)
            payload.append(to_row(r))
            added += 1

    if not payload:
        print("Sheet already up to date; nothing to append.")
        return

    node(["append", sid, rng], stdin=json.dumps(payload))
    print(f"Appended {added} new lead(s) to '{tab}'.")


if __name__ == "__main__":
    main()
