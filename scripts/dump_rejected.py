#!/usr/bin/env python3
"""Dump rejected candidate profiles and reasons for the last run.
"""
import os, re, json, requests
from pathlib import Path
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / 'data'
TOKEN_PATH = ROOT / 'credentials' / 'apify.token'

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
MUSIC_CATS = {"musician/band", "artist", "music", "dj", "singer", "rapper",
              "producer", "music production studio", "podcast"}
MUSIC_KW = ["music", "artist", "rapper", "singer", "songwriter", "producer",
            "beats", "studio", "mixtape", "album", "single", "ep ", "out now",
            "stream", "spotify", "soundcloud", "apple music", "new music", "rnb", "r&b"]
COMPETITOR_KW = ["mix & master", "mixing & mastering", "mix and master",
                 "audio engineer", "recording studio", "dm for promo",
                 "promo page", "submit your music", "playlist curator",
                 "music blog", "for promo", "promo only", "we promote",
                 "event venue", "performance &", "interview", "podcast",
                 "comedy club", "radio station", "magazine", "booking agency",
                 "event space", "concert venue"]

API = "https://api.apify.com/v2"


def load_token():
    tok = os.environ.get('APIFY_TOKEN')
    if tok:
        return tok.strip()
    return TOKEN_PATH.read_text().strip()


def run_actor(token, actor, payload):
    r = requests.post(f"{API}/acts/{actor}/run-sync-get-dataset-items",
                      params={"token": token}, json=payload, timeout=600)
    r.raise_for_status()
    return r.json()


def is_artist(p):
    if (p.get("businessCategoryName") or "").lower() in MUSIC_CATS:
        return True
    bio = (p.get("biography") or "").lower()
    return any(k in bio for k in MUSIC_KW)


def is_competitor(p):
    bio = p.get("biography") or ""
    cat = p.get("businessCategoryName") or ""
    name = (p.get("fullName") or "") + " " + (p.get("username") or "")
    t = (bio + " " + cat + " " + name).lower()
    return any(k in t for k in COMPETITOR_KW)


MIN_F = int(os.environ.get('IG_MIN_FOLLOWERS', '3000'))
ALLOW_DM = os.environ.get('IG_ALLOW_DM_ONLY', '0') == '1'


def main():
    token = load_token()
    handles_path = DATA / 'handles.txt'
    seeds = [l.strip().lower() for l in handles_path.read_text().splitlines() if l.strip() and not l.startswith('#')]
    # todays seeds from artist_pipeline.todays_seeds logic
    doy = datetime.now(timezone.utc).timetuple().tm_yday
    n = len(seeds)
    start = (doy * int(os.environ.get('IG_SEEDS_PER_DAY','6'))) % n if n>0 else 0
    SEEDS_PER_DAY = int(os.environ.get('IG_SEEDS_PER_DAY','6'))
    todays = [seeds[(start + i) % n] for i in range(min(SEEDS_PER_DAY, n))] if n>0 else []

    # gather candidates
    cands = []
    try:
        res = run_actor(token, "scraping_solutions~instagram-scraper-followers-following-no-cookies",
                        {"Account": todays, "resultsLimit": int(os.environ.get('IG_FOLLOWERS_PER_SEED','25')), "dataToScrape": "Followers"})
    except Exception as e:
        print('follower scrape failed', e)
        res = []
    for r in res:
        if r.get('is_private') or r.get('is_verified'):
            continue
        u = (r.get('username') or '').strip().lower()
        if u:
            cands.append(u)
    try:
        htags = run_actor(token, "apify~instagram-hashtag-scraper",
                          {"hashtags": (os.environ.get('IG_HASHTAGS') or 'atlmusic,atlhiphop,atlrap,atlrnb,atlantaartist,georgiamusic').split(','),
                           "resultsType": "posts",
                           "resultsLimit": int(os.environ.get('IG_POSTS_PER_HASHTAG','8'))})
        hlist = [(it.get('ownerUsername') or '').strip().lower() for it in htags if it.get('ownerUsername')]
    except Exception as e:
        print('hashtag scrape failed', e)
        hlist = []

    seen = set()
    candidates = []
    history_path = DATA / 'ig_sent_history.txt'
    history = {l.strip().lower() for l in history_path.read_text().splitlines() if l.strip()} if history_path.exists() else set()
    seed_set = set(seeds)
    for u in cands + hlist:
        if u and u not in seen and u not in seed_set and u not in history:
            seen.add(u); candidates.append(u)
    candidates = candidates[:int(os.environ.get('IG_PROFILE_CAP','70'))]
    if not candidates:
        print('No candidates')
        return

    profiles = run_actor(token, "apify~instagram-profile-scraper", {"usernames": candidates})

    rejected = []
    accepted = []
    for p in profiles:
        reasons = []
        if p.get('private'):
            reasons.append('private')
        f = p.get('followersCount')
        if f is None or not (MIN_F <= (f or 0)):
            reasons.append(f'followers<{MIN_F}' if f is not None else 'no_followers')
        if not is_artist(p):
            reasons.append('not_artist')
        if is_competitor(p):
            reasons.append('competitor')
        bio = p.get('biography') or ''
        em = EMAIL_RE.search(bio)
        email = em.group(0) if em else ''
        ext = p.get('externalUrl') or ''
        if not (email or ext) and not ALLOW_DM:
            reasons.append('no_contact')
        if reasons:
            rejected.append({'username': p.get('username') or '', 'followers': f, 'reasons': reasons, 'profileUrl': f"https://www.instagram.com/{(p.get('username') or '')}"})
        else:
            accepted.append(p.get('username') or '')

    # Print rejected list
    print(f"Candidates: {len(candidates)} | Accepted: {len(accepted)} | Rejected: {len(rejected)}")
    for r in rejected:
        print(f"@{r['username']},{r.get('followers')},{';'.join(r['reasons'])},{r['profileUrl']}")

if __name__ == '__main__':
    main()
