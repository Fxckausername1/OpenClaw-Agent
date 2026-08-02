#!/usr/bin/env python3
"""Weekly paper-trade review for the mean-reversion strategy.

Reads data/paper_trades.csv and writes data/paper_weekly_latest.txt (sent to
Telegram by the wrapper). Shows this week vs all-time, LONG (Robinhood-tradable)
vs SHORT, exit-type breakdown, and a comparison to the backtest LONG benchmark
so we can tell early whether the live edge is holding up.
"""
import csv
from pathlib import Path
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
CSV_PATH = DATA / "paper_trades.csv"
OUT_PATH = DATA / "paper_weekly_latest.txt"
ET = ZoneInfo("America/New_York")

# Backtest LONG benchmark (earnings-filtered, ~59 sessions) for comparison.
BENCH_WR = 66.1
BENCH_EXP = 0.56


def load_rows():
    if not CSV_PATH.exists():
        return []
    rows = []
    with CSV_PATH.open() as f:
        for r in csv.DictReader(f):
            try:
                r["_r"] = float(r["outcome_r"])
            except Exception:
                continue
            try:
                r["_d"] = datetime.fromisoformat(r["close_time"]).date()
            except Exception:
                r["_d"] = None
            rows.append(r)
    return rows


def agg(rows):
    rs = [r["_r"] for r in rows]
    if not rs:
        return None
    wins = [x for x in rs if x > 0]
    losses = [x for x in rs if x <= 0]
    pf = (sum(wins) / abs(sum(losses))) if losses and sum(losses) != 0 else float("inf")
    return dict(n=len(rs), wr=len(wins) / len(rs) * 100,
                exp=sum(rs) / len(rs), total=sum(rs), pf=pf)


def line(label, st):
    if not st:
        return f"{label}: none"
    pf = "inf" if st["pf"] == float("inf") else f"{st['pf']:.2f}"
    return (f"{label}: {st['n']} | win {st['wr']:.0f}% | exp {st['exp']:+.2f}R "
            f"| total {st['total']:+.1f}R | PF {pf}")


def main():
    today = datetime.now(ET).date()
    rows = load_rows()
    out = [f"\U0001F4CA Paper-trade weekly review -- {today.strftime('%b %d')}"]

    if not rows:
        out.append("No paper trades closed yet. Scanner is live and accumulating.")
        OUT_PATH.write_text("\n".join(out))
        print("\n".join(out))
        return

    week_cut = today - timedelta(days=6)
    week = [r for r in rows if r["_d"] and r["_d"] >= week_cut]
    longs = [r for r in rows if r["side"] == "LONG"]
    shorts = [r for r in rows if r["side"] == "SHORT"]

    out.append("")
    out.append(line("This week", agg(week)))
    out.append("")
    out.append(line("All-time", agg(rows)))
    out.append(line("  LONG (RH-tradable)", agg(longs)))
    out.append(line("  SHORT (needs puts)", agg(shorts)))

    reasons = {}
    for r in rows:
        reasons[r["exit_reason"]] = reasons.get(r["exit_reason"], 0) + 1
    out.append("Exits: " + " / ".join(f"{v} {k}" for k, v in sorted(reasons.items())))

    out.append("")
    out.append(f"Backtest LONG benchmark: win {BENCH_WR:.0f}%, exp +{BENCH_EXP:.2f}R")
    ls = agg(longs)
    if not ls or ls["n"] < 10:
        need = 10 - (ls["n"] if ls else 0)
        out.append(f"Status: early sample -- ~{need} more LONG trades for a first read.")
    elif ls["exp"] >= 0.40:
        out.append("Status: holding up vs backtest. Keep accumulating toward go/no-go.")
    else:
        out.append("Status: under-performing backtest -- review before any live step.")

    OUT_PATH.write_text("\n".join(out))
    print("\n".join(out))


if __name__ == "__main__":
    main()
