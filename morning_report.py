#!/usr/bin/env python3
"""morning_report.py — ONE consolidated pre-market brief (replaces the separate Command
Center / follow-up / artist pings). Self-contained: pulls markets (FIXED index data),
autonomous-bot + paper track-record status, an artist-leads SUMMARY (full DMs left in the
CSV per heff), and follow-ups. Sends a single clean Telegram message.

Run: ./venv/bin/python morning_report.py  [--dry-run]
"""
import csv
import re
import sys
import glob
import argparse
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
ET = ZoneInfo("America/New_York")
TG = "7590346809"
MSG_OUT = DATA / "morning_report_latest.txt"


def money(d):
    return f"+${d:,.2f}" if d >= 0 else f"-${abs(d):,.2f}"


def markets():
    try:
        import yfinance as yf
    except Exception:
        return ["  (market data unavailable)"]
    out = []
    for sym, name in (("^GSPC", "S&P 500"), ("^IXIC", "Nasdaq"), ("^VIX", "VIX")):
        try:
            h = yf.Ticker(sym).history(period="5d")["Close"].dropna()
            last = float(h.iloc[-1]); prev = float(h.iloc[-2])
            chg = (last / prev - 1) * 100
            arrow = "▲" if chg >= 0 else "▼"
            out.append(f"  {name:<8} {last:,.0f}  {arrow}{abs(chg):.1f}%")
        except Exception:
            out.append(f"  {name:<8} n/a")
    return out


def kill_switch_off():
    return not (DATA / "kill_switch.flag").exists()


def alpaca_open_count():
    try:
        import alpaca_executor as ax
        return len(ax.Alpaca().positions())
    except Exception:
        return None


def track(path):
    if not path.exists():
        return None
    rs, ds = [], []
    with path.open() as f:
        for r in csv.DictReader(f):
            try:
                rs.append(float(r["outcome_r"])); ds.append(float(r.get("dollar_pnl") or 0))
            except Exception:
                pass
    if not rs:
        return None
    w = sum(1 for r in rs if r > 0)
    return f"{len(rs)} trades · {w/len(rs)*100:.0f}% win · {sum(rs):+.1f}R · {money(sum(ds))}"


def artist_summary():
    # the curated daily batch: leads_<YYYY-MM-DD>.csv (excludes _master and _unqualified)
    files = sorted(f for f in glob.glob(str(DATA / "leads_*.csv"))
                   if re.search(r"leads_\d{4}-\d{2}-\d{2}\.csv$", f))
    if not files:
        return ["  no recent leads file"]
    latest = Path(files[-1])
    rows = []
    with latest.open() as f:
        for r in csv.DictReader(f):
            rows.append(r)
    if not rows:
        return [f"  0 leads ({latest.name})"]
    def score(r):
        try:
            return float(r.get("score") or 0)
        except Exception:
            return 0.0
    rows.sort(key=score, reverse=True)
    out = [f"  {len(rows)} leads in {latest.name}"]
    for r in rows[:3]:
        handle = r.get("handle") or r.get("username") or r.get("instagram") or "?"
        out.append(f"    • {handle} (score {score(r):.0f})")
    out.append(f"  → full DMs: {latest}")
    return out


def followups():
    msg = DATA / "followup_message_latest.txt"
    if msg.exists() and msg.read_text().strip():
        return ["  " + ln for ln in msg.read_text().strip().splitlines()[:6]]
    return ["  none due today ✅"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    L = [f"☀️ Morning Brief — {datetime.now(ET).strftime('%a %b %d')}",
         "━━━━━━━━━━━━━━━━━",
         "📈 Markets (overnight / prior close)"]
    L += markets()
    L += ["",
          "🤖 Trading bot (paper)"]
    armed = "ARMED · kill-switch OFF" if kill_switch_off() else "HALTED · kill-switch ON"
    oc = alpaca_open_count()
    L.append(f"  Status: {armed}" + (f" · {oc} open positions" if oc is not None else ""))
    mr = track(DATA / "paper_trades.csv"); orb = track(DATA / "orb_paper_trades.csv")
    if mr:
        L.append(f"  Mean reversion: {mr}")
    if orb:
        L.append(f"  ORB momentum:   {orb}")
    L += ["", "🎤 Artist leads"]
    L += artist_summary()
    L += ["", "✅ Follow-ups"]
    L += followups()

    msg = "\n".join(L)
    MSG_OUT.write_text(msg)
    print(msg)
    if not a.dry_run:
        # Reliable send (2026-07-04): ported verbatim from night_report.py's 2026-06-30 fix.
        # night_report got this hardening after dropped recaps (Jun 25 + Jun 29), but this
        # script never did -- and the ecosystem audit found 6 of 15 morning briefs had
        # raised TimeoutExpired UNCAUGHT on the bare timeout=40 send (9:50 ET is peak cron
        # load on this single-core box, so a slow openclaw return is routine, not rare).
        # The msg is already saved to MSG_OUT above, so even a total failure is
        # recoverable from disk.
        import subprocess
        import time as _time
        sent = False
        for attempt in range(1, 5):
            try:
                r = subprocess.run(["/usr/bin/openclaw", "message", "send", "--channel", "telegram",
                                    "--target", TG, "--message", msg], timeout=90, check=False)
                if r.returncode == 0:
                    sent = True
                    break
                print(f"morning_report telegram attempt {attempt}/4: openclaw exit {r.returncode}")
            except Exception as e:
                print(f"morning_report telegram attempt {attempt}/4 failed: {e}")
            _time.sleep(8 * attempt)        # 8s, 16s, 24s backoff
        print("morning_report: brief SENT" if sent
              else f"morning_report: ALL SEND ATTEMPTS FAILED -- brief saved at {MSG_OUT}")


if __name__ == "__main__":
    main()
