import json
import requests
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
ROOT = Path(".")
KEY = (ROOT / "credentials" / "alpaca_key.txt").read_text().strip()
SEC = (ROOT / "credentials" / "alpaca_secret.txt").read_text().strip()
H = {"APCA-API-KEY-ID": KEY, "APCA-API-SECRET-KEY": SEC}

events = json.loads((ROOT / "data" / "wall_alert_events_parsed.json").read_text())
tickers = sorted(set(e["ticker"] for e in events))
print(f"fetching 5min bars for {len(tickers)} tickers...")

today = datetime.now(ET).date().isoformat()
start = f"{today}T09:30:00-04:00"
end_dt = datetime.now(ET)
end = end_dt.isoformat()

bars_by_ticker = {t: [] for t in tickers}
for i in range(0, len(tickers), 100):
    batch = tickers[i:i + 100]
    page_token = None
    while True:
        params = {"symbols": ",".join(batch), "timeframe": "5Min", "start": start, "end": end,
                   "feed": "iex", "limit": 10000, "sort": "asc"}
        if page_token:
            params["page_token"] = page_token
        r = requests.get("https://data.alpaca.markets/v2/stocks/bars", headers=H, params=params, timeout=30)
        data = r.json()
        for sym, bars in (data.get("bars") or {}).items():
            bars_by_ticker[sym].extend(bars)
        page_token = data.get("next_page_token")
        if not page_token:
            break

print("bars fetched:", {t: len(b) for t, b in list(bars_by_ticker.items())[:5]}, "...")

def parse_ts(bar_t):
    return datetime.fromisoformat(bar_t.replace("Z", "+00:00")).astimezone(ET)

# Pre-sort bars per ticker by time
for t in bars_by_ticker:
    bars_by_ticker[t].sort(key=lambda b: b["t"])

LOOKFORWARD_BARS = 12  # 60 min at 5min cadence

def score_event(ev):
    t = ev["ticker"]
    bars = bars_by_ticker.get(t, [])
    if not bars:
        return None
    alert_dt = datetime.strptime(f"{today} {ev['ts'].split()[1]}", "%Y-%m-%d %H:%M").replace(tzinfo=ET)
    # find first bar at/after alert time
    idx = None
    for i, b in enumerate(bars):
        if parse_ts(b["t"]) >= alert_dt:
            idx = i
            break
    if idx is None:
        return None
    window = bars[idx: idx + LOOKFORWARD_BARS + 1]
    if len(window) < 2:
        return None
    wall = ev["wall_level"]
    cracked = False
    if ev["wall_type"] == "put":
        # support: cracks if price closes BELOW the wall at any point in window
        cracked = any(b["c"] < wall * 0.999 for b in window[1:])
    else:
        # resistance: cracks if price closes ABOVE the wall at any point in window
        cracked = any(b["c"] > wall * 1.001 for b in window[1:])
    predicted_crack = ev["verdict"] == "cracking"
    correct = predicted_crack == cracked
    return {"correct": correct, "actual": "cracked" if cracked else "held", "n_bars_available": len(window) - 1}

scored = []
for ev in events:
    r = score_event(ev)
    if r is not None:
        scored.append({**ev, **r})

print(f"\nscored {len(scored)} / {len(events)} events (rest lacked sufficient forward data)")

def summarize(rows, keyfunc, label):
    from collections import defaultdict
    buckets = defaultdict(list)
    for r in rows:
        buckets[keyfunc(r)].append(r)
    print(f"\n--- by {label} ---")
    for k, rs in sorted(buckets.items(), key=lambda kv: str(kv[0])):
        n = len(rs)
        acc = sum(1 for r in rs if r["correct"]) / n if n else 0
        print(f"  {k!r:30s} n={n:3d}  accuracy={acc:.1%}")

summarize(scored, lambda r: r["wss_flag"], "WSS flag")
summarize(scored, lambda r: r["regime"], "regime")
summarize(scored, lambda r: r["verdict"], "verdict (holding vs cracking)")
summarize(scored, lambda r: "confluence" if "no strong signal" not in r["confluence"] else "no confluence", "confluence presence")
summarize(scored, lambda r: f"p_c<10" if r["p_c"] < 10 else "p_c 10-25" if r["p_c"] < 25 else "p_c 25-50" if r["p_c"] < 50 else "p_c 50+", "P(C) bucket")
summarize(scored, lambda r: "dist<0.1%" if r["dist_pct"] < 0.1 else "dist 0.1-0.2%" if r["dist_pct"] < 0.2 else "dist 0.2-0.3%" if r["dist_pct"] < 0.3 else "dist 0.3%+", "distance-to-wall bucket")
summarize(scored, lambda r: (r["wss_flag"], r["verdict"]), "WSS flag x verdict")
summarize(scored, lambda r: (r["regime"], r["wall_type"]), "regime x wall type")

out = Path("data/wall_alert_scored.json")
out.write_text(json.dumps(scored, indent=2))
print(f"\nwrote {out}")
overall_acc = sum(1 for r in scored if r["correct"]) / len(scored) if scored else 0
print(f"\nOVERALL accuracy: {overall_acc:.1%} ({sum(1 for r in scored if r['correct'])}/{len(scored)})")
