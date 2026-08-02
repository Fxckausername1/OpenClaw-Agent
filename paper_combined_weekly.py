#!/usr/bin/env python3
"""Combined weekly paper-trade review — mean reversion + ORB side by side.

Reads data/paper_trades.csv (mean-rev) and data/orb_paper_trades.csv (ORB),
shows this-week + all-time for each vs its net-of-cost backtest target, and a
combined total. One Friday Telegram digest for the whole two-edge portfolio.
"""
import csv
from pathlib import Path
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
ET = ZoneInfo("America/New_York")
MR_CSV = DATA / "paper_trades.csv"
ORB_CSV = DATA / "orb_paper_trades.csv"
OUT = DATA / "combined_weekly_latest.txt"
MR_BENCH, ORB_BENCH = 0.18, 0.07   # net-of-cost backtest targets


def load(csvf):
    rows = []
    if not csvf.exists():
        return rows
    with csvf.open() as f:
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
    w = [x for x in rs if x > 0]
    return dict(n=len(rs), wr=len(w) / len(rs) * 100, exp=sum(rs) / len(rs), total=sum(rs))


def line(label, a, bench=None):
    if not a:
        return f"{label}: none yet"
    s = f"{label}: {a['n']} | win {a['wr']:.0f}% | exp {a['exp']:+.2f}R | total {a['total']:+.1f}R"
    if bench is not None:
        s += f"  (target ~+{bench:.2f}R)"
    return s


def main():
    today = datetime.now(ET).date()
    wk = today - timedelta(days=6)
    mr, orb = load(MR_CSV), load(ORB_CSV)

    out = [f"\U0001F4CA Combined weekly review -- {today.strftime('%b %d')}", ""]
    out.append("\U0001F4C8 Mean reversion (core)")
    out.append("  " + line("This week", agg([r for r in mr if r["_d"] and r["_d"] >= wk])))
    out.append("  " + line("All-time", agg(mr), MR_BENCH))
    out.append("")
    out.append("\U0001F680 ORB momentum (diversifier)")
    out.append("  " + line("This week", agg([r for r in orb if r["_d"] and r["_d"] >= wk])))
    out.append("  " + line("All-time", agg(orb), ORB_BENCH))
    out.append("")
    out.append("\U0001F517 Combined")
    out.append("  " + line("All-time", agg(mr + orb)))

    ma = agg(mr)
    if not ma or ma["n"] < 10:
        need = 10 - (ma["n"] if ma else 0)
        out.append(f"  Status: early -- ~{need} more mean-rev trades for a first read.")
    elif ma["exp"] >= MR_BENCH * 0.7:
        out.append("  Status: mean-rev holding up vs backtest. Keep accumulating toward go/no-go.")
    else:
        out.append("  Status: mean-rev under its backtest target -- review before any live step.")

    OUT.write_text("\n".join(out))
    print("\n".join(out))


if __name__ == "__main__":
    main()
