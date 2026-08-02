#!/usr/bin/env python3
import json, requests
from datetime import datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
KEY = open("credentials/alpaca_key.txt").read().strip()
SEC = open("credentials/alpaca_secret.txt").read().strip()
H = {"APCA-API-KEY-ID": KEY, "APCA-API-SECRET-KEY": SEC}

events = [
    {"ticker": "PSKY", "date": "2026-07-09", "time": "09:45", "wall_level": 9.0},
    {"ticker": "T", "date": "2026-07-09", "time": "09:50", "wall_level": 21.0},
]

date_str = "2026-07-09"
tickers = sorted(set(e["ticker"] for e in events))
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

DURATIONS_MIN = [5, 15, 30, 60, 90, 120, 150]

for e in events:
    bars = bars_by_ticker.get(e["ticker"], [])
    alert_dt = datetime.strptime("{} {}".format(e["date"], e["time"]), "%Y-%m-%d %H:%M").replace(tzinfo=ET)
    idx = None
    for i, b in enumerate(bars):
        bt = datetime.fromisoformat(b["t"].replace("Z", "+00:00")).astimezone(ET)
        if bt >= alert_dt:
            idx = i
            break
    if idx is None:
        print(json.dumps(dict(e, error="no bars found after alert")))
        continue
    wall = e["wall_level"]
    entry_price = bars[idx]["c"]
    window = bars[idx:]

    # sustained-crack analysis
    crack_idx = None
    for j, b in enumerate(window):
        if b["c"] < wall * 0.999:
            crack_idx = j
            break
    result = {"ticker": e["ticker"], "alert_time": e["time"], "wall": wall, "entry_price": entry_price}
    if crack_idx is None:
        result["cracked_at_all"] = False
    else:
        run_bars = 0
        for k in range(crack_idx, len(window)):
            if window[k]["c"] < wall * 0.999:
                run_bars += 1
            else:
                break
        result["cracked_at_all"] = True
        result["crack_time"] = datetime.fromisoformat(window[crack_idx]["t"].replace("Z", "+00:00")).astimezone(ET).strftime("%H:%M")
        result["sustained_min"] = run_bars * 5
        result["reverted_before_eod"] = run_bars < (len(window) - crack_idx)

    # fixed-duration returns (long-direction since these are "holding" put-wall calls,
    # i.e. the implied trade if you believed the "holding" verdict was RIGHT and went long
    # expecting a bounce -- same convention as the 2026-07-08 hold-time optimization)
    returns = {}
    for dur in DURATIONS_MIN:
        bars_needed = dur // 5
        if bars_needed < len(window):
            px = window[bars_needed]["c"]
            returns[f"{dur}min"] = round((px - entry_price) / entry_price * 100, 2)
        else:
            returns[f"{dur}min"] = None
    eod_close = window[-1]["c"]
    returns["EOD"] = round((eod_close - entry_price) / entry_price * 100, 2)
    result["returns_pct"] = returns
    result["eod_close"] = eod_close
    result["eod_vs_wall_pct"] = round((eod_close - wall) / wall * 100, 2)

    print(json.dumps(result, indent=2))
