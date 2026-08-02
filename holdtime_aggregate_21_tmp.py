#!/usr/bin/env python3
import json, statistics, requests
from datetime import datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
KEY = open("credentials/alpaca_key.txt").read().strip()
SEC = open("credentials/alpaca_secret.txt").read().strip()
H = {"APCA-API-KEY-ID": KEY, "APCA-API-SECRET-KEY": SEC}

events = [
    {"ticker": "HRL", "date": "2026-07-06", "time": "09:30", "wall_level": 25.0},
    {"ticker": "GLW", "date": "2026-07-06", "time": "09:35", "wall_level": 200.0},
    {"ticker": "FISV", "date": "2026-07-06", "time": "10:10", "wall_level": 51.0},
    {"ticker": "UAL", "date": "2026-07-06", "time": "10:10", "wall_level": 135.0},
    {"ticker": "MDLZ", "date": "2026-07-06", "time": "10:45", "wall_level": 59.0},
    {"ticker": "LYB", "date": "2026-07-06", "time": "13:40", "wall_level": 52.5},
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
    {"ticker": "PSKY", "date": "2026-07-09", "time": "09:45", "wall_level": 9.0},
    {"ticker": "T", "date": "2026-07-09", "time": "09:50", "wall_level": 21.0},
]

by_date = {}
for e in events:
    by_date.setdefault(e["date"], []).append(e)

bars_by_date_ticker = {}
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
    bars_by_date_ticker[date_str] = bars_by_ticker

DURATIONS_MIN = [5, 15, 30, 60, 90, 120, 150]
per_event_returns = []  # list of dicts: {duration_label: pct_return or None}
sustained_results = []

for e in events:
    bars = bars_by_date_ticker[e["date"]].get(e["ticker"], [])
    alert_dt = datetime.strptime("{} {}".format(e["date"], e["time"]), "%Y-%m-%d %H:%M").replace(tzinfo=ET)
    idx = None
    for i, b in enumerate(bars):
        bt = datetime.fromisoformat(b["t"].replace("Z", "+00:00")).astimezone(ET)
        if bt >= alert_dt:
            idx = i
            break
    if idx is None:
        print(f"WARN: no bars for {e['ticker']} {e['date']}")
        continue
    wall = e["wall_level"]
    entry_price = bars[idx]["c"]
    window = bars[idx:]

    crack_idx = None
    for j, b in enumerate(window):
        if b["c"] < wall * 0.999:
            crack_idx = j
            break
    sres = {"ticker": e["ticker"], "date": e["date"]}
    if crack_idx is not None:
        run_bars = 0
        for k in range(crack_idx, len(window)):
            if window[k]["c"] < wall * 0.999:
                run_bars += 1
            else:
                break
        sres["sustained_min"] = run_bars * 5
        sres["reverted_before_eod"] = run_bars < (len(window) - crack_idx)
    sustained_results.append(sres)

    rec = {"ticker": e["ticker"], "date": e["date"]}
    for dur in DURATIONS_MIN:
        bars_needed = dur // 5
        if bars_needed < len(window):
            px = window[bars_needed]["c"]
            rec[f"{dur}min"] = (px - entry_price) / entry_price * 100
        else:
            rec[f"{dur}min"] = None
    eod_close = window[-1]["c"]
    rec["EOD"] = (eod_close - entry_price) / entry_price * 100
    per_event_returns.append(rec)

print(f"n events with usable bars: {len(per_event_returns)}")
print()
print(f"{'Duration':<10}{'Avg %':>10}{'Median %':>12}{'Win rate':>12}{'n':>6}")
for dur_label in [f"{d}min" for d in DURATIONS_MIN] + ["EOD"]:
    vals = [r[dur_label] for r in per_event_returns if r[dur_label] is not None]
    if not vals:
        continue
    avg = sum(vals) / len(vals)
    med = statistics.median(vals)
    win = sum(1 for v in vals if v > 0) / len(vals) * 100
    print(f"{dur_label:<10}{avg:>10.2f}{med:>12.2f}{win:>11.1f}%{len(vals):>6}")

print()
print("=== sustained-crack duration ===")
cracked_n = sum(1 for s in sustained_results if "sustained_min" in s)
sustained_30plus = sum(1 for s in sustained_results if s.get("sustained_min", 0) >= 30)
reverted = sum(1 for s in sustained_results if s.get("reverted_before_eod"))
print(f"cracked at all: {cracked_n}/{len(sustained_results)}")
print(f"sustained >=30min: {sustained_30plus}/{cracked_n}")
print(f"reverted before EOD: {reverted}/{cracked_n}")
