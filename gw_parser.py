#!/usr/bin/env python3
"""gw_parser.py — file front-door for Build-Watcher.

Reads a real AIA G703 continuation sheet from Excel (.xlsx), CSV, or PDF and
maps it into the JSON `draw` dict that gw_buildwatch.audit() consumes. Column
detection is keyword-based and tolerant of the usual AIA layout variation, so
it doesn't depend on exact column order.

The standard G703 columns it looks for:
  A  Item No.            -> (part of) item label
  B  Description of Work -> item label
  C  Scheduled Value     -> scheduled_value      (required to count as a line)
  D  From Previous Appl. -> from_previous
  E  This Period         -> this_period
  F  Materials Stored    -> materials_stored
  G  Total Completed &   -> completed_to_date     (stated; audited vs D+E+F)
     Stored To Date
  I  Balance To Finish   -> balance_to_finish      (stated; audited vs C-G)

G702-summary fields (retainage, previous payments, current payment due) live on
the G702 cover page, not the continuation sheet, so they're taken as arguments
(retainage_pct defaults to 0.10). A bare G703 still gets a full per-line audit;
the stated-vs-calc summary checks just don't fire without a G702.

Dependencies (lazy-imported, only for the format used):
  .xlsx -> openpyxl     .pdf -> pdfplumber     .csv -> stdlib

Usage:
  ./venv/bin/python gw_parser.py payapp.xlsx                 # dump parsed JSON
  ./venv/bin/python gw_parser.py --make-sample test.xlsx     # write a synthetic G703
  ./venv/bin/python gw_parser.py --make-sample clean.xlsx --clean
"""
import argparse
import csv
import json
import sys
from pathlib import Path

# field -> ordered predicates on a lowercased header cell. First field that
# matches an as-yet-unassigned column wins that column.
_HEADER_RULES = [
    ("scheduled_value", lambda h: "scheduled" in h),
    ("from_previous", lambda h: "previous" in h),
    ("this_period", lambda h: "this period" in h or h.strip() in ("d+e", "this application")),
    ("completed_to_date", lambda h: "total completed" in h or ("completed" in h and "to date" in h)),
    ("materials_stored", lambda h: "stored" in h and "total" not in h),
    ("balance_to_finish", lambda h: "balance" in h),
    ("item", lambda h: "description" in h or "item" in h),
]


def _num(x):
    """Tolerant money/number parse: handles $, commas, (parens)=negative, blanks."""
    if x is None:
        return 0.0
    if isinstance(x, (int, float)):
        return float(x)
    s = str(x).strip().replace(",", "").replace("$", "").replace(" ", "")
    if not s or s in {"-", "—", "–"}:
        return 0.0
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()%")
    try:
        v = float(s)
    except ValueError:
        return 0.0
    return -v if neg else v


# ---------- raw grid loaders -------------------------------------------------
def _load_csv(path):
    with open(path, newline="", encoding="utf-8-sig") as f:
        return [list(row) for row in csv.reader(f)]


def _load_xlsx(path):
    try:
        from openpyxl import load_workbook
    except ImportError:
        sys.exit("Excel parsing needs openpyxl — run: ./venv/bin/pip install openpyxl")
    wb = load_workbook(path, data_only=True, read_only=True)
    ws = wb.active
    return [list(row) for row in ws.iter_rows(values_only=True)]


def _load_pdf(path):
    try:
        import pdfplumber
    except ImportError:
        sys.exit("PDF parsing needs pdfplumber — run: ./venv/bin/pip install pdfplumber")
    grid = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            for tbl in page.extract_tables():
                grid.extend(tbl)
    return grid


def load_grid(path):
    ext = Path(path).suffix.lower()
    if ext == ".csv":
        return _load_csv(path)
    if ext in (".xlsx", ".xlsm"):
        return _load_xlsx(path)
    if ext == ".pdf":
        return _load_pdf(path)
    sys.exit(f"Unsupported file type '{ext}' — use .xlsx, .csv, or .pdf")


# ---------- header detection + mapping --------------------------------------
def _map_header(row):
    """Return {field: col_index} for a candidate header row, else {}."""
    colmap = {}
    cells = [("" if c is None else str(c)).strip().lower() for c in row]
    for field, pred in _HEADER_RULES:
        if field in colmap:
            continue
        for idx, h in enumerate(cells):
            if h and idx not in colmap.values() and pred(h):
                colmap[field] = idx
                break
    return colmap


def find_header(grid, scan=20):
    """Find the header row: the first row that maps scheduled_value plus >=2
    other recognized columns. Returns (row_index, colmap)."""
    for i, row in enumerate(grid[:scan]):
        cm = _map_header(row)
        if "scheduled_value" in cm and len(cm) >= 3:
            return i, cm
    return None, {}


def _cell(row, colmap, field):
    idx = colmap.get(field)
    if idx is None or idx >= len(row):
        return None
    return row[idx]


def parse_grid(grid, retainage_pct=0.10, previous_payments=0.0,
               project=None, application_no=None):
    hdr_i, cm = find_header(grid)
    if hdr_i is None:
        sys.exit("Could not locate a G703 header row (need a 'Scheduled Value' column "
                 "plus at least two of: Previous, This Period, Materials Stored, "
                 "Total Completed, Balance). Check the file.")
    lines, contract_sum = [], None
    for row in grid[hdr_i + 1:]:
        if not any(c not in (None, "") for c in row):
            continue
        desc = _cell(row, cm, "item")
        desc_str = ("" if desc is None else str(desc)).strip()
        sv = _num(_cell(row, cm, "scheduled_value"))
        low = desc_str.lower()
        if low.startswith("total") or "grand total" in low:   # totals row
            if sv > 0:
                contract_sum = sv
            break
        if sv <= 0:                                            # not a real line
            continue
        li = {
            "item": desc_str or f"Line {len(lines) + 1}",
            "scheduled_value": sv,
            "from_previous": _num(_cell(row, cm, "from_previous")),
            "this_period": _num(_cell(row, cm, "this_period")),
            "materials_stored": _num(_cell(row, cm, "materials_stored")),
        }
        if "completed_to_date" in cm and _cell(row, cm, "completed_to_date") not in (None, ""):
            li["completed_to_date"] = _num(_cell(row, cm, "completed_to_date"))
        if "balance_to_finish" in cm and _cell(row, cm, "balance_to_finish") not in (None, ""):
            li["balance_to_finish"] = _num(_cell(row, cm, "balance_to_finish"))
        lines.append(li)

    if not lines:
        sys.exit("Found a header but no line items with a scheduled value.")
    if contract_sum is None:
        contract_sum = sum(li["scheduled_value"] for li in lines)
    return {
        "project": project or "(parsed from file)",
        "application_no": application_no or "?",
        "retainage_pct": retainage_pct,
        "contract_sum": contract_sum,
        "previous_payments": previous_payments,
        "line_items": lines,
    }


def parse_file(path, **kw):
    return parse_grid(load_grid(path), **kw)


# ---------- synthetic test-file generator (proof + reusable fixture) --------
def make_sample(path, flawed=True):
    """Write a synthetic AIA G703 continuation sheet (.xlsx) built from
    gw_buildwatch's own sample draw, so a file->parse->audit round-trip can be
    proven against a known-good (or known-flawed) result."""
    try:
        from openpyxl import Workbook
    except ImportError:
        sys.exit("Generating a sample needs openpyxl — ./venv/bin/pip install openpyxl")
    from gw_buildwatch import SAMPLES
    draw = SAMPLES["flagged" if flawed else "clean"]()
    wb = Workbook()
    ws = wb.active
    ws.title = "G703"
    ws.append(["AIA DOCUMENT G703 — CONTINUATION SHEET", None, None, None, None, None, None, None])
    ws.append([f"PROJECT: {draw['project']}", None, None, None,
               f"APPLICATION NO: {draw.get('application_no','')}", None, None, None])
    ws.append(["DESCRIPTION OF WORK", "SCHEDULED VALUE", "FROM PREVIOUS APPLICATION",
               "WORK COMPLETED THIS PERIOD", "MATERIALS PRESENTLY STORED",
               "TOTAL COMPLETED AND STORED TO DATE", "%", "BALANCE TO FINISH"])
    total_sv = 0.0
    for li in draw["line_items"]:
        sv = li["scheduled_value"]
        total_sv += sv
        ctd = li.get("completed_to_date", li["from_previous"] + li["this_period"] + li["materials_stored"])
        bal = li.get("balance_to_finish", sv - ctd)
        pctc = f"{(ctd / sv * 100):.0f}%" if sv else ""
        ws.append([li["item"], sv, li["from_previous"], li["this_period"],
                   li["materials_stored"], ctd, pctc, bal])
    ws.append(["GRAND TOTAL", total_sv, None, None, None, None, None, None])
    wb.save(path)
    return path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("file", nargs="?", help="pay-application file to parse (.xlsx/.csv/.pdf)")
    ap.add_argument("--make-sample", metavar="PATH", help="write a synthetic G703 .xlsx here")
    ap.add_argument("--clean", action="store_true", help="with --make-sample, write the clean version")
    ap.add_argument("--retainage-pct", type=float, default=0.10)
    ap.add_argument("--previous-payments", type=float, default=0.0)
    a = ap.parse_args()
    if a.make_sample:
        p = make_sample(a.make_sample, flawed=not a.clean)
        print(f"[synthetic G703 -> {p}]  ({'clean' if a.clean else 'flawed'} version)")
        return
    if not a.file:
        ap.error("give a file to parse, or --make-sample PATH")
    draw = parse_file(a.file, retainage_pct=a.retainage_pct,
                      previous_payments=a.previous_payments)
    print(json.dumps(draw, indent=2))


if __name__ == "__main__":
    main()
