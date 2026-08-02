#!/usr/bin/env python3
"""Discover Instagram artists who might need audio engineering services.

Pipeline:
1. Pull recent posts for a few hashtags via Apify (apify/instagram-hashtag-scraper)
2. Dedupe poster usernames against seed accounts + history of already-sent handles
3. Check follower counts via Apify (apify/instagram-followers-count-scraper)
4. Keep accounts with MIN_FOLLOWERS <= followers <= MAX_FOLLOWERS
5. Take up to MAX_RESULTS new candidates, record to history + CSV, write digest text
"""
import os
import csv
import requests
from pathlib import Path
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / 'data'
CSV_PATH = DATA_DIR / 'artists.csv'
HISTORY_PATH = DATA_DIR / 'ig_sent_history.txt'
DIGEST_PATH = DATA_DIR / 'ig_digest_latest.txt'
TOKEN_PATH = ROOT / 'credentials' / 'apify.token'
HANDLES_PATH = DATA_DIR / 'handles.txt'

HASHTAGS = [h.strip() for h in os.environ.get(
    'IG_HASHTAGS', 'unsignedartist,independentartist,upcomingartist'
).split(',') if h.strip()]
POSTS_PER_HASHTAG = int(os.environ.get('IG_POSTS_PER_HASHTAG', '12'))
MIN_FOLLOWERS = int(os.environ.get('IG_MIN_FOLLOWERS', '3000'))
MAX_FOLLOWERS = int(os.environ.get('IG_MAX_FOLLOWERS', '40000'))
MAX_RESULTS = int(os.environ.get('IG_MAX_RESULTS', '15'))

API = 'https://api.apify.com/v2'


def load_token():
    tok = os.environ.get('APIFY_TOKEN')
    if tok:
        return tok.strip()
    if TOKEN_PATH.exists():
        return TOKEN_PATH.read_text().strip()
    raise SystemExit('APIFY_TOKEN not set and credentials/apify.token missing')


def run_actor(token, actor, payload):
    url = f'{API}/acts/{actor}/run-sync-get-dataset-items'
    r = requests.post(url, params={'token': token}, json=payload, timeout=300)
    r.raise_for_status()
    return r.json()


def load_history():
    if not HISTORY_PATH.exists():
        return set()
    return set(l.strip().lower() for l in HISTORY_PATH.read_text().splitlines() if l.strip())


def append_history(handles):
    HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    with HISTORY_PATH.open('a') as f:
        for h in handles:
            f.write(h.lower() + '\n')


def append_csv(rows):
    header = ['handle', 'platform', 'profileUrl', 'followers', 'recentPostDate', 'contactEmail', 'collected_at']
    write_header = not CSV_PATH.exists()
    CSV_PATH.parent.mkdir(parents=True, exist_ok=True)
    with CSV_PATH.open('a', newline='') as f:
        w = csv.DictWriter(f, fieldnames=header)
        if write_header:
            w.writeheader()
        for row in rows:
            w.writerow({k: row.get(k, '') for k in header})


def main():
    token = load_token()
    history = load_history()

    seeds = set()
    if HANDLES_PATH.exists():
        seeds = set(l.strip().lower() for l in HANDLES_PATH.read_text().splitlines()
                     if l.strip() and not l.strip().startswith('#'))

    print(f'Scraping hashtags: {HASHTAGS} ({POSTS_PER_HASHTAG} posts each)')
    items = run_actor(token, 'apify~instagram-hashtag-scraper', {
        'hashtags': HASHTAGS,
        'resultsType': 'posts',
        'resultsLimit': POSTS_PER_HASHTAG,
    })

    candidates = []
    seen = set()
    for item in items:
        u = (item.get('ownerUsername') or '').strip().lower()
        if not u or u in seen or u in seeds or u in history:
            continue
        seen.add(u)
        candidates.append(u)

    print(f'Found {len(candidates)} unique new candidates from hashtags')
    if not candidates:
        DIGEST_PATH.write_text('No new IG artist leads found today.')
        print('No candidates found; exiting')
        return

    print(f'Checking follower counts for {len(candidates)} candidates')
    profiles = run_actor(token, 'apify~instagram-followers-count-scraper', {
        'usernames': candidates,
    })

    qualified = []
    for p in profiles:
        u = (p.get('userName') or '').strip().lower()
        followers = p.get('followersCount')
        if not u or followers is None:
            continue
        if MIN_FOLLOWERS <= followers <= MAX_FOLLOWERS:
            qualified.append({
                'handle': u,
                'fullName': p.get('userFullName') or '',
                'followers': followers,
                'profileUrl': p.get('userUrl') or f'https://www.instagram.com/{u}',
            })

    print(f'{len(qualified)} candidates passed follower filter ({MIN_FOLLOWERS}-{MAX_FOLLOWERS})')

    selected = qualified[:MAX_RESULTS]

    if not selected:
        DIGEST_PATH.write_text('No new IG artist leads passed the follower filter today.')
        print('No qualified candidates today')
        return

    now = datetime.now(timezone.utc).isoformat()
    rows = [{
        'handle': c['handle'],
        'platform': 'instagram',
        'profileUrl': c['profileUrl'],
        'followers': c['followers'],
        'recentPostDate': '',
        'contactEmail': '',
        'collected_at': now,
    } for c in selected]
    append_csv(rows)
    append_history([c['handle'] for c in selected])

    lines = [f"\U0001F3A7 {len(selected)} IG artist leads ({datetime.now().strftime('%b %d')}):"]
    for i, c in enumerate(selected, 1):
        lines.append(f"{i}. @{c['handle']} — {c['followers']:,} followers — {c['profileUrl']}")
    digest = '\n'.join(lines)
    DIGEST_PATH.write_text(digest)
    print('--- DIGEST ---')
    print(digest)


if __name__ == '__main__':
    main()
