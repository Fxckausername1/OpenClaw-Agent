"""catalyst_alert.py -- Opportunity: catalyst-awareness trigger (2026-07-04).

Automates the TRIGGER half of the manual XLI/CAT-Burry and SNDK/KLAC-
semiconductor-crash digs from earlier today: when a ticker clears
confluence_score.py's "strong confluence" bar (3+ independent signals, at
most 1 disagreeing), send ONE Telegram alert listing those names so a human
(or a follow-up Claude session) can go find the actual catalyst -- exactly
what a manual web search resolved for CAT and the chip names.

Deliberately does NOT try to auto-fetch news itself. A real news/headlines
API is a new paid dependency this project doesn't have yet (see
[[spend-discipline]]), and UW access -- which every signal here depends on
-- is itself ending in about a week (see [[build-tough-data-independence]]).
Automating the *trigger* is cheap and durable; automating the *lookup* would
add cost right as the underlying data source is going away. This script is
the trigger only.

NOT scheduled on a cron by this change -- deliberately. Every underlying
UW pull (uw_historical_pull.py, pull_iv_rank.py, pull_short_interest.py,
pull_sweep_alerts.py) has so far only been run manually/ad-hoc, not on any
automated schedule. Wiring THIS onto a cron without also scheduling those
would just re-alert on the same stale snapshot every run. Whether to also
schedule the underlying pulls (which would spend part of UW's remaining
trial-week request budget daily) is a real decision left to heff, not made
here.

Run manually: ./venv/bin/python catalyst_alert.py
"""
import subprocess
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from confluence_score import compute_confluence, strength

ET = ZoneInfo("America/New_York")
ROOT = Path(__file__).resolve().parent
STALE_HOURS = 30  # underlying UW pulls are manual/ad-hoc, not daily -- warn rather than alert silently on old data


def data_age_hours():
    """Age of the most recently touched UW data file -- a cheap staleness
    proxy since these pulls aren't on a cron. None if no UW data exists."""
    uw_dir = ROOT / "data" / "unusualwhales"
    if not uw_dir.exists():
        return None
    newest = max((p.stat().st_mtime for p in uw_dir.rglob("*.json")), default=None)
    if newest is None:
        return None
    return (datetime.now().timestamp() - newest) / 3600.0


def send_telegram(message):
    subprocess.run(
        ["openclaw", "message", "send", "--channel", "telegram",
         "--target", "7590346809", "--message", message],
        capture_output=True,
    )


def main():
    age_h = data_age_hours()
    if age_h is None:
        print("no UW data found, nothing to check")
        return
    if age_h > STALE_HOURS:
        print(f"UW data is {age_h:.1f}h old (>{STALE_HOURS}h) -- likely stale, skipping alert "
              f"rather than flag names off an old snapshot. Re-run the pull scripts first.")
        return

    rows = compute_confluence()
    strong = sorted([r for r in rows if strength(r) > 0], key=lambda r: -strength(r))

    if not strong:
        print("no strong-confluence names today")
        return

    now = datetime.now(ET).strftime("%Y-%m-%d %H:%M ET")
    lines = [f"Confluence check ({now}, UW data {age_h:.0f}h old) -- {len(strong)} names worth a manual news check:"]
    for r in strong[:15]:  # cap message length
        lean = "BEARISH" if r["n_bear"] > r["n_bull"] else "BULLISH"
        sig_str = "/".join(sorted(k for k, v in r["signals"].items() if v == lean))
        lines.append(f"  {r['ticker']} {lean} ({r['n_bull']}b/{r['n_bear']}b of {r['n_total']}, {sig_str})")
    if len(strong) > 15:
        lines.append(f"  ...and {len(strong) - 15} more")
    message = "\n".join(lines)

    print(message)
    send_telegram(message)


if __name__ == "__main__":
    main()
