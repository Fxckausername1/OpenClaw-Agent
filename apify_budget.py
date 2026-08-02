#!/usr/bin/env python3
"""Apify free-tier budget guard for the artist pipeline.

The IG scraping runs on Apify's free plan: a HARD $5.00/month usage cap that
resets on a rolling cycle. Before 2026-06-14 the pipeline had no awareness of
this — it burned the whole $5 by day ~21, then every run crashed with an
unhandled 403 ("Monthly usage hard limit exceeded") and heff got no useful
alert.

This module fixes that two ways:
  1. fetch_usage() reads the live cap / spend / cycle window from Apify.
  2. decide() returns one of:
       run        — under the paced budget, go ahead
       skip_paced — under the hard cap but ahead of a linear spend pace; skip
                    today so the $5 stretches across the full cycle
       skip_hard  — at/over the hard cap; skip (would 403 anyway)
     plus a human reason string for the Telegram digest.

Pure logic in decide() is unit-tested via --selftest (no network).
"""
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

API = "https://api.apify.com/v2"
# Keep this fraction of the cap as headroom above the linear pace, so a normal
# day's run isn't blocked the instant it edges over the perfectly-even line.
PACE_HEADROOM = float(os.environ.get("APIFY_PACE_HEADROOM", "0.15"))
# At/above this fraction of the cap, treat as exhausted (the API starts 4xx-ing
# a little before the exact cap).
HARD_FRAC = float(os.environ.get("APIFY_HARD_FRAC", "0.97"))


def fetch_usage(token, timeout=30):
    """Return dict: used, cap, cycle_start, cycle_end (datetimes, UTC)."""
    r = requests.get(f"{API}/users/me/limits", params={"token": token}, timeout=timeout)
    r.raise_for_status()
    d = r.json().get("data", {})
    cyc = d.get("monthlyUsageCycle", {})
    return {
        "used": float(d.get("current", {}).get("monthlyUsageUsd", 0.0)),
        "cap": float(d.get("limits", {}).get("maxMonthlyUsageUsd", 0.0)),
        "cycle_start": _parse(cyc.get("startAt")),
        "cycle_end": _parse(cyc.get("endAt")),
    }


def _parse(s):
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def decide(used, cap, cycle_start, cycle_end, now,
           headroom=PACE_HEADROOM, hard_frac=HARD_FRAC):
    """Return (action, reason). Pure function — no I/O."""
    if not cap or cap <= 0:
        return ("run", "Apify: no usage cap reported; running.")
    end_s = cycle_end.strftime("%b %d") if cycle_end else "next cycle"
    left = cap - used

    if used >= cap * hard_frac:
        return ("skip_hard",
                f"⏸️ Apify free tier exhausted (${used:.2f}/${cap:.0f}). "
                f"Skipping scrape; resets {end_s}.")

    if cycle_start and cycle_end and cycle_end > cycle_start:
        total = (cycle_end - cycle_start).total_seconds()
        elapsed = max(0.0, min(total, (now - cycle_start).total_seconds()))
        frac = elapsed / total
        allowed = cap * min(1.0, frac + headroom)
        if used >= allowed:
            return ("skip_paced",
                    f"⏸️ Pacing Apify budget: ${used:.2f}/${cap:.0f} used, "
                    f"ahead of the ${allowed:.2f} mark for this point in the cycle. "
                    f"Skipping today so the free tier lasts to {end_s}.")

    return ("run",
            f"Apify: ${used:.2f}/${cap:.0f} used, ${left:.2f} left (resets {end_s}).")


def preflight(token, now=None):
    """Live check. Returns (action, reason, usage_dict). Fails OPEN (run) on a
    network/parse error so a flaky limits endpoint never blocks lead-gen."""
    now = now or datetime.now(timezone.utc)
    try:
        u = fetch_usage(token)
    except Exception as e:
        return ("run", f"Apify budget check failed ({e}); running anyway.", {})
    action, reason = decide(u["used"], u["cap"], u["cycle_start"], u["cycle_end"], now)
    return (action, reason, u)


def selftest():
    from datetime import timedelta
    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    end = datetime(2026, 7, 1, tzinfo=timezone.utc)
    cap = 5.0

    # Over hard cap -> skip_hard
    a, r = decide(5.12, cap, start, end, start + timedelta(days=14))
    assert a == "skip_hard", (a, r)

    # Day 15 (~50% through), spent $4.50 -> way ahead of pace -> skip_paced
    a, r = decide(4.50, cap, start, end, start + timedelta(days=15))
    assert a == "skip_paced", (a, r)

    # Day 15, spent $2.00 -> under paced allowance (2.5 + headroom) -> run
    a, r = decide(2.00, cap, start, end, start + timedelta(days=15))
    assert a == "run", (a, r)

    # Day 1, spent $0.50 -> headroom covers it -> run
    a, r = decide(0.50, cap, start, end, start + timedelta(days=1))
    assert a == "run", (a, r)

    # Day 28, spent $4.80 (under hard 0.97*5=4.85) but past pace -> still allowed
    # because near end of cycle the linear line is ~$4.66 + headroom $0.75 = full.
    a, r = decide(4.80, cap, start, end, start + timedelta(days=28))
    assert a in ("run", "skip_paced"), (a, r)

    # No cap -> run
    a, r = decide(99, 0, start, end, start)
    assert a == "run", (a, r)

    print("apify_budget selftest OK")
    for used, day in [(5.12, 14), (4.50, 15), (2.00, 15), (0.50, 1)]:
        a, r = decide(used, cap, start, end, start + timedelta(days=day))
        print(f"  used=${used} day{day}: {a} :: {r}")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    else:
        tok = (os.environ.get("APIFY_TOKEN")
               or Path(__file__).resolve().parent.joinpath("credentials/apify.token").read_text().strip())
        print(preflight(tok))
