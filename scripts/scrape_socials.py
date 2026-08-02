#!/usr/bin/env python3
"""Scrape Instagram and TikTok profile data using Apify Actors.

This script uses the official Apify Python client (apify-client). It expects
an APIFY_TOKEN environment variable to be set (or placed in credentials/apify.token).

Recommended Apify Actors (marketplace actor IDs):
- Instagram profile: "apify/instagram-scraper" (profile mode)
- TikTok profile: "apify/tiktok-scraper" (profile mode)

You can change actor IDs below if you prefer other marketplace actors.

Usage:
- Set APIFY_TOKEN env var (or create credentials/apify.token)
- Ensure data/handles.txt contains handles (one per line)
- Run: python3 scripts/scrape_socials.py --platform instagram
  or: python3 scripts/scrape_socials.py --platform tiktok

Note: Actor input JSON varies by actor. The script uses a conservative input
shape that works with many common profile scrapers, but you may need to
adjust the input keys for the exact actor you pick.
"""
import os
import sys
import time
import json
import argparse
from pathlib import Path
from datetime import datetime, timedelta

ROOT = Path(__file__).resolve().parent.parent
HANDLES_PATH = ROOT / 'data' / 'handles.txt'
CSV_PATH = ROOT / 'data' / 'artists.csv'

# Default actor ids (change if you pick other Apify actors)
ACTORS = {
    # Prefer apidojo/instagram-user-scraper for cookie-backed profile extraction
    'instagram': 'apidojo/instagram-user-scraper',
    'tiktok': 'apify/tiktok-scraper',
}


def load_ig_cookies():
    """Load Instagram cookies from credentials/ig_cookies.json.

    Accepted formats (JSON):
    1) {"cookie": "sessionid=...; csrftoken=..."}
    2) {"sessionid": "..."}
    3) {"cookies": [{"name": "sessionid", "value": "..."}, ...]}

    Returns a cookie header string like "sessionid=...; csrftoken=..." or None.
    """
    p = ROOT / 'credentials' / 'ig_cookies.json'
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text())
    except Exception:
        return None
    # format 1
    if isinstance(data, dict) and 'cookie' in data and isinstance(data['cookie'], str):
        return data['cookie']
    # format 2
    if isinstance(data, dict) and 'sessionid' in data and isinstance(data['sessionid'], str):
        return f"sessionid={data['sessionid']}"
    # format 3
    if isinstance(data, dict) and 'cookies' in data and isinstance(data['cookies'], list):
        parts = []
        for c in data['cookies']:
            name = c.get('name')
            val = c.get('value')
            if name and val:
                parts.append(f"{name}={val}")
        return '; '.join(parts) if parts else None
    return None


def load_apify_token():
    token = os.environ.get('APIFY_TOKEN')
    if not token:
        p = ROOT / 'credentials' / 'apify.token'
        if p.exists():
            token = p.read_text().strip()
    return token


def read_handles():
    if not HANDLES_PATH.exists():
        print(f'No handles file at {HANDLES_PATH}', file=sys.stderr)
        return []
    return [l.strip() for l in HANDLES_PATH.read_text().splitlines() if l.strip() and not l.strip().startswith('#')]


def append_csv(row, path=CSV_PATH):
    import csv
    header = ['handle', 'platform', 'profileUrl', 'followers', 'recentPostDate', 'contactEmail', 'collected_at']
    write_header = not path.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, 'a', newline='') as cf:
        writer = csv.DictWriter(cf, fieldnames=header)
        if write_header:
            writer.writeheader()
        writer.writerow({k: row.get(k, '') for k in header})


def maybe_append_sheet(row, sheet_id):
    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
    except Exception as e:
        print('Google Sheets libs not installed; skipping sheet append:', e, file=sys.stderr)
        return False

    creds_path = os.environ.get('GOOGLE_APPLICATION_CREDENTIALS')
    if not creds_path or not Path(creds_path).exists():
        print('GOOGLE_APPLICATION_CREDENTIALS not set or file missing; skipping sheet append', file=sys.stderr)
        return False

    creds = service_account.Credentials.from_service_account_file(creds_path, scopes=['https://www.googleapis.com/auth/spreadsheets'])
    service = build('sheets', 'v4', credentials=creds)
    sheet = service.spreadsheets()
    values = [[
        row.get('handle',''), row.get('platform',''), row.get('profileUrl',''), row.get('followers',''),
        row.get('recentPostDate',''), row.get('contactEmail',''), row.get('collected_at','')
    ]]
    body = {'values': values}
    try:
        sheet.values().append(spreadsheetId=sheet_id, range='A1', valueInputOption='RAW', insertDataOption='INSERT_ROWS', body=body).execute()
        return True
    except Exception as e:
        print('Failed to append to Google Sheet:', e, file=sys.stderr)
        return False


def run_actor_for_handle(client, actor_id, platform, handle, run_input=None, wait_for_finish=True):
    """Run the actor with given input and return output.

    This uses the apify-client. Behavior: start actor run -> wait for finish -> fetch dataset items.
    """
    try:
        actor = client.actor(actor_id)
    except Exception:
        # apify-client older/newer API differences: try alternative entrypoint
        actor = client.actors.get(actor_id)

    if run_input is None:
        # conservative default input keys; actors may ignore unused keys
        if platform == 'instagram':
            # Many Instagram actors accept directUrls or username; prefer directUrls for profile scraping
            run_input = {'directUrls': [f'https://www.instagram.com/{handle}/'], 'maxItems': 1}
        else:
            run_input = {'directUrls': [f'https://www.tiktok.com/@{handle}'], 'maxItems': 1}

    # Start run
    # Start the actor and wait for completion (actor.call waits until finish by default)
    # Wait up to 30s for the actor to finish to keep the CLI responsive.
    run = actor.call(run_input=run_input, wait_duration=timedelta(seconds=30))
    # run may be a pydantic model or dict; attempt to extract defaultDatasetId robustly
    ds_id = None
    if isinstance(run, dict):
        ds_id = run.get('defaultDatasetId') or run.get('data', {}).get('defaultDatasetId')
    else:
        ds_id = getattr(run, 'defaultDatasetId', None) or (getattr(run, 'data', None) and getattr(run.data, 'defaultDatasetId', None))
    # Attempt to fetch dataset items
    items = []
    if ds_id:
        # apify-client models expose `.items` on list_items() results
        try:
            items = client.dataset(ds_id).list_items().items
        except Exception:
            # fallback to dict-like access
            items = client.dataset(ds_id).list_items().get('items', [])
    return {'run': run, 'items': items}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--platform', choices=['instagram', 'tiktok'], required=True)
    args = parser.parse_args()

    token = load_apify_token()
    if not token:
        print('APIFY_TOKEN not set. Export APIFY_TOKEN env var or put token in credentials/apify.token', file=sys.stderr)
        sys.exit(2)

    try:
        from apify_client import ApifyClient
    except Exception as e:
        print('apify-client not installed; please pip install apify-client', e, file=sys.stderr)
        sys.exit(2)

    client = ApifyClient(token)
    actor_id = ACTORS[args.platform]
    handles = read_handles()
    if not handles:
        print('No handles found; populate data/handles.txt', file=sys.stderr)
        sys.exit(0)

    for h in handles:
        print(f'Running {actor_id} for {h}...', file=sys.stderr)

        # Try several input shapes until the actor returns items
        # For apify/instagram-profile-scraper use the usernames list payload
        tried_inputs = [
            {'usernames': [h], 'maxItems': 1},
            {'usernames': [h], 'useApifyProxy': True, 'maxItems': 1},
        ]

        items = []
        last_exception = None
        cookie_header = load_ig_cookies()
        for inp in tried_inputs:
            # inject cookie header into payload when available
            payload = dict(inp)
            if cookie_header:
                payload['cookies'] = cookie_header
            print(f'  Trying input shape: {list(payload.keys())}', file=sys.stderr)
            try:
                res = run_actor_for_handle(client, actor_id, args.platform, h, run_input=payload)
            except Exception as e:
                last_exception = e
                print('  Actor run failed for', h, e, file=sys.stderr)
                continue
            items = res.get('items') or []
            if items:
                print(f'  Actor returned {len(items)} item(s) for {h}', file=sys.stderr)
                break
            else:
                print(f'  No items for input shape {list(payload.keys())}', file=sys.stderr)

        if not items:
            print(f'No items returned for {h} after trying inputs; last error: {last_exception}', file=sys.stderr)
            continue

        # Heuristic: use first item
        item = items[0]

        followers_raw = item.get('followers') or item.get('followerCount') or item.get('followers_count') or ''
        # normalize follower count
        def parse_followers(x):
            if x is None:
                return None
            if isinstance(x, (int, float)):
                return int(x)
            s = str(x)
            # remove commas, plus signs, spaces
            s = s.replace(',', '').replace('+', '').strip()
            # sometimes '1.2K' formats
            try:
                if s.lower().endswith('k'):
                    return int(float(s[:-1]) * 1000)
                if s.lower().endswith('m'):
                    return int(float(s[:-1]) * 1000000)
                return int(float(s))
            except Exception:
                return None

        followers = parse_followers(followers_raw)

        rec = {
            'handle': h,
            'platform': args.platform,
            'profileUrl': item.get('profileUrl') or item.get('url') or item.get('profile') or '',
            'followers': str(followers) if followers is not None else '',
            'recentPostDate': item.get('lastPostDate') or item.get('recentPostDate') or item.get('last_post_date') or '',
            'contactEmail': item.get('email') or item.get('contact') or '',
            'collected_at': datetime.utcnow().isoformat() + 'Z'
        }

        # Quality filter: only append if followers between 3,000 and 40,000
        if followers is None or not (3000 <= followers <= 40000):
            print(f"Skipped: {h} (Out of follower range)")
            continue

        # Passed quality filter: append to CSV and sheet
        print(json.dumps(rec))
        append_csv(rec)
        sheet_id = os.environ.get('SHEET_ID')
        if sheet_id:
            ok = maybe_append_sheet(rec, sheet_id)
            if ok:
                print(f'Appended to sheet: {h}', file=sys.stderr)

        # polite sleep
        time.sleep(2)


if __name__ == '__main__':
    main()
