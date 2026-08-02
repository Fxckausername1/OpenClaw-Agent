#!/usr/bin/env python3
"""rader_watchlist_alert.py — same-day Telegram price alert for today's Rader
Report watchlist (ARM/NVDA/NOW/MSFT). Self-expires via WATCHLIST_DATE (also
matched by the cron's own day-of-month/month fields, belt-and-suspenders) so
a stray run on a later day can't fire on today's stale trigger levels.

Reuses fetch_live_prices()/send_telegram() from wall_proximity_alert.py
rather than reimplementing the Alpaca snapshot call and Telegram send.

Run: ./venv/bin/python rader_watchlist_alert.py
"""
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from wall_proximity_alert import fetch_live_prices, send_telegram

ET = ZoneInfo("America/New_York")
ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
STATE_PATH = DATA / "rader_watchlist_state.json"

WATCHLIST_DATE = "2026-07-07"

# Levels/thresholds taken directly from today's Rader Report thesis for each name.
WATCHLIST = {
    "ARM": {"trigger": "below", "level": 306.0,
            "note": "Rader short trigger -- breakdown below 305-306"},
    "NOW": {"trigger": "below", "level": 110.0,
            "note": "Rader long invalidation -- thesis needs price to hold above 110"},
    "NVDA": {"trigger": "pct_move", "pct": 2.0,
             "note": "Rader options/OVI play, no hard price trigger given -- generic move alert"},
    "MSFT": {"trigger": "pct_move", "pct": 2.0,
             "note": "Rader software-rotation long, no hard price trigger given -- generic move alert"},
}


def load_state():
    if not STATE_PATH.exists():
        return {"date": WATCHLIST_DATE, "reference": {}, "fired": {}}
    state = json.loads(STATE_PATH.read_text())
    if state.get("date") != WATCHLIST_DATE:
        return {"date": WATCHLIST_DATE, "reference": {}, "fired": {}}
    return state


def save_state(state):
    DATA.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state))


def main():
    today = datetime.now(ET).date().isoformat()
    if today != WATCHLIST_DATE:
        print(f"watchlist expired ({WATCHLIST_DATE}), today is {today} -- no-op")
        return

    prices = fetch_live_prices(list(WATCHLIST.keys()))
    state = load_state()
    reference = state["reference"]
    fired = state["fired"]

    alerts = []
    for ticker, cfg in WATCHLIST.items():
        price = prices.get(ticker)
        if not price:
            print(f"  [warn] no live price for {ticker}")
            continue
        if ticker not in reference:
            reference[ticker] = price  # first observation today = reference for pct-move

        if cfg["trigger"] == "below":
            key = f"{ticker}_below_{cfg['level']}"
            if price < cfg["level"] and not fired.get(key):
                alerts.append(f"{ticker} ${price:.2f} broke BELOW ${cfg['level']:.2f} -- {cfg['note']}")
                fired[key] = True
            elif price >= cfg["level"] and fired.get(key):
                fired[key] = False  # re-arm on reclaim so a later re-break fires again

        elif cfg["trigger"] == "pct_move":
            ref = reference[ticker]
            move_pct = (price - ref) / ref * 100.0
            key = f"{ticker}_move"
            if abs(move_pct) >= cfg["pct"] and not fired.get(key):
                direction = "up" if move_pct > 0 else "down"
                alerts.append(
                    f"{ticker} ${price:.2f} moved {move_pct:+.2f}% {direction} "
                    f"from today's reference (${ref:.2f}) -- {cfg['note']}"
                )
                fired[key] = True

    if alerts:
        msg = "RADER WATCHLIST ALERT\n\n" + "\n\n".join(alerts)
        send_telegram(msg)
        print(msg)
    else:
        print("no triggers hit this check")

    save_state(state)


if __name__ == "__main__":
    main()
