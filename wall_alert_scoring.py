#!/usr/bin/env python3
"""wall_alert_scoring.py -- daily cross-check of wall_proximity_alert.py's "holding"/
"cracking" verdicts against real subsequent price action, accumulated across sessions.

Built 2026-07-06 after a one-off manual check of that day's alerts found the raw
wss_score>=0 "holding" cutoff underperforming a coin flip (40.4%, n=52) while a
wss_score>=9 sub-zone hit 58.3% (n=12) -- see advanced_gex.py's compute_advanced()
wall_confidence tag, which surfaces that finding on the dashboard. That was ONE
session's data. This script exists to keep accumulating real outcomes day over day
so the wall_confidence threshold (and any future parameter) can be validated or
revised on a real multi-day sample instead of staying frozen on day-1 noise.

Idempotent: every alert event has a stable key (ticker, ts, wall_type), so re-running
this against the same log never double-counts -- only genuinely new events get scored
and appended to the ledger. Safe to run daily via cron (see wall_alert_scoring_wrapper.sh).

Run after market close so the 60min lookforward window has real data to check against
(a same-day alert scored too early would show artificially few/incomplete forward bars).
"""
import json
import re
import requests
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
LOG_PATH = ROOT / "logs" / "wall_proximity_alert.log"
LEDGER_PATH = DATA / "wall_alert_ledger.jsonl"
SUMMARY_PATH = DATA / "wall_alert_accuracy_summary.json"
ET = ZoneInfo("America/New_York")

KEY = (ROOT / "credentials" / "alpaca_key.txt").read_text().strip()
SEC = (ROOT / "credentials" / "alpaca_secret.txt").read_text().strip()
H = {"APCA-API-KEY-ID": KEY, "APCA-API-SECRET-KEY": SEC}

LOOKFORWARD_BARS = 12  # 60 min at 5min cadence

ALERT_RE = re.compile(
    # score group allows an optional decimal (2026-07-07 fix, was integer-only \d+):
    # next_move_read() now logs wss_score at 1 decimal place instead of 0, since a real
    # score like -0.4 used to print as "-0" and int("-0")==0 silently ate the sign for
    # any score in (-1, 0) -- 40 of 236 ledger rows were corrupted this way before the fix.
    #
    # Leading (?:\U0001F3AF+ )? (2026-07-10 fix): wall_proximity_alert.py's roll-up lines
    # got a "\U0001F3AF "/"\U0001F3AF\U0001F3AF " hot-setup marker prepended directly to
    # the ticker starting 2026-07-09 (is_hot_setup) and 2026-07-10 (the ultra-tight second
    # tier) -- this regex's ^([A-Z.]+): anchor never accounted for that prefix, so EVERY
    # marked line silently failed to match and was dropped from parse_all_events() entirely.
    # Confirmed 2026-07-10: all 5 of that day's real "HIGH-CONVICTION SETUP" Telegram
    # alerts (MOS, XLY, F, EQT, SPGI) had zero matching ledger rows even though the alerts
    # genuinely fired -- exactly the negative-gamma+holding population this whole edge is
    # about, silently missing from its own scoring pipeline. Since main() re-parses the
    # FULL log every run and only appends keys not already in the ledger, fixing this and
    # re-running backfills every previously-dropped marked event automatically, not just
    # new ones going forward.
    #
    # Optional "(?:\s+CVD[^\n]*\n)?" (2026-07-10 fix, found while investigating the marker
    # bug above): cvd_reversal_read()'s note (added 2026-07-05, see wall_proximity_alert.py)
    # inserts a whole extra "  CVD ... reversal-watch" line between the wall-status line and
    # "Confluence:" whenever a ticker has a confirming CVD read -- the old regex required
    # "Confluence:" on the VERY NEXT line with no allowance for that, so every CVD-flagged
    # event has been silently dropped from scoring since 2026-07-05, independent of the
    # marker bug. Same backfill-on-re-run property applies.
    r"^(?:\U0001F3AF+ )?([A-Z.]+): \$([\d.]+) within ([\d.]+)% of (put|call) wall \$([\d.]+) \((positive|negative)\)\n"
    r"\s+(wall holding|wall cracking)[^(]*\(([A-Z]+) ([+-]?\d+(?:\.\d+)?), P\(C\) (\d+)%\)[^\n]*\n"
    r"(?:\s+CVD[^\n]*\n)?"
    r"\s+Confluence: (.+)",
    re.MULTILINE,
)
TS_RE = re.compile(r"WALL ALERT \((\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}) ET\):")


def parse_all_events():
    """Every alert event ever logged (all dates present in the log). Returns a dict
    keyed by (ticker, date, time, wall_type) -- the stable dedup key -- so callers can
    diff against what's already in the ledger."""
    if not LOG_PATH.exists():
        return {}
    text = LOG_PATH.read_text()
    blocks = re.split(r"(?=WALL ALERT \()", text)
    events = {}
    for block in blocks:
        ts_m = TS_RE.search(block)
        if not ts_m:
            continue
        date_str, time_str = ts_m.groups()
        for m in ALERT_RE.finditer(block):
            ticker, price, dist_pct, wall_type, wall_level, regime, verdict, wss_flag, wss_score, p_c, confluence = m.groups()
            key = (ticker, date_str, time_str, wall_type)
            events[key] = {
                "ticker": ticker, "date": date_str, "time": time_str, "price": float(price),
                "dist_pct": float(dist_pct), "wall_type": wall_type, "wall_level": float(wall_level),
                "regime": regime, "verdict": "holding" if verdict == "wall holding" else "cracking",
                "wss_flag": wss_flag, "wss_score": float(wss_score), "p_c": int(p_c),
                "confluence": confluence.strip(),
            }
    return events


def load_ledger_keys():
    """Stable keys already scored, from the persistent ledger."""
    if not LEDGER_PATH.exists():
        return set()
    keys = set()
    for line in LEDGER_PATH.read_text().splitlines():
        try:
            row = json.loads(line)
            keys.add((row["ticker"], row["date"], row["time"], row["wall_type"]))
        except Exception:
            continue
    return keys


def fetch_bars_for_date(tickers, date_str):
    """5min RTH bars for one calendar date, batched across all tickers needed that day."""
    start = f"{date_str}T09:30:00-04:00"
    end = f"{date_str}T16:30:00-04:00"  # small buffer past close for late-day alerts' lookforward
    bars_by_ticker = {t: [] for t in tickers}
    for i in range(0, len(tickers), 100):
        batch = tickers[i:i + 100]
        page_token = None
        while True:
            params = {"symbols": ",".join(batch), "timeframe": "5Min", "start": start, "end": end,
                       "feed": "iex", "limit": 10000, "sort": "asc"}
            if page_token:
                params["page_token"] = page_token
            try:
                r = requests.get("https://data.alpaca.markets/v2/stocks/bars", headers=H, params=params, timeout=30)
                data = r.json()
            except Exception:
                break
            for sym, bars in (data.get("bars") or {}).items():
                bars_by_ticker[sym].extend(bars)
            page_token = data.get("next_page_token")
            if not page_token:
                break
    for t in bars_by_ticker:
        bars_by_ticker[t].sort(key=lambda b: b["t"])
    return bars_by_ticker


def score_event(ev, bars_by_ticker):
    bars = bars_by_ticker.get(ev["ticker"], [])
    if not bars:
        return None
    alert_dt = datetime.strptime(f"{ev['date']} {ev['time']}", "%Y-%m-%d %H:%M").replace(tzinfo=ET)
    idx = None
    for i, b in enumerate(bars):
        bt = datetime.fromisoformat(b["t"].replace("Z", "+00:00")).astimezone(ET)
        if bt >= alert_dt:
            idx = i
            break
    if idx is None:
        return None
    window = bars[idx: idx + LOOKFORWARD_BARS + 1]
    if len(window) < 2:
        return None
    wall = ev["wall_level"]
    if ev["wall_type"] == "put":
        cracked = any(b["c"] < wall * 0.999 for b in window[1:])
    else:
        cracked = any(b["c"] > wall * 1.001 for b in window[1:])
    predicted_crack = ev["verdict"] == "cracking"
    return {"correct": predicted_crack == cracked, "actual": "cracked" if cracked else "held",
            "n_bars_available": len(window) - 1}


def summarize(rows, keyfunc):
    from collections import defaultdict
    buckets = defaultdict(list)
    for r in rows:
        buckets[keyfunc(r)].append(r)
    out = []
    for k, rs in sorted(buckets.items(), key=lambda kv: str(kv[0])):
        n = len(rs)
        acc = round(sum(1 for r in rs if r["correct"]) / n, 4) if n else None
        out.append({"key": str(k), "n": n, "accuracy": acc})
    return out


def main():
    all_events = parse_all_events()
    already = load_ledger_keys()
    new_keys = [k for k in all_events if k not in already]
    if not new_keys:
        print("no new alert events to score")
    else:
        # Only fetch bars for dates that actually have new events, and only for tickers
        # needed that date -- keeps this cheap even as the log grows.
        by_date = {}
        for k in new_keys:
            ev = all_events[k]
            by_date.setdefault(ev["date"], []).append(ev)

        new_rows = []
        for date_str, evs in by_date.items():
            tickers = sorted(set(e["ticker"] for e in evs))
            print(f"scoring {len(evs)} new event(s) on {date_str} across {len(tickers)} ticker(s)...")
            bars_by_ticker = fetch_bars_for_date(tickers, date_str)
            for ev in evs:
                scored = score_event(ev, bars_by_ticker)
                if scored is not None:
                    new_rows.append({**ev, **scored})

        if new_rows:
            with LEDGER_PATH.open("a") as f:
                for row in new_rows:
                    f.write(json.dumps(row) + "\n")
            print(f"appended {len(new_rows)} newly-scored event(s) to {LEDGER_PATH}")
        else:
            print("no events had sufficient forward price data to score yet")

    # Recompute the full accuracy summary across EVERY accumulated day, not just today.
    if not LEDGER_PATH.exists():
        print("no ledger yet, nothing to summarize")
        return
    all_scored = [json.loads(l) for l in LEDGER_PATH.read_text().splitlines() if l.strip()]
    summary = {
        "generated_at": datetime.now(ET).isoformat(),
        "n_days": len(set(r["date"] for r in all_scored)),
        "n_events": len(all_scored),
        "overall_accuracy": round(sum(1 for r in all_scored if r["correct"]) / len(all_scored), 4) if all_scored else None,
        "by_verdict": summarize(all_scored, lambda r: r["verdict"]),
        "by_wss_flag": summarize(all_scored, lambda r: r["wss_flag"]),
        "by_wall_confidence_bucket": summarize(
            all_scored,
            lambda r: "high (wss>=9)" if r["wss_score"] >= 9 else ("holding, standard (0<=wss<9)" if r["wss_score"] >= 0 else "cracking (wss<0)"),
        ),
        "by_confluence_presence": summarize(
            all_scored, lambda r: "confluence" if "no strong signal" not in r["confluence"] else "no confluence"
        ),
    }
    SUMMARY_PATH.write_text(json.dumps(summary, indent=2))
    print(f"wrote {SUMMARY_PATH}")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
