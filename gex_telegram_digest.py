"""gex_telegram_digest.py -- TEMPORARY bridge (2026-07-04) for the WSS/P(C)/
Ghost-Wall/regime picture that's already computing live on the box but
can't be seen on the dashboard yet (Netlify deploy freeze until 2026-07-08).
Sends a compact Telegram digest instead, at the same 3x/day cadence
live_gex_wrapper.sh already runs (called as its last step, no new cron).

Self-expires after 2026-07-08: once the frontend push lands, this exact
data becomes visible on the dashboard itself (modal technicals + chart
overlay), so a redundant Telegram digest stops being worth the noise --
this script just exits quietly past that date rather than needing someone
to remember to disable it.

Scope, to stay short enough for a phone notification: SPY (30-DTE reading,
the only one carrying WSS/P(C) -- see compute_live_0dte()'s docstring,
Phase 2/3 isn't wired into the 0DTE path), SPY-0DTE (regime/flip/walls
only), plus any ticker with a genuinely extreme WSS score.

AUDIT NOTE (2026-07-04): first version filtered on the wss_flag category
(FRACTURED/TRAPDOOR). Real data showed that's not selective at all -- WSS
scores cluster tightly around a median of 0.03 across the live universe,
so the FRACTURED/YIELDING boundary (set at zero) just bisects the universe
roughly in half (83/192 came back FRACTURED; HARD FLOOR/TRAPDOOR never
appeared at all in this sample). Switched to a real score-magnitude
threshold (NOTABLE_SCORE, WSS score more negative than this = trending
toward FRACTURED/TRAPDOOR = the direction actually worth a look) instead --
on the same real data this narrows correctly to the 2-4 names that are
actually extreme (e.g. AVGO -13, WMB -12, WMT -11) rather than ~40% of the
universe.
"""
import json
import subprocess
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo
from datetime import datetime

EXPIRES = date(2026, 7, 8)
ROOT = Path(__file__).resolve().parent
ET = ZoneInfo("America/New_York")
NOTABLE_SCORE = -10.0  # wss_score below this = genuinely extreme, see audit note above


def load_snapshot(name):
    path = ROOT / "data" / name
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text()).get("results", [])
    except Exception:
        return []


def fmt_full(r):
    wss = f"{r.get('wss_flag')} ({r.get('wss_score'):.0f})" if r.get("wss_flag") else "--"
    pc = f"{r.get('p_c') * 100:.0f}%{'*' if r.get('p_c_flow_state_tracked') is False else ''}" if r.get("p_c") is not None else "--"
    flip = f"${r['flip']:.2f}" if r.get("flip") is not None else "--"
    cw = f"${r['call_wall']:.2f}" if r.get("call_wall") is not None else "--"
    pw = f"${r['put_wall']:.2f}" if r.get("put_wall") is not None else "--"
    return (f"{r['ticker']}: {r.get('regime') or 'no read'} | flip {flip} | "
            f"CW {cw} / PW {pw} | WSS {wss} | P(C) {pc}")


def fmt_0dte(r):
    flip = f"${r['flip']:.2f}" if r.get("flip") is not None else "--"
    cw = f"${r['call_wall']:.2f}" if r.get("call_wall") is not None else "--"
    pw = f"${r['put_wall']:.2f}" if r.get("put_wall") is not None else "--"
    return f"{r['ticker']}: {r.get('regime') or 'no read'} | flip {flip} | CW {cw} / PW {pw}"


def send_telegram(message):
    subprocess.run(
        ["openclaw", "message", "send", "--channel", "telegram",
         "--target", "7590346809", "--message", message],
        capture_output=True,
    )


def main():
    if date.today() >= EXPIRES:
        print(f"past {EXPIRES} -- dashboard should be live, this bridge digest is retired, no-op")
        return

    full = load_snapshot("live_gex_snapshot.json")
    zerodte = load_snapshot("live_gex_0dte_snapshot.json")
    full_by_ticker = {r["ticker"]: r for r in full if not r.get("error")}

    lines = [f"GEX digest ({datetime.now(ET).strftime('%Y-%m-%d %H:%M ET')}) -- "
             f"bridge until the dashboard push lands 2026-07-08:"]

    spy = full_by_ticker.get("SPY")
    if spy:
        lines.append(fmt_full(spy))
    spy_0dte = next((r for r in zerodte if r.get("ticker") == "SPY-0DTE" and not r.get("error")), None)
    if spy_0dte:
        lines.append(fmt_0dte(spy_0dte))

    notable = [r for r in full_by_ticker.values()
               if r.get("wss_score") is not None and r["wss_score"] <= NOTABLE_SCORE and r["ticker"] != "SPY"]
    if notable:
        lines.append(f"-- {len(notable)} other names with WSS <= {NOTABLE_SCORE:.0f} (genuinely fractured) --")
        for r in sorted(notable, key=lambda r: r["wss_score"])[:15]:
            lines.append(fmt_full(r))
        if len(notable) > 15:
            lines.append(f"...and {len(notable) - 15} more")
    else:
        lines.append(f"no other names currently below WSS {NOTABLE_SCORE:.0f}")

    lines.append("* = P(C) partial (order-flow state not tracked live yet, see advanced_gex.py)")
    message = "\n".join(lines)
    print(message)
    send_telegram(message)


if __name__ == "__main__":
    main()
