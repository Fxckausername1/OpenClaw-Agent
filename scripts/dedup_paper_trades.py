#!/usr/bin/env python3
"""One-shot, reversible migration: remove the 34 stale duplicate rows from
data/paper_trades.csv.

WHY THIS EXISTS. paper_eval.py's load_open -> ingest -> evaluate -> save_open
is a read-modify-write with no mutual exclusion. At 16:05 ET the MR wrapper
and night_report.py both run paper_eval.py. The --eod run closes every
position, appends its CSV rows and writes {} to paper_open.json; the
concurrent non-eod run -- which loaded the file BEFORE that write -- then
saves its stale dict back, resurrecting the just-closed positions. Next
session they are evaluated again and closed a SECOND time. ingest()'s
in_csv() guard never fires because `tid in open_pos` short-circuits first,
and append_csv() has no trade_id check at all.

THE RULE, and why it is this one. Keep the row whose close_time DATE equals
its entry_time DATE (the legitimate same-day EOD close); drop the other.
Verified to be unambiguous for all 34 groups: each has exactly one same-day
row, and it is always the earlier line.

Do NOT use exit_reason == 'eod' as the discriminator. It fails on
INVH:LONG:2026-07-17 and MO:LONG:2026-07-28, where BOTH rows are 'eod' --
the stale partner is a next-SESSION eod close. The date rule handles those
two correctly; the exit_reason rule would silently keep the wrong row.

SAFETY. Reads the live file, writes a new file to a temp path, then
os.replace (atomic). Refuses to run unless the pre-state matches exactly
what was audited. Writes nothing if --apply is absent.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import os
import shutil
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path("/home/heff/.openclaw/workspace")
CSV_PATH = ROOT / "data" / "paper_trades.csv"

EXPECTED_ROWS_BEFORE = 823
EXPECTED_UNIQUE = 789
EXPECTED_DUP_GROUPS = 34
# Physical CSV line numbers (header = line 1) independently identified by the
# forensic pass. Used ONLY as a cross-check on the date rule -- if the rule
# and this list disagree, something changed and we abort rather than guess.
EXPECTED_DROP_LINES = {
    501, 502, 503, 504, 505, 506, 507, 508, 509, 510, 511, 512, 513, 514,
    517, 518, 520, 530, 734, 735, 736, 737, 738, 739, 740, 741, 742, 743,
    744, 745, 746, 747, 748, 761,
}


def date_of(value: str) -> str:
    """Leading YYYY-MM-DD, tolerant of a tz offset on close_time (which is
    written tz-aware while entry_time is naive -- comparing the full
    timestamps directly would raise)."""
    return (value or "").strip()[:10]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="actually rewrite the ledger (default is dry run)")
    ap.add_argument("--backup-dir", required=True)
    args = ap.parse_args()

    if not CSV_PATH.exists():
        print(f"FATAL: {CSV_PATH} missing", file=sys.stderr)
        return 2

    raw = CSV_PATH.read_bytes()
    sha_before = hashlib.sha256(raw).hexdigest()
    with CSV_PATH.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        fields = reader.fieldnames
        rows = list(reader)

    # physical line number: header is 1, so data row i (0-based) is i+2
    for i, row in enumerate(rows):
        row["_line"] = i + 2

    print(f"file        : {CSV_PATH}")
    print(f"sha256      : {sha_before}")
    print(f"rows before : {len(rows)}")

    by_id = defaultdict(list)
    for row in rows:
        by_id[row.get("trade_id", "")].append(row)
    dup_groups = {k: v for k, v in by_id.items() if len(v) > 1}
    print(f"unique ids  : {len(by_id)}")
    print(f"dup groups  : {len(dup_groups)}")

    if len(rows) != EXPECTED_ROWS_BEFORE or len(by_id) != EXPECTED_UNIQUE \
            or len(dup_groups) != EXPECTED_DUP_GROUPS:
        print("FATAL: pre-state does not match the audited state. Refusing to "
              "migrate a file that changed since it was analysed.", file=sys.stderr)
        return 3

    drop_lines, keep_lines, ambiguous = set(), set(), []
    for tid, group in sorted(dup_groups.items()):
        same_day = [r for r in group
                    if date_of(r.get("close_time", "")) == date_of(r.get("entry_time", ""))]
        if len(same_day) != 1:
            ambiguous.append((tid, len(same_day)))
            continue
        keep = same_day[0]
        keep_lines.add(keep["_line"])
        for r in group:
            if r["_line"] != keep["_line"]:
                drop_lines.add(r["_line"])

    if ambiguous:
        print("FATAL: rule is ambiguous for these groups; aborting rather than "
              f"guessing: {ambiguous}", file=sys.stderr)
        return 4

    print(f"drop lines  : {len(drop_lines)}")
    if drop_lines != EXPECTED_DROP_LINES:
        print("FATAL: date rule and the independently-derived line list "
              "DISAGREE. Aborting.", file=sys.stderr)
        print(f"  rule-only     : {sorted(drop_lines - EXPECTED_DROP_LINES)}", file=sys.stderr)
        print(f"  expected-only : {sorted(EXPECTED_DROP_LINES - drop_lines)}", file=sys.stderr)
        return 5
    print("cross-check : date rule EXACTLY matches the audited drop set")

    def rsum(rs):
        total = 0.0
        for r in rs:
            try:
                total += float(r.get("outcome_r") or 0)
            except ValueError:
                pass
        return total

    kept = [r for r in rows if r["_line"] not in drop_lines]
    dropped = [r for r in rows if r["_line"] in drop_lines]
    print(f"rows after  : {len(kept)}")
    print(f"sum outcome_r before : {rsum(rows):.3f}")
    print(f"sum outcome_r after  : {rsum(kept):.3f}")
    print(f"delta removed        : {rsum(dropped):.3f}")

    if not args.apply:
        print("\nDRY RUN -- nothing written. Re-run with --apply to migrate.")
        return 0

    backup_dir = Path(args.backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)
    pre = backup_dir / "paper_trades.csv.PRE_DEDUP"
    shutil.copy2(CSV_PATH, pre)
    (backup_dir / "dropped_rows.csv").write_text(
        _as_csv(fields, dropped), encoding="utf-8")

    tmp = CSV_PATH.with_name(f".{CSV_PATH.name}.migrate.tmp")
    tmp.write_text(_as_csv(fields, kept), encoding="utf-8")
    os.replace(tmp, CSV_PATH)

    after = CSV_PATH.read_bytes()
    print(f"\nAPPLIED")
    print(f"  pre-dedup copy : {pre}")
    print(f"  dropped rows   : {backup_dir / 'dropped_rows.csv'}")
    print(f"  sha256 after   : {hashlib.sha256(after).hexdigest()}")
    return 0


def _as_csv(fields, rows) -> str:
    import io
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({k: row.get(k, "") for k in fields})
    return buf.getvalue()


if __name__ == "__main__":
    sys.exit(main())
