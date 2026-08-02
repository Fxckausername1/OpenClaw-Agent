#!/usr/bin/env python3
"""Daily follow-up reminders for the artist-leads CRM.

Reads the Google Sheet and flags every lead whose Follow-up Date is today or
overdue AND whose Status is not closed -> writes a Telegram message. Keeps
nagging daily until the lead's Status is set to a closed value.

Schema (user's columns, 0-indexed): 0 Artist Name | 3 IG/TikTok | 9 Status |
12 Follow-up Date | 13 Notes.

Config (env or file): GOOGLE_SA_KEY_FILE | data/sheet_id.txt | data/sheet_tab.txt
Run --selftest to exercise the matching logic without touching the sheet.
"""
import os
import re
import sys
import json
import subprocess
from pathlib import Path
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
SHEETS_MJS = ROOT / "skills" / "google-sheets-agent" / "scripts" / "sheets.mjs"
KEY_FILE = os.environ.get("GOOGLE_SA_KEY_FILE") or str(ROOT / "credentials" / "gcp_sa.json")
MSG_PATH = DATA / "followup_message_latest.txt"
ET = ZoneInfo("America/New_York")

# Status values that mean "stop reminding".
CLOSED_KW = ["sign", "dead", "declin", "not interest", "clos", "lost", "won",
             "pass", "reject", "no longer", "skip"]
HANDLE_RE = re.compile(r"instagram\.com/([A-Za-z0-9_.]+)")
AT_RE = re.compile(r"@?([A-Za-z0-9_.]+)$")
SHEET_EPOCH = date(1899, 12, 30)  # Google Sheets serial date base


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


def parse_date(raw, today):
    s = (raw or "").strip()
    if not s:
        return None
    if s.isdigit():  # Google Sheets serial number
        try:
            return SHEET_EPOCH + timedelta(days=int(s))
        except Exception:
            return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    for fmt in ("%B %d", "%b %d", "%m/%d"):  # no year -> assume this year
        try:
            return datetime.strptime(s, fmt).date().replace(year=today.year)
        except ValueError:
            pass
    try:
        from dateutil import parser as dup
        return dup.parse(s, default=datetime(today.year, 1, 1)).date()
    except Exception:
        return None


def is_closed(status):
    s = (status or "").strip().lower()
    return any(k in s for k in CLOSED_KW)


def handle_of(cell):
    c = (cell or "").strip()
    m = HANDLE_RE.search(c.lower())
    if m:
        return m.group(1)
    m = AT_RE.search(c)
    return m.group(1) if m else c


def due_followups(rows, today):
    """rows = sheet values incl header. Returns list of due reminder dicts."""
    due = []
    for row in rows[1:]:
        row = row + [""] * (14 - len(row))
        name, ig, status, fu_raw, notes = row[0], row[3], row[9], row[12], row[13]
        if is_closed(status):
            continue
        fu = parse_date(fu_raw, today)
        if fu is None or fu > today:
            continue
        due.append({
            "name": name or handle_of(ig), "handle": handle_of(ig),
            "status": status.strip() or "(no status)", "date": fu,
            "overdue": fu < today, "notes": (notes or "").strip(),
        })
    due.sort(key=lambda d: d["date"])
    return due


def render(due, today):
    lines = [f"⏰ Follow-ups due ({len(due)}) -- {today.strftime('%b %d')}:"]
    for d in due:
        tag = "overdue" if d["overdue"] else "due today"
        line = (f"• {d['name']} (@{d['handle']}) -- {d['date'].strftime('%b %d')} "
                f"({tag}) - Status: {d['status']}")
        if d["notes"]:
            line += f" - {d['notes'][:60]}"
        lines.append(line)
    return "\n".join(lines)


def selftest():
    today = date(2026, 6, 12)
    hdr = ["Artist Name", "", "", "IG/TikTok", "", "", "", "", "", "Status", "", "", "Follow-up Date", "Notes"]
    rows = [hdr,
            ["Blue Melodies", "", "", "https://www.instagram.com/bluemelodiesss", "", "", "", "", "", "Contacted", "", "", "2026-06-10", "sent free mix"],   # overdue, open -> due
            ["Vonte", "", "", "@v_tizzle", "", "", "", "", "", "Contacted", "", "", "6/12/2026", ""],                                                          # today, open -> due
            ["Big Shop", "", "", "https://www.instagram.com/shop.strong.tay", "", "", "", "", "", "Signed", "", "", "2026-06-01", ""],                          # closed -> skip
            ["Future Guy", "", "", "@futureguy", "", "", "", "", "", "Contacted", "", "", "2026-06-20", ""],                                                   # future -> skip
            ["No Date", "", "", "@nodate", "", "", "", "", "", "", "", "", "", ""]]                                                                            # no date -> skip
    due = due_followups(rows, today)
    print(f"selftest: {len(due)} due (expect 2)")
    print(render(due, today))


def main():
    if "--selftest" in sys.argv:
        selftest(); return
    sid = cfg("SHEET_ID", "sheet_id.txt")
    tab = cfg("SHEET_TAB", "sheet_tab.txt", "Sheet1")
    if not sid or not Path(KEY_FILE).exists():
        print("CRM not configured; skipping."); return

    today = datetime.now(ET).date()
    data = json.loads(node(["read", sid, f"'{tab}'!A:N"]) or "{}")
    rows = data.get("values") or []
    due = due_followups(rows, today)

    if not due:
        print("No follow-ups due today.")
        if MSG_PATH.exists():
            MSG_PATH.unlink()
        return
    msg = render(due, today)
    MSG_PATH.write_text(msg)
    print(msg)


if __name__ == "__main__":
    main()
