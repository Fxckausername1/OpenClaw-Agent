#!/usr/bin/env python3
"""Scrape YouTube channel info using the YouTube Data API v3.

Reads handles from data/handles.txt (one per line), resolves channel IDs,
fetches channel statistics and latest video date, and emits one JSON object
per handle to stdout. Also appends results to data/artists.csv and — if
configured — to a Google Sheet.

Configuration:
- YOUTUBE_API_KEY: either the API key string or a path to a file containing the key.
- GOOGLE_APPLICATION_CREDENTIALS: optional path to service account JSON for Sheets API.
- SHEET_ID: optional Google Sheet ID to append rows to.

Usage: python3 scripts/scrape_youtube.py
"""
import os
import sys
import json
import csv
import time
from pathlib import Path
from datetime import datetime
import requests
import math

ROOT = Path(__file__).resolve().parent.parent
HANDLES_PATH = ROOT / 'data' / 'handles.txt'
CSV_PATH = ROOT / 'data' / 'artists.csv'


def load_api_key():
    key = os.environ.get('YOUTUBE_API_KEY')
    if not key:
        p = ROOT / 'credentials' / 'youtube_api.key'
        if p.exists():
            key = p.read_text().strip()
    # If the env var points to a file, load it
    if key and os.path.exists(key) and '\n' not in key and len(key) > 0 and key.endswith('.key'):
        try:
            key = Path(key).read_text().strip()
        except Exception:
            pass
    return key


def read_handles(path=HANDLES_PATH):
    if not path.exists():
        print(f'No handles file at {path}', file=sys.stderr)
        return []
    lines = [l.strip() for l in path.read_text().splitlines()]
    return [l for l in lines if l and not l.startswith('#')]


def _get_with_retry(url, params, max_retries=5, backoff_factor=1.5, timeout=15):
    """GET with simple exponential backoff on 429/5xx errors."""
    attempt = 0
    while True:
        try:
            r = requests.get(url, params=params, timeout=timeout)
            if r.status_code == 429 or 500 <= r.status_code < 600:
                raise requests.HTTPError(f'{r.status_code} Server error', response=r)
            r.raise_for_status()
            return r
        except requests.HTTPError as e:
            attempt += 1
            if attempt > max_retries:
                raise
            sleep = backoff_factor * (2 ** (attempt - 1))
            sleep = min(sleep, 60)
            print(f'HTTP error ({e}), retry {attempt}/{max_retries} after {sleep:.1f}s', file=sys.stderr)
            time.sleep(sleep)
        except requests.RequestException as e:
            attempt += 1
            if attempt > max_retries:
                raise
            sleep = backoff_factor * (2 ** (attempt - 1))
            print(f'Request exception ({e}), retry {attempt}/{max_retries} after {sleep:.1f}s', file=sys.stderr)
            time.sleep(sleep)


def find_channel_id(api_key, handle):
    url = 'https://www.googleapis.com/youtube/v3/search'
    params = {'part': 'snippet', 'q': handle, 'type': 'channel', 'maxResults': 1, 'key': api_key}
    r = _get_with_retry(url, params)
    data = r.json()
    items = data.get('items', [])
    if not items:
        return None, data
    # Prefer id.channelId when present
    channelId = items[0].get('id', {}).get('channelId') or items[0].get('snippet', {}).get('channelId')
    return channelId, data


def get_channels_batch(api_key, channel_ids):
    """Fetch channel resources for up to 50 channel IDs at once."""
    url = 'https://www.googleapis.com/youtube/v3/channels'
    params = {'part': 'snippet,statistics,brandingSettings', 'id': ','.join(channel_ids), 'key': api_key, 'maxResults': 50}
    r = _get_with_retry(url, params)
    data = r.json()
    items = data.get('items', [])
    by_id = {item.get('id'): item for item in items}
    return by_id, data


def get_latest_video_date(api_key, channel_id):
    url = 'https://www.googleapis.com/youtube/v3/search'
    params = {'part': 'snippet', 'channelId': channel_id, 'order': 'date', 'type': 'video', 'maxResults': 1, 'key': api_key}
    r = _get_with_retry(url, params)
    data = r.json()
    items = data.get('items', [])
    if not items:
        return None, data
    return items[0]['snippet'].get('publishedAt'), data


def append_csv(row, path=CSV_PATH):
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
        print(f'Appended to Google Sheet: {sheet_id}', file=sys.stderr)
        return True
    except Exception as e:
        print('Failed to append to Google Sheet:', e, file=sys.stderr)
        return False


def main():
    api_key = load_api_key()
    if not api_key:
        print('Missing YouTube API key. Set YOUTUBE_API_KEY env var or place key in credentials/youtube_api.key', file=sys.stderr)
        sys.exit(2)

    handles = read_handles()
    if not handles:
        print('No handles to process. Populate data/handles.txt with one handle per line.', file=sys.stderr)
        sys.exit(0)

    sheet_id = os.environ.get('SHEET_ID') or os.environ.get('SHEET_ID')

    # Step 1: resolve channel IDs for handles (with polite delays)
    handle_to_channel = {}
    for h in handles:
        try:
            channel_id, _ = find_channel_id(api_key, h)
            if channel_id:
                handle_to_channel[h] = channel_id
                print(f'Found channel for {h}: {channel_id}', file=sys.stderr)
            else:
                print(json.dumps({'handle': h, 'platform': 'youtube', 'error': 'channel_not_found'}))
        except Exception as e:
            print(json.dumps({'handle': h, 'platform': 'youtube', 'error': 'exception', 'detail': str(e)}))
        time.sleep(2)

    # Step 2: batch fetch channel resources
    unique_channel_ids = list({v for v in handle_to_channel.values()})
    id_to_item = {}
    batch_size = 50
    for i in range(0, len(unique_channel_ids), batch_size):
        batch = unique_channel_ids[i:i+batch_size]
        try:
            by_id, _ = get_channels_batch(api_key, batch)
            id_to_item.update(by_id)
            print(f'Fetched channel batch: {len(batch)} channels', file=sys.stderr)
        except Exception as e:
            print(f'Error fetching channel batch: {e}', file=sys.stderr)
        time.sleep(2)

    # Step 3: for each handle, get latest video and persist
    for h, channel_id in handle_to_channel.items():
        try:
            ch = id_to_item.get(channel_id)
            followers = None
            contact = ''
            profile_url = f'https://www.youtube.com/channel/{channel_id}'
            if ch:
                stats = ch.get('statistics', {})
                followers = stats.get('subscriberCount')

            latest_date, _ = get_latest_video_date(api_key, channel_id)

            rec = {
                'handle': h,
                'platform': 'youtube',
                'profileUrl': profile_url,
                'followers': int(followers) if followers and str(followers).isdigit() else (followers or ''),
                'recentPostDate': latest_date or '',
                'contactEmail': contact,
                'collected_at': datetime.utcnow().isoformat() + 'Z'
            }
            print(json.dumps(rec))
            append_csv(rec)
            if sheet_id:
                ok = maybe_append_sheet(rec, sheet_id)
                if ok:
                    print(f'Sheet append succeeded for {h}', file=sys.stderr)

        except requests.HTTPError as e:
            print(json.dumps({'handle': h, 'platform': 'youtube', 'error': 'http_error', 'detail': str(e)}))
        except Exception as e:
            print(json.dumps({'handle': h, 'platform': 'youtube', 'error': 'exception', 'detail': str(e)}))
        time.sleep(2)


if __name__ == '__main__':
    main()
