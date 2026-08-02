#!/usr/bin/env python3
"""night_report.py — ONE consolidated AT-CLOSE trading report (replaces the split 5:30pm
recaps). Runs the EOD evaluators, reads the trade CSVs directly for clean formatting, folds
in the Alpaca real-fill recon, and sends a single Telegram message right after the close.
Friday also appends the weekly review.

Fires ~16:05 ET (20:05 UTC) — the 5-min wait lets the final 5-min bars settle for the EOD eval.
Run: ./venv/bin/python night_report.py  [--dry-run]   (--dry-run: compose+print, no eval/send)
"""
import csv
import sys
import subprocess
import argparse
from pathlib import Path
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
ET = ZoneInfo("America/New_York")
PY = str(ROOT / "venv" / "bin" / "python")
TG = "7590346809"
MR_CSV = DATA / "paper_trades.csv"
ORB_CSV = DATA / "orb_paper_trades.csv"
MSG_OUT = DATA / "night_report_latest.txt"


def run(*args, timeout=300):
    try:
        return subprocess.run([PY, str(ROOT / args[0])] + list(args[1:]), cwd=ROOT,
                              capture_output=True, text=True, timeout=timeout)
    except Exception as e:
        return None


def read_rows(path):
    if not path.exists():
        return []
    out = []
    with path.open() as f:
        for r in csv.DictReader(f):
            try:
                r["_r"] = float(r["outcome_r"]); r["_d"] = float(r.get("dollar_pnl") or 0)
                out.append(r)
            except Exception:
                pass
    return out


def money(d):
    return f"+${d:,.2f}" if d >= 0 else f"-${abs(d):,.2f}"


def day_line(rows, today, label):
    today_rows = [r for r in rows if (r.get("close_time") or "")[:10] == today]
    if not today_rows:
        return f"  {label:<16} no trades closed", 0.0, 0.0
    w = sum(1 for r in today_rows if r["_r"] > 0)
    R = sum(r["_r"] for r in today_rows); D = sum(r["_d"] for r in today_rows)
    return (f"  {label:<16} {len(today_rows)} closed · {w}W/{len(today_rows)-w}L · "
            f"{R:+.1f}R · {money(D)}", R, D)


def alltime_line(rows, label):
    if not rows:
        return f"  {label:<16} no trades yet"
    w = sum(1 for r in rows if r["_r"] > 0)
    R = sum(r["_r"] for r in rows); D = sum(r["_d"] for r in rows)
    return (f"  {label:<16} {len(rows)} trades · {w/len(rows)*100:.0f}% win · "
            f"{R:+.1f}R total · {money(D)}")


def recon_section(dry):
    if dry:
        return ["  (dry-run — recon not run)"]
    r = run("alpaca_recon.py")
    if r is None or not r.stdout:
        return ["  (no autonomous orders yet)"]
    keep = [ln for ln in r.stdout.splitlines()
            if any(k in ln for k in ("orders:", "REAL fills", "IDEALIZED", "slippage", "GO-LIVE"))]
    return ["  " + ln.strip() for ln in keep] or ["  (no fills to reconcile yet)"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true", help="run even if market not yet closed")
    a = ap.parse_args()
    now = datetime.now(ET)
    today = now.date().isoformat()
    # only run after the close (DST-safe: cron is fixed UTC; this gate keeps --eod from firing
    # mid-session in winter). --dry-run/--force bypass.
    if not a.dry_run and not a.force and (now.weekday() >= 5 or now.time() < dtime(16, 0)):
        print(f"market not closed yet ({now.strftime('%H:%M %Z')}); skipping night report.")
        return

    if not a.dry_run:
        run("paper_eval.py", "--eod")        # close mean-rev at EOD, update CSV
        run("orb_paper_eval.py", "--eod")    # close ORB at EOD, update CSV

    mr = read_rows(MR_CSV); orb = read_rows(ORB_CSV)
    mr_day, mrR, mrD = day_line(mr, today, "Mean reversion")
    orb_day, orbR, orbD = day_line(orb, today, "ORB momentum")

    L = [f"🌙 Market Close — {datetime.now(ET).strftime('%a %b %d')}",
         "━━━━━━━━━━━━━━━━━",
         "💰 Today's paper trades",
         mr_day, orb_day,
         f"  {'Day net':<16} {mrR+orbR:+.1f}R · {money(mrD+orbD)}",
         "",
         "🤖 Autonomous bot (Alpaca paper · real fills)"]
    L += recon_section(a.dry_run)
    L += ["",
          "📊 All-time track record",
          alltime_line(mr, "Mean reversion"),
          alltime_line(orb, "ORB momentum")]

    if datetime.now(ET).weekday() == 4 and not a.dry_run:   # Friday weekly review
        run("paper_combined_weekly.py")
        wk = DATA / "combined_weekly_latest.txt"
        if wk.exists():
            L += ["", wk.read_text().strip()]

    msg = "\n".join(L)
    MSG_OUT.write_text(msg)
    print(msg)
    if not a.dry_run:
        # Reliable send (2026-06-30): the daily recap kept getting DROPPED because a single openclaw
        # send timed out under load and raised TimeoutExpired UNCAUGHT (Jun 25 + Jun 29 both died this
        # way -> heff never saw the recap). Now: longer timeout + retry with backoff, fully caught so a
        # slow/failed send logs loudly but never crashes the report. The msg is also saved to MSG_OUT
        # above, so even a total failure is recoverable from disk.
        import time as _time
        sent = False
        for attempt in range(1, 5):
            try:
                r = subprocess.run(["/usr/bin/openclaw", "message", "send", "--channel", "telegram",
                                    "--target", TG, "--message", msg], timeout=90, check=False)
                if r.returncode == 0:
                    sent = True
                    break
                print(f"night_report telegram attempt {attempt}/4: openclaw exit {r.returncode}")
            except Exception as e:
                print(f"night_report telegram attempt {attempt}/4 failed: {e}")
            _time.sleep(8 * attempt)        # 8s, 16s, 24s backoff
        print("night_report: recap SENT" if sent
              else f"night_report: recap NOT sent after 4 tries -- saved at {MSG_OUT}")


if __name__ == "__main__":
    main()
