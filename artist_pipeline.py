#!/usr/bin/env python3
"""Artist lead pipeline v3 — Atlanta-focused, seed-follower discovery.

Discovery (two sources, merged):
  A) FOLLOWERS of a rotating subset of seed accounts (data/handles.txt) — these
     are people invested in the local scene = high intent / ATL-biased.
  B) Posters under tight ATL + genre hashtags.
Then: drop private/verified, dedupe vs history, cap, profile-scrape, hard-filter
to real ATL-leaning artists in 3k-50k who actually release music (and aren't
competitors), score (ATL signal weighted heavy), analyze top tracks, draft DMs.

Sniper-mode triage: only the top 5 by Fit Score (ties broken toward leads with
an actionable song link or booking email) go to Telegram + leads CSVs + the
CRM sheet (sheet push handled by the wrapper); the rest are saved to
data/leads_unqualified_<date>.csv so nothing is lost but the CRM stays a
curated daily shortlist.
"""
import os
import re
import csv
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import requests
import analyze_track as at
import apify_budget

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
TOKEN_PATH = ROOT / "credentials" / "apify.token"
HISTORY_PATH = DATA / "ig_sent_history.txt"
HANDLES_PATH = DATA / "handles.txt"
DIGEST_PATH = DATA / "ig_digest_latest.txt"
MASTER_PATH = DATA / "leads_master.csv"
API = "https://api.apify.com/v2"

HASHTAGS = [h.strip() for h in os.environ.get(
    "IG_HASHTAGS", "atlmusic,atlhiphop,atlrap,atlrnb,atlantaartist,georgiamusic").split(",") if h.strip()]
POSTS_PER_HASHTAG = int(os.environ.get("IG_POSTS_PER_HASHTAG", "8"))
SEEDS_PER_DAY = int(os.environ.get("IG_SEEDS_PER_DAY", "6"))
FOLLOWERS_PER_SEED = int(os.environ.get("IG_FOLLOWERS_PER_SEED", "25"))
PROFILE_CAP = int(os.environ.get("IG_PROFILE_CAP", "70"))
MIN_FOLLOWERS = int(os.environ.get("IG_MIN_FOLLOWERS", "3000"))
MAX_FOLLOWERS = int(os.environ.get("IG_MAX_FOLLOWERS", "50000"))
MAX_RESULTS = int(os.environ.get("IG_MAX_RESULTS", "5"))
AUDIO_MAX = int(os.environ.get("IG_AUDIO_MAX", "5"))
DETAIL_IN_DIGEST = int(os.environ.get("IG_DIGEST_DETAIL", "5"))
UNQUALIFIED_PATH_FMT = "leads_unqualified_{date}.csv"
UNQUALIFIED_FIELDS = ["handle", "fullName", "followers", "score", "source", "reason"]
OFFER = os.environ.get(
    "IG_OFFER", "an audio engineer offering mixing and mastering, including a free sample mix")

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
MUSIC_CATS = {"musician/band", "music", "dj", "singer", "rapper",
              "producer", "music production studio"}
MUSIC_KW = ["rapper", "singer", "songwriter", "producer", "recording artist",
            "hip hop", "hiphop", "rnb", "r&b", "trap", "beats", "beatmaker",
            "prod by", "produced by", "mixtape", "album", "out now", "new music",
            "stream", "spotify", "soundcloud", "apple music", "audiomack", "music"]
# A real music-platform link is a strong artist signal (overrides the visual-creative
# exclusion below). NOTE: linktr.ee / youtube are too generic to count here.
MUSIC_LINK_DOMAINS = ["spotify", "soundcloud", "audiomack", "beatstars", "bandlab",
                      "distrokid", "music.apple", "unitedmasters", "tunecore",
                      "songwhip", "ffm.to", "fanlink", "ditto.fm", "boomplay",
                      "univer.se", "soundclick", "lynkify"]
# IG's "Artist" bucket is dominated by these — exclude unless a music link is present.
EXCLUDE_KW = ["visual artist", "fine art", "painter", "painting", "illustrator",
              "tattoo", "makeup artist", "nail tech", "nail artist", "lash tech",
              "hairstylist", "hair stylist", "barber", "photographer", "photography",
              "videographer", "graphic design", "gallery", "sculptor",
              "commissions open", "dm for prints", "fashion designer",
              "clothing brand", "boutique"]
# Handle / display-name / URL signals (checked as one haystack, not just the bio).
MUSIC_HANDLE_KW = ["music", "beats", "beatsby", "prodby", ".wav", "_dj", "dj_", "thedj", "rapper"]
VISUAL_HANDLE_KW = [".art", "myart", "artby", "art_", "_art", "tattoo", "paintings",
                    "photography", "photog", "gallery", "sculpt", "nails", "lashes", "hairby"]
ATL_MARKERS = ["atlanta", "atl ", " atl", "atl,", ".atl", "#atl", "404", "678",
               "770", "470", "georgia", " ga ", "ga.", "#ga", "decatur",
               "east atlanta", "zone 6", "swats", "bankhead", "eastside atl"]
COMPETITOR_KW = ["mix & master", "mixing & mastering", "mix and master",
                 "audio engineer", "recording studio", "dm for promo",
                 "promo page", "submit your music", "playlist curator",
                 "music blog", "for promo", "promo only", "we promote",
                 "event venue", "performance &", "interview", "podcast",
                 "comedy club", "radio station", "magazine", "booking agency",
                 "event space", "concert venue"]
CSV_FIELDS = ["handle", "fullName", "profileUrl", "followers", "postsCount",
              "verified", "category", "email", "externalUrl", "score",
              "biography", "audio", "drafted_dm", "collected_at", "source"]

# Discovery-method provenance, threaded from candidate-gathering through to the
# CSV "source" column and the CRM's Lead Source field. Keys are the internal
# tags set in main(); values are the human-readable labels shown to heff.
SOURCE_LABELS = {
    "seed_follower": "Seed followers",
    "hashtag": "IG hashtag scan",
    "unknown": "Unknown (pre-migration)",
}


def load_token():
    tok = os.environ.get("APIFY_TOKEN")
    return tok.strip() if tok else TOKEN_PATH.read_text().strip()


def run_actor(token, actor, payload):
    r = requests.post(f"{API}/acts/{actor}/run-sync-get-dataset-items",
                      params={"token": token}, json=payload, timeout=600)
    r.raise_for_status()
    return r.json()


def load_set(path):
    if not path.exists():
        return []
    return [l.strip() for l in path.read_text().splitlines()
            if l.strip() and not l.strip().startswith("#")]


def todays_seeds(seeds):
    if not seeds:
        return []
    doy = datetime.now(timezone.utc).timetuple().tm_yday
    n = len(seeds)
    start = (doy * SEEDS_PER_DAY) % n
    return [seeds[(start + i) % n] for i in range(min(SEEDS_PER_DAY, n))]


def scrape_followers(token, seeds):
    """Returns list of candidate usernames (drops private + verified)."""
    try:
        res = run_actor(token, "scraping_solutions~instagram-scraper-followers-following-no-cookies",
                        {"Account": seeds, "resultsLimit": FOLLOWERS_PER_SEED,
                         "dataToScrape": "Followers"})
    except Exception as e:
        print(f"follower scrape failed: {e}")
        return []
    out = []
    for r in res:
        if r.get("is_private") or r.get("is_verified"):
            continue
        u = (r.get("username") or "").strip().lower()
        if u:
            out.append(u)
    return out


def scrape_hashtags(token):
    try:
        items = run_actor(token, "apify~instagram-hashtag-scraper",
                          {"hashtags": HASHTAGS, "resultsType": "posts",
                           "resultsLimit": POSTS_PER_HASHTAG})
    except Exception as e:
        print(f"hashtag scrape failed: {e}")
        return []
    return [(it.get("ownerUsername") or "").strip().lower() for it in items if it.get("ownerUsername")]


def music_link(p):
    ext = (p.get("externalUrl") or "").lower()
    return any(d in ext for d in MUSIC_LINK_DOMAINS)


def is_artist(p):
    """MUSIC artists only. IG's 'Artist' category and the bare word 'artist' are
    dominated by VISUAL artists, so require a real music marker — a music-platform
    link, a music category, or a music keyword — and exclude other creatives."""
    bio = (p.get("biography") or "").lower()
    cat = (p.get("businessCategoryName") or "").lower()
    hay = " ".join([p.get("username") or "", p.get("fullName") or "",
                    p.get("externalUrl") or ""]).lower()
    if music_link(p) or any(s in hay for s in MUSIC_HANDLE_KW):
        return True                       # music link, or a music handle like @x_music / @prodby_y
    if any(x in bio for x in EXCLUDE_KW) or any(s in hay for s in VISUAL_HANDLE_KW):
        return False                      # painter / tattoo / MUA / photographer / @myart_x / etc.
    if cat in MUSIC_CATS:
        return True
    return any(k in bio for k in MUSIC_KW)


def is_competitor(p):
    bio = p.get("biography") or ""
    cat = p.get("businessCategoryName") or ""
    name = (p.get("fullName") or "") + " " + (p.get("username") or "")
    t = (bio + " " + cat + " " + name).lower()
    return any(k in t for k in COMPETITOR_KW)


def atl_signal(bio, name):
    t = ((bio or "") + " " + (name or "")).lower()
    return any(m in t for m in ATL_MARKERS)


def score_lead(p, email, ext, atl, analyzable):
    f = p.get("followersCount") or 0
    s = 0
    if atl:
        s += 30
    if ext:
        s += 20
    if email:
        s += 15
    if analyzable:
        s += 10
    s += 15 if 5000 <= f <= 25000 else 8
    if (p.get("postsCount") or 0) >= 50:
        s += 10
    if (p.get("businessCategoryName") or "").lower() in MUSIC_CATS:
        s += 10
    return min(s, 100)


def draft_dm(name, category, bio, audio_note=""):
    pr = (f"Write a casual, friendly Instagram DM (2 sentences, under 45 words) from {OFFER}. "
          f"The recipient is an independent Atlanta artist named '{name}'"
          + (f" ({category})" if category else "") + ". ")
    if audio_note:
        pr += (f"I ran one of their tracks through my tools and found: {audio_note}. "
               f"Mention this specific finding naturally as why I'm reaching out. ")
    else:
        pr += "Reference their music naturally. "
    pr += "No emojis, no hashtags, no quotes. Sound like a real person, not a salesperson."
    try:
        r = subprocess.run(["openclaw", "infer", "model", "run", "--prompt", pr, "--json"],
                           capture_output=True, text=True, timeout=60)
        txt = (json.loads(r.stdout).get("outputs") or [{}])[0].get("text", "").strip()
        if txt:
            return txt
    except Exception:
        pass
    if audio_note:
        return (f"Hey {name}, ran your track through my tools and noticed {audio_note} — "
                f"I'm {OFFER} and can fix that. Want a free sample so you can hear it?")
    return (f"Hey {name}, I really like your sound. I'm {OFFER} and would love to "
            f"mix one of your tracks free so you can hear the difference, no strings attached.")


def append_csv(path, rows):
    new = not path.exists()
    DATA.mkdir(parents=True, exist_ok=True)
    with path.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        if new:
            w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in CSV_FIELDS})


def existing_handles(path):
    if not path.exists():
        return set()
    with path.open(newline="") as f:
        return {(r.get("handle") or "").strip().lower() for r in csv.DictReader(f)}


def has_actionable_contact(L):
    """True if this lead has a booking email or a song link we can run
    analyze_track on -- the inputs for our two strongest DM hooks."""
    return bool(L["email"]) or (L["analyzable"] and bool(L["externalUrl"]))


def triage_sort_key(L):
    """Sniper-mode ranking: Fit Score first, then leads with an actionable
    contact (email or analyzable song link) ahead of DM-only leads at the same
    score."""
    return (L["score"], 1 if has_actionable_contact(L) else 0)


def write_unqualified_csv(path, rows):
    DATA.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=UNQUALIFIED_FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in UNQUALIFIED_FIELDS})


def append_csv_dedup(path, rows):
    """Like append_csv, but skips rows whose handle is already in the file.
    Keeps leads_master.csv free of same-day re-run duplicates without manual
    cleanup."""
    seen = existing_handles(path)
    new_rows = [r for r in rows if (r.get("handle") or "").strip().lower() not in seen]
    if new_rows:
        append_csv(path, new_rows)
    return len(new_rows)


def main():
    token = load_token()

    # Apify free-tier budget guard. Skip gracefully (exit 0, digest written so
    # the wrapper still pings Telegram) when the $5/mo cap is spent or we're
    # ahead of a linear spend pace for the cycle. See apify_budget.py.
    action, budget_reason, _usage = apify_budget.preflight(token)
    if action != "run":
        DIGEST_PATH.write_text(budget_reason)
        print(budget_reason)
        return

    history = {h.lower() for h in load_set(HISTORY_PATH)}
    seeds = [s.lower() for s in load_set(HANDLES_PATH)]
    seed_set = set(seeds)

    todays = todays_seeds(seeds)
    print(f"Scraping followers of {len(todays)} seeds: {todays}")
    cands = scrape_followers(token, todays)
    print(f"  {len(cands)} follower candidates")
    htags = scrape_hashtags(token)
    print(f"  {len(htags)} hashtag candidates ({HASHTAGS})")

    tagged = [(u, "seed_follower") for u in cands] + [(u, "hashtag") for u in htags]
    seen, candidates, source_map = set(), [], {}
    for u, src in tagged:
        if u and u not in seen and u not in seed_set and u not in history:
            seen.add(u); candidates.append(u); source_map[u] = src
    candidates = candidates[:PROFILE_CAP]
    print(f"{len(candidates)} unique new candidates (capped {PROFILE_CAP}); profile-scraping...")
    if not candidates:
        DIGEST_PATH.write_text("No new artist leads found today.")
        return

    try:
        profiles = run_actor(token, "apify~instagram-profile-scraper", {"usernames": candidates})
    except Exception as e:
        # Don't crash with an unhandled traceback (the old behavior left heff
        # with no actionable alert). Write a clear digest so the wrapper pings.
        msg = (f"⚠️ Artist pipeline hit an Apify error mid-run and stopped before "
               f"producing leads: {str(e)[:180]}\n{budget_reason}")
        DIGEST_PATH.write_text(msg)
        print(msg)
        return

    leads, out_of_band = [], []
    for p in profiles:
        if p.get("private"):
            continue
        # Bot / low-activity heuristics
        posts = p.get("postsCount") or 0
        bio = p.get("biography") or ""
        uname = (p.get("username") or "")
        MIN_POSTS = int(os.environ.get("IG_MIN_POSTS", "3"))
        if posts < MIN_POSTS:
            continue
        # Reject likely bot/spam usernames (long numeric suffixes)
        if re.search(r"\d{4,}", uname):
            continue
        # Reject obvious promo/bot bios
        lowbio = bio.lower()
        if any(k in lowbio for k in ("free followers", "follow for follow", "buy followers", "promo", "click here")):
            continue
        if not is_artist(p) or is_competitor(p):
            continue
        em = EMAIL_RE.search(bio)
        email = em.group(0) if em else ""
        ext = p.get("externalUrl") or ""
        allow_dm_only = os.environ.get("IG_ALLOW_DM_ONLY", "0") == "1"
        if not (email or ext) and not allow_dm_only:
            continue
        f = p.get("followersCount") or 0
        handle = (p.get("username") or "").lower()
        name = p.get("fullName") or handle
        atl = atl_signal(bio, name)
        analyzable = at.is_analyzable(ext)
        source = SOURCE_LABELS[source_map.get(handle, "unknown")]
        score = score_lead(p, email, ext, atl, analyzable)
        # Goldilocks follower band, strictly enforced: too small (<3k) can't
        # move our pricing, too big (>50k) won't read DMs from a stranger.
        if not (MIN_FOLLOWERS <= f <= MAX_FOLLOWERS):
            reason = "followers<min" if f < MIN_FOLLOWERS else "followers>max"
            out_of_band.append({"handle": handle, "fullName": name, "followers": f,
                                 "score": score, "source": source, "reason": reason})
            continue
        leads.append({
            "handle": handle, "fullName": name,
            "profileUrl": f"https://www.instagram.com/{handle}",
            "followers": f, "postsCount": p.get("postsCount") or 0,
            "verified": bool(p.get("verified")), "category": p.get("businessCategoryName") or "",
            "email": email, "externalUrl": ext, "biography": bio,
            "atl": atl, "analyzable": analyzable,
            "score": score,
            "source": source,
        })

    today = datetime.now().strftime("%Y-%m-%d")

    # Sniper-mode triage: Fit Score desc, ties broken toward leads with an
    # actionable contact (email or analyzable song link). Only the top
    # MAX_RESULTS go to the CRM / master CSV / Telegram; the rest are saved to
    # a local "unqualified" CSV (out-of-band followers + score overflow) so
    # nothing is lost but the CRM stays a curated shortlist.
    leads.sort(key=triage_sort_key, reverse=True)
    selected = leads[:MAX_RESULTS]
    overflow = leads[MAX_RESULTS:]
    for L in overflow:
        out_of_band.append({"handle": L["handle"], "fullName": L["fullName"],
                             "followers": L["followers"], "score": L["score"],
                             "source": L["source"], "reason": "score_overflow"})
    if out_of_band:
        write_unqualified_csv(DATA / UNQUALIFIED_PATH_FMT.format(date=today), out_of_band)
        print(f"{len(out_of_band)} lead(s) saved to {UNQUALIFIED_PATH_FMT.format(date=today)}")

    print(f"{len(selected)} qualified leads; analyzing tracks + drafting DMs...")
    if not selected:
        DIGEST_PATH.write_text("No qualified Atlanta artist leads passed filters today.")
        return

    now = datetime.now(timezone.utc).isoformat()
    analyzed = 0
    for L in selected:
        L["audio"] = ""
        note = ""
        if analyzed < AUDIO_MAX and L["analyzable"]:
            res = at.analyze_url(L["externalUrl"])
            if res:
                L["audio"] = at.short_summary(res); note = L["audio"]; analyzed += 1
                print(f"  analyzed {L['handle']}: {note}")
        L["drafted_dm"] = draft_dm(L["fullName"], L["category"], L["biography"], note)
        L["collected_at"] = now

    append_csv(DATA / f"leads_{today}.csv", selected)
    append_csv_dedup(MASTER_PATH, selected)
    with HISTORY_PATH.open("a") as fh:
        for L in selected:
            fh.write(L["handle"] + "\n")

    lines = [f"\U0001F3AF ATL artist leads -- {datetime.now().strftime('%b %d')} "
             f"({len(selected)} ranked)"]
    for i, L in enumerate(selected[:DETAIL_IN_DIGEST], 1):
        contact = (f"email {L['email']}" if L["email"]
                   else (f"link {L['externalUrl']}" if L["externalUrl"] else "DM only"))
        tags = []
        if L["atl"]:
            tags.append("ATL")
        if L["verified"]:
            tags.append("verified")
        tagstr = (" · " + " ".join(tags)) if tags else ""
        lines.append("")
        lines.append(f"{i}. {L['fullName']} (@{L['handle']}) -- score {L['score']}{tagstr}")
        lines.append(f"   {L['followers']:,} followers - {L['category'] or 'artist'}")
        if L["audio"]:
            lines.append(f"   \U0001F39B Track: {L['audio']}")
        lines.append(f"   Contact: {contact}")
        lines.append(f"   {L['profileUrl']}")
        lines.append(f"   DM: {L['drafted_dm']}")
    rest = selected[DETAIL_IN_DIGEST:]
    if rest:
        lines.append("")
        lines.append("Also: " + ", ".join(f"@{L['handle']}({L['score']})" for L in rest))
    lines.append("")
    lines.append(f"Full details + DMs: data/leads_{today}.csv")
    lines.append(budget_reason)
    DIGEST_PATH.write_text("\n".join(lines))
    print("\n".join(lines))


if __name__ == "__main__":
    main()
