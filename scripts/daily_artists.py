#!/usr/bin/env python3
"""Daily artists runner: orchestrates available scraper/contact tools.

This is a lightweight coordinator for testing. It runs any stub scrapers found
under skills/*/scripts and collects their output. Exits nonzero on failure.
"""
import subprocess
import shutil
import sys
import json
import csv
from pathlib import Path
from datetime import datetime

ROOT = Path(__file__).resolve().parent.parent
SCRAPER = ROOT / 'skills' / 'social-media-scraper' / 'scripts' / 'scrape_stub.mjs'
NODE = shutil.which('node')
DATA_DIR = ROOT / 'data'
DATA_DIR.mkdir(parents=True, exist_ok=True)
CSV_PATH = DATA_DIR / 'artists.csv'

print('daily_artists: start', datetime.utcnow().isoformat() + 'Z')

if not NODE:
    print('ERROR: node executable not found in PATH', file=sys.stderr)
    sys.exit(2)

if not SCRAPER.exists():
    print(f'ERROR: expected scraper not found: {SCRAPER}', file=sys.stderr)
    sys.exit(3)

platforms = ['instagram', 'tiktok', 'youtube']
handle = 'example_artist'
results = []
saved = 0

for p in platforms:
    cmd = [NODE, str(SCRAPER), '--platform', p, '--handle', handle]
    print('Running:', ' '.join(cmd))
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except Exception as e:
        print('Exception running scraper for', p, str(e), file=sys.stderr)
        sys.exit(4)
    out = r.stdout.strip()
    err = r.stderr.strip()
    print('--- stdout ---')
    print(out)
    if err:
        print('--- stderr ---', file=sys.stderr)
        print(err, file=sys.stderr)
    results.append({'platform': p, 'returncode': r.returncode, 'stdout': out, 'stderr': err})
    if r.returncode != 0:
        print(f'Platform {p} failed with exit {r.returncode}', file=sys.stderr)
        sys.exit(5)
    # Try to parse JSON output and append to CSV
    if out:
        try:
            rec = json.loads(out)
        except Exception as e:
            print(f'Could not parse JSON from {p}:', e, file=sys.stderr)
        else:
            # Ensure CSV has header
            header = ['handle', 'platform', 'profileUrl', 'followers', 'recentPostDate', 'contactEmail', 'collected_at']
            write_header = not CSV_PATH.exists()
            with open(CSV_PATH, 'a', newline='') as cf:
                writer = csv.DictWriter(cf, fieldnames=header)
                if write_header:
                    writer.writeheader()
                row = {k: rec.get(k, '') for k in header}
                row['platform'] = p
                row['collected_at'] = datetime.utcnow().isoformat() + 'Z'
                writer.writerow(row)
                saved += 1

print('All scrapers finished successfully')
print('Summary:')
for r in results:
    print(f"- {r['platform']}: returncode={r['returncode']} stdout_lines={len(r['stdout'].splitlines())}")
print(f'Saved {saved} record(s) to {CSV_PATH}')

print('daily_artists: finish', datetime.utcnow().isoformat() + 'Z')
sys.exit(0)
