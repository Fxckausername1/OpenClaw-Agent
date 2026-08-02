#!/usr/bin/env python3
"""Daily command-center briefing -> one Telegram message.

Sections: market pulse (SPY/QQQ/VIX) | trading paper-trade P&L + last session |
latest artist-lead batch | follow-ups due today. Writes data/briefing_latest.txt
(the wrapper sends it). Each section fails soft so a missing source never breaks
the whole briefing.
"""
import csv
import json
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf

import follow_up_reminders as fr  # reuse sheet read + due-followup logic

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
OUT_PATH = DATA / "briefing_latest.txt"
ET = ZoneInfo("America/New_York")


def market_pulse():
    out = []
    for sym, label in [("SPY", "S&P"), ("QQQ", "Nasdaq"), ("^VIX", "VIX")]:
        try:
            df = yf.download(sym, period="5d", interval="1d", progress=False, threads=False)
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            df = df.dropna()
            last, prev = float(df["Close"].iloc[-1]), float(df["Close"].iloc[-2])
            chg = (last - prev) / prev * 100
            arrow = "▲" if chg >= 0 else "▼"
            out.append(f"{label} {last:,.2f} {arrow}{abs(chg):.1f}%")
        except Exception:
            pass
    return "  " + " | ".join(out) if out else "  (market data unavailable)"


def paper_section():
    p = DATA / "paper_trades.csv"
    if not p.exists():
        return ["  No paper trades closed yet."]
    rows, rs = [], []
    with p.open() as f:
        for r in csv.DictReader(f):
            try:
                r["_r"] = float(r["outcome_r"]); rows.append(r); rs.append(r["_r"])
            except Exception:
                pass
    if not rs:
        return ["  No paper trades closed yet."]
    wins = [x for x in rs if x > 0]
    lines = [f"  Track record: {len(rs)} trades, {len(wins)/len(rs)*100:.0f}% win, "
             f"{sum(rs)/len(rs):+.2f}R avg, {sum(rs):+.1f}R total"]
    last_date = max(r["close_time"][:10] for r in rows if r.get("close_time"))
    day = [r for r in rows if r.get("close_time", "")[:10] == last_date]
    if day:
        dr = sum(r["_r"] for r in day); w = sum(1 for r in day if r["_r"] > 0)
        lines.append(f"  Last session ({last_date}): {len(day)} closed, "
                     f"{w}W/{len(day)-w}L, {dr:+.1f}R")
    return lines


def leads_section():
    files = sorted(DATA.glob("leads_2*.csv"))
    if not files:
        return ["  No lead batches yet."]
    f = files[-1]
    rows = list(csv.DictReader(f.open()))
    if not rows:
        return ["  Latest lead batch is empty."]
    rows.sort(key=lambda r: int(r.get("score") or 0), reverse=True)
    date = f.stem.replace("leads_", "")
    top = ", ".join(f"@{r['handle']}({r.get('score')})" for r in rows[:3])
    return [f"  Latest batch ({date}): {len(rows)} leads. Top: {top}"]


def followups_section():
    sid = fr.cfg("SHEET_ID", "sheet_id.txt")
    tab = fr.cfg("SHEET_TAB", "sheet_tab.txt", "Sheet1")
    if not sid or not Path(fr.KEY_FILE).exists():
        return ["  (CRM not configured)"]
    try:
        data = json.loads(fr.node(["read", sid, f"'{tab}'!A:N"]) or "{}")
        today = datetime.now(ET).date()
        due = fr.due_followups(data.get("values") or [], today)
        if not due:
            return ["  None due today ✅"]
        names = "; ".join(f"{d['name']} (@{d['handle']})"
                          + (" [overdue]" if d["overdue"] else "") for d in due[:6])
        return [f"  {len(due)} due: {names}"]
    except Exception:
        return ["  (could not read CRM)"]


def main():
    now = datetime.now(ET)
    parts = [f"\U0001F4CA Command Center -- {now.strftime('%A %b %d')}", ""]
    parts.append("\U0001F4C8 Markets")
    parts.append(market_pulse())
    parts.append("")
    parts.append("\U0001F4B0 Trading (paper)")
    parts += paper_section()
    parts.append("")
    parts.append("\U0001F3A4 Artist leads")
    parts += leads_section()
    parts.append("")
    parts.append("⏰ Follow-ups")
    parts += followups_section()

    msg = "\n".join(parts)
    OUT_PATH.write_text(msg)
    print(msg)


if __name__ == "__main__":
    main()
