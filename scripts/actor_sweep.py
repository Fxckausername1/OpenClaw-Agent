#!/usr/bin/env python3
"""Automated sweep: try multiple Apify actors and input shapes until dataset items returned.

Behavior:
- Reads handles from data/handles.txt
- Tries a sequence of actors and input payload shapes for each handle
- When dataset items are returned, applies follower-quality filter (3k-40k) and appends passing rows to CSV and Google Sheet
- Stops when all handles have at least one returned item (not necessarily passing filter), or when actors exhausted

Usage: env APIFY_TOKEN=... SHEET_ID=... GOOGLE_APPLICATION_CREDENTIALS=... .venv/bin/python scripts/actor_sweep.py
"""
import os
import sys
import time
import json
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HANDLES_PATH = ROOT / 'data' / 'handles.txt'
CSV_PATH = ROOT / 'data' / 'artists.csv'

ACTORS_TO_TRY = [
    'apify/instagram-profile-scraper',
    'apify/instagram-scraper',
    'apidojo/instagram-user-scraper',
    'figue/instagram-profile-scraper',
]


def load_handles():
    if not HANDLES_PATH.exists():
        return []
    return [l.strip() for l in HANDLES_PATH.read_text().splitlines() if l.strip() and not l.strip().startswith('#')]


def append_csv_row(row):
    import csv
    header = ['handle', 'platform', 'profileUrl', 'followers', 'recentPostDate', 'contactEmail', 'collected_at']
    write_header = not CSV_PATH.exists()
    CSV_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CSV_PATH, 'a', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=header)
        if write_header:
            writer.writeheader()
        writer.writerow({k: row.get(k, '') for k in header})


def maybe_append_sheet(row, sheet_id):
    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
    except Exception as e:
        print('Google libs missing; skipping sheet append', e, file=sys.stderr)
        return False
    creds_path = os.environ.get('GOOGLE_APPLICATION_CREDENTIALS')
    if not creds_path or not Path(creds_path).exists():
        print('GOOGLE_APPLICATION_CREDENTIALS missing; skipping sheet append', file=sys.stderr)
        return False
    creds = service_account.Credentials.from_service_account_file(creds_path, scopes=['https://www.googleapis.com/auth/spreadsheets'])
    service = build('sheets', 'v4', credentials=creds)
    sheet = service.spreadsheets()
    values = [[row.get('handle',''), row.get('platform',''), row.get('profileUrl',''), row.get('followers',''), row.get('recentPostDate',''), row.get('contactEmail',''), row.get('collected_at','')]]
    try:
        sheet.values().append(spreadsheetId=sheet_id, range='A1', valueInputOption='RAW', insertDataOption='INSERT_ROWS', body={'values': values}).execute()
        return True
    except Exception as e:
        print('Sheet append failed', e, file=sys.stderr)
        return False


def parse_followers(x):
    if x is None:
        return None
    if isinstance(x, (int, float)):
        return int(x)
    s = str(x).strip()
    s = s.replace(',', '').replace('+', '')
    try:
        if s.lower().endswith('k'):
            return int(float(s[:-1]) * 1000)
        if s.lower().endswith('m'):
            return int(float(s[:-1]) * 1000000)
        return int(float(s))
    except Exception:
        return None


def main():
    token = os.environ.get('APIFY_TOKEN')
    if not token:
        p = ROOT / 'credentials' / 'apify.token'
        if p.exists():
            token = p.read_text().strip()
    if not token:
        print('APIFY_TOKEN missing', file=sys.stderr); sys.exit(1)

    try:
        from apify_client import ApifyClient
    except Exception as e:
        print('apify-client missing:', e, file=sys.stderr); sys.exit(1)

    client = ApifyClient(token=token)
    handles = load_handles()
    if not handles:
        print('No handles found in', HANDLES_PATH, file=sys.stderr); sys.exit(0)

    sheet_id = os.environ.get('SHEET_ID')

    remaining = set(handles)

    for actor_id in ACTORS_TO_TRY:
        if not remaining:
            break
        print('\nTrying actor:', actor_id)
        actor = client.actor(actor_id)

        # input shapes to try
        inputs_for_handle = [
            lambda h: {'usernames': [h], 'maxItems': 1},
            lambda h: {'usernames': [h], 'useApifyProxy': True, 'maxItems': 1},
            lambda h: {'directUrls': [f'https://www.instagram.com/{h}/'], 'resultsType': 'details', 'maxItems': 1},
            lambda h: {'startUrls': [{'url': f'https://www.instagram.com/{h}/'}], 'resultsType': 'details', 'maxItems': 1},
        ]

        for h in list(remaining):
            success = False
            for gen in inputs_for_handle:
                inp = gen(h)
                print(' Running', actor_id, 'with input keys', list(inp.keys()), 'for', h)
                try:
                    # Use a modest wait so the sweep progresses quickly
                    run = actor.call(run_input=inp, wait_duration=timedelta(seconds=15))
                except Exception as e:
                    print('  call failed:', e)
                    continue
                ds = getattr(run, 'defaultDatasetId', None) or (getattr(run, 'data', None) and getattr(run.data, 'defaultDatasetId', None))
                print('  run status', getattr(run, 'status', None), 'dataset', ds)
                if not ds:
                    # nothing returned
                    continue
                items = client.dataset(ds).list_items().items
                print('  items returned:', len(items))
                if not items:
                    continue
                # process first item (profile)
                item = items[0]
                followers_raw = item.get('followers') or item.get('followerCount') or item.get('followers_count') or ''
                followers = parse_followers(followers_raw)
                rec = {
                    'handle': h,
                    'platform': 'instagram',
                    'profileUrl': item.get('profileUrl') or item.get('url') or '',
                    'followers': str(followers) if followers is not None else '',
                    'recentPostDate': item.get('lastPostDate') or item.get('recentPostDate') or '',
                    'contactEmail': item.get('email') or item.get('contact') or '',
                    'collected_at': datetime.utcnow().isoformat() + 'Z'
                }
                if followers is None or not (3000 <= followers <= 40000):
                    print(f'Skipped: {h} (Out of follower range) - followers={followers}')
                else:
                    append_csv_row(rec)
                    if sheet_id:
                        maybe_append_sheet(rec, sheet_id)
                        print(' Appended to sheet for', h)
                    print(' Appended to CSV for', h)
                success = True
                # mark as done regardless of quality filter: we got data
                remaining.discard(h)
                break
            if not success:
                print(' No input shape returned items for', h, 'with actor', actor_id)

    if remaining:
        print('\nSweep finished; some handles returned no items:', remaining)
    else:
        print('\nSweep finished; all handles returned items at least once')


if __name__ == '__main__':
    main()
