#!/usr/bin/env python3
import json, requests
from datetime import datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
KEY = open("credentials/alpaca_key.txt").read().strip()
SEC = open("credentials/alpaca_secret.txt").read().strip()
H = {"APCA-API-KEY-ID": KEY, "APCA-API-SECRET-KEY": SEC}

events = [
    {"ticker": "HRL", "date": "2026-07-06", "time": "09:30", "wall_level": 25.0},
    {"ticker": "FISV", "date": "2026-07-06", "time": "10:10", "wall_level": 51.0},
    {"ticker": "UAL", "date": "2026-07-06", "time": "10:10", "wall_level": 135.0},
    {"ticker": "SNDK", "date": "2026-07-07", "time": "09:35", "wall_level": 1600.0},
    {"ticker": "MRVL", "date": "2026-07-07", "time": "09:55", "wall_level": 230.0},
    {"ticker": "LITE", "date": "2026-07-07", "time": "10:10", "wall_level": 700.0},
    {"ticker": "MPWR", "date": "2026-07-07", "time": "10:10", "wall_level": 1260.0},
    {"ticker": "TER", "date": "2026-07-07", "time": "10:15", "wall_level": 340.0},
    {"ticker": "MU", "date": "2026-07-07", "time": "10:25", "wall_level": 900.0},
    {"ticker": "T", "date": "2026-07-07", "time": "10:30", "wall_level": 21.0},
    {"ticker": "WY", "date": "2026-07-08", "time": "09:30", "wall_level": 23.0},
    {"ticker": "MOS", "date": "2026-07-08", "time": "09:35", "wall_level": 21.0},
    {"ticker": "MRVL", "date": "2026-07-08", "time": "10:50", "wall_level": 230.0},
    {"ticker": "LITE", "date": "2026-07-08", "time": "10:50", "wall_level": 700.0},
    {"ticker": "TER", "date": "2026-07-08", "time": "11:10", "wall_level": 340.0},
    {"ticker": "SNDK", "date": "2026-07-08", "time": "11:30", "wall_level": 1600.0},
]

by_date = {}
for e in events:
    by_date.setdefault(e["date"], []).append(e)

results = []
for date_str, evs in by_date.items():
    tickers = sorted(set(e["ticker"] for e in evs))
    start = f"{date_str}T09:30:00-04:00"
    end = f"{date_str}T16:05:00-04:00"
    bars_by_ticker = {t: [] for t in tickers}
    page_token = None
    while True:
        params = {"symbols": ",".join(tickers), "timeframe": "5Min", "start": start, "end": end,
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
    for t in bars_by_ticker:
        bars_by_ticker[t].sort(key=lambda b: b["t"])

    for e in evs:
        bars = bars_by_ticker.get(e["ticker"], [])
        alert_dt = datetime.strptime("{} {}".format(e["date"], e["time"]), "%Y-%m-%d %H:%M").replace(tzinfo=ET)
        idx = None
        for i, b in enumerate(bars):
            bt = datetime.fromisoformat(b["t"].replace("Z", "+00:00")).astimezone(ET)
            if bt >= alert_dt:
                idx = i
                break
        if idx is None:
            results.append(dict(e, error="no bars found after alert"))
            continue
        wall = e["wall_level"]
        window = bars[idx:]
        crack_idx = None
        for j, b in enumerate(window):
            if b["c"] < wall * 0.999:
                crack_idx = j
                break
        if crack_idx is None:
            results.append(dict(e, cracked_at_all=False, sustained_min=None))
            continue
        run_bars = 0
        for k in range(crack_idx, len(window)):
            if window[k]["c"] < wall * 0.999:
                run_bars += 1
            else:
                break
        sustained_min = run_bars * 5
        crack_time = datetime.fromisoformat(window[crack_idx]["t"].replace("Z", "+00:00")).astimezone(ET).strftime("%H:%M")
        eod_close = window[-1]["c"]
        results.append(dict(e, cracked_at_all=True, crack_time=crack_time,
                             sustained_min=sustained_min, reverted_before_eod=run_bars < (len(window) - crack_idx),
                             eod_close=eod_close, eod_vs_wall_pct=round((eod_close - wall) / wall * 100, 2)))

for r in results:
    print(json.dumps(r))

n_cracked = len([r for r in results if r.get("cracked_at_all")])
sustained_30 = [r for r in results if r.get("sustained_min") is not None and r["sustained_min"] >= 30]
print("")
print("total events: {}".format(len(results)))
print("re-confirmed cracked (walked to EOD): {}".format(n_cracked))
print("stayed continuously below wall for >=30 min: {}".format(len(sustained_30)))
for r in sustained_30:
    print("  {} {} crack@{} sustained {}min".format(r["ticker"], r["date"], r["crack_time"], r["sustained_min"]))
