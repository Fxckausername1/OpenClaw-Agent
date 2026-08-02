#!/usr/bin/env python3
"""Pipeline: run enrichment, prepare append preview, and append to master CSV.

Runs scripts/collect_and_enrich.py, reads its output, de-duplicates against
atlanta_leads.csv, writes a preview CSV under runs/daily_artists/<ts>/append_preview.csv
and appends new rows to atlanta_leads.csv.
"""
from pathlib import Path
import subprocess
import sys
import csv
from datetime import datetime

ROOT = Path(__file__).resolve().parent.parent
VENV_PY = ROOT / 'venv' / 'bin' / 'python'
COLLECT = ROOT / 'scripts' / 'collect_and_enrich.py'
OUT_DIR = ROOT / 'output'
ENRICHED = OUT_DIR / 'atl_indie_leads_enriched.csv'
MASTER = ROOT / 'atlanta_leads.csv'
RUNS = ROOT / 'runs' / 'daily_artists'


def run_collector():
    py = str(VENV_PY) if VENV_PY.exists() else sys.executable
    cmd = [py, str(COLLECT)]
    print('Running collector:', ' '.join(cmd))
    r = subprocess.run(cmd, capture_output=True, text=True)
    print('collector exit', r.returncode)
    if r.stdout:
        print(r.stdout)
    if r.stderr:
        print(r.stderr, file=sys.stderr)
    if r.returncode != 0:
        raise SystemExit(f'collector failed: {r.returncode}')


def read_enriched():
    if not ENRICHED.exists():
        raise SystemExit(f'enriched file missing: {ENRICHED}')
    rows = []
    with ENRICHED.open(encoding='utf-8', newline='') as f:
        r = csv.DictReader(f)
        for row in r:
            rows.append(row)
    return rows


def existing_profile_urls():
    seen = set()
    if not MASTER.exists():
        return seen
    with MASTER.open(encoding='utf-8', newline='') as f:
        r = csv.reader(f)
        # header present; we expect Primary Social Link as 2nd column
        try:
            header = next(r)
        except StopIteration:
            return seen
        for row in r:
            if len(row) >= 2:
                seen.add(row[1])
    return seen


def write_preview_and_append(selected, ts_dir, rundate_iso):
    ts_dir.mkdir(parents=True, exist_ok=True)
    preview = ts_dir / 'append_preview.csv'
    preview_header = [
        'Artist Name','LeadID','Canonical URL','Public Email','Most Recent Release URL',
        'IG Followers','Spotify Monthly Listeners','Fit Score','Vocal Focus','LocalFootprint',
        'Notes','Source Links','RunDate'
    ]
    with preview.open('w', encoding='utf-8', newline='') as f:
        w = csv.writer(f)
        w.writerow(preview_header)
        for r in selected:
            w.writerow([
                r.get('name',''), '', r.get('profile_url',''), (r.get('emails') or '').split(';')[0] if r.get('emails') else '', '',
                '', '', '', '', '', r.get('notes',''), r.get('platform',''), rundate_iso
            ])

    # Append to master CSV (Artist Name/Handle,Primary Social Link,Email,Genre,Recent Activity,Bio,Source)
    append_rows = []
    for r in selected:
        append_rows.append([
            r.get('name',''), r.get('profile_url',''), (r.get('emails') or '').split(';')[0] if r.get('emails') else '', '', '', r.get('notes',''), r.get('platform','')
        ])

    # write append file for audit
    append_file = ts_dir / 'append_rows.csv'
    with append_file.open('w', encoding='utf-8', newline='') as f:
        w = csv.writer(f)
        w.writerow(['Artist Name/Handle','Primary Social Link','Email','Genre','Recent Activity','Bio','Source'])
        for r in append_rows:
            w.writerow(r)

    # actually append to master
    with MASTER.open('a', encoding='utf-8', newline='') as f:
        w = csv.writer(f)
        for r in append_rows:
            w.writerow(r)

    print('Wrote preview ->', preview)
    print('Appended', len(append_rows), 'rows to', MASTER)


def main():
    now = datetime.utcnow()
    ts = now.strftime('%Y-%m-%d-%H%M%S')
    ts_dir = RUNS / ts
    rundate_iso = now.isoformat() + 'Z'

    run_collector()
    rows = read_enriched()

    # prefer those with emails
    rows_sorted = sorted(rows, key=lambda x: (0 if x.get('emails') else 1))
    selected = rows_sorted[:15]

    seen = existing_profile_urls()
    # filter out duplicates
    selected = [r for r in selected if r.get('profile_url') and r.get('profile_url') not in seen]

    write_preview_and_append(selected, ts_dir, rundate_iso)


if __name__ == '__main__':
    main()

