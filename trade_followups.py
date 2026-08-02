#!/usr/bin/env python3
"""trade_followups.py — weekday-morning Telegram nudge of open trading follow-ups.

Pings the user's Telegram pre-market with the things they need to "hit Claude for"
so nothing falls through the cracks. The agent (Claude) MAINTAINS the OPEN_ITEMS
list below — pruning resolved items and adding new ones as the build progresses.

Each item may carry an optional `until` ISO date: the item auto-drops after that
date (so time-sensitive reminders like the token test expire themselves). Items
with no `until` are standing reminders. If nothing is active, no message is sent.

Run:
  ./venv/bin/python trade_followups.py            # send to Telegram
  ./venv/bin/python trade_followups.py --print     # print only (no send) — for testing
"""
import sys
import subprocess
from datetime import datetime, date
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
TG_TARGET = "7590346809"

# --- editable: Claude prunes resolved items + adds new ones ---
# fields: tag (emoji/label), text, optional until="YYYY-MM-DD" (auto-expires after)
OPEN_ITEMS = [
    {"tag": "⏰ TODAY", "until": "2026-06-15",
     "text": "Token Lifetime Test — prompt Claude to run read-only get_accounts pre-market. "
             "Success = OAuth token persists (headless viable); 401 = we must solve headless re-auth."},
    {"tag": "💰",
     "text": "Fund the Agentic account ($200 → ~$800) to match the $800 / 3-slot / $266-cap sizing "
             "before going live."},
    {"tag": "🛑",
     "text": "Decide the stop-leg mechanic (entry limit + a SEPARATE stop order after fill) before "
             "any live trade — RH can't bracket in one call."},
    {"tag": "🔴",
     "text": "System is DORMANT — kill-switch is ENGAGED. Only release it (guardrails.py --unkill) "
             "when consciously going live."},
    {"tag": "📊",
     "text": "Ask Claude for the live paper recap (z=1.5 + Alpaca feed + dollar P&L) to see how the "
             "upgraded stack is performing."},
]


def active_items(today):
    out = []
    for it in OPEN_ITEMS:
        u = it.get("until")
        if u:
            try:
                if today > date.fromisoformat(u):
                    continue   # expired
            except ValueError:
                pass
        out.append(it)
    return out


def build_message(today):
    items = active_items(today)
    if not items:
        return None
    header = f"🔔 Trading follow-ups ({today.strftime('%b %d')}) — things to hit Claude for:"
    lines = [header, ""]
    for it in items:
        lines.append(f"{it['tag']}  {it['text']}")
        lines.append("")
    return "\n".join(lines).rstrip()


def send_telegram(msg):
    try:
        subprocess.run(["openclaw", "message", "send", "--channel", "telegram",
                        "--target", TG_TARGET, "--message", msg],
                       timeout=30, check=False)
    except Exception as e:
        print(f"telegram send failed: {e}", file=sys.stderr)


def main():
    today = datetime.now(ET).date()
    msg = build_message(today)
    if not msg:
        print("no active follow-ups — nothing sent")
        return
    if "--print" in sys.argv:
        print(msg)
        return
    send_telegram(msg)
    print("sent follow-up digest to Telegram")


if __name__ == "__main__":
    main()
