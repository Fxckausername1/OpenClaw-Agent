#!/usr/bin/env python3
"""trading_status.py — plain-English trading status for Telegram (Big Claw's
`trading-status` skill).

Reads the ALREADY-COMPUTED dashboard snapshot (same source of truth the web
dashboard itself polls, refreshed every ~2min RTH by dashboard_snapshot.py's
own cron) plus the standalone wall-alert accuracy file. Deliberately never
recomputes P&L/win-rate itself from raw CSVs/the options DB -- this codebase
has twice shipped real contamination bugs from a second, drifted P&L
computation (see mean-reversion-strategy / options-tournament history), so
reusing the one already-proven aggregator is the safe choice, not a shortcut.

Run: ./venv/bin/python scripts/trading_status.py [--full]
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SNAPSHOT = Path.home() / "trading-dashboard-snapshot" / "snapshot.json"
WALL_ACCURACY = ROOT / "data" / "wall_alert_accuracy_summary.json"


def _load(path):
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def _age_minutes(iso_ts):
    if not iso_ts:
        return None
    try:
        ts = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
        if ts.tzinfo is None:
            return None
        return (datetime.now(timezone.utc) - ts).total_seconds() / 60
    except Exception:
        return None


def main():
    full = "--full" in sys.argv
    snap = _load(SNAPSHOT)
    if snap is None:
        print("No trading data available right now -- snapshot.json is missing or "
              "unreadable on the box. Say so plainly, don't guess at numbers.")
        return

    lines = []
    age = _age_minutes(snap.get("generated_at"))
    staleness = ""
    if age is not None and age > 15:
        staleness = f" (stale, last updated {age:.0f} min ago -- likely outside market hours)"
    lines.append(
        f"Trading status -- session {snap.get('session_date', '?')}, "
        f"generated {snap.get('generated_at', '?')}{staleness}"
    )

    # --- Equity book (mean-reversion + ORB paper) ---
    m = snap.get("metrics") or {}
    lines.append(
        f"\nEQUITY BOOK (mean-reversion + ORB paper, $1000):\n"
        f"  Today: ${m.get('daily_pnl', 0):+.2f} | {m.get('wins', 0)}W-{m.get('losses', 0)}L "
        f"({m.get('win_rate', 0) * 100:.0f}%) | {m.get('active_trades', 0)} open"
    )
    for pos in (snap.get("open_positions") or [])[:6]:
        lines.append(
            f"    {pos.get('ticker')} {pos.get('side')} @ {pos.get('entry')} "
            f"({pos.get('strategy', '?')})"
        )

    # --- Options tournament ---
    gs = snap.get("guardrail_status") or {}
    eq_halt = gs.get("equity") or {}
    tourn = gs.get("tournament") or {}
    opt_cap = (snap.get("capacity") or {}).get("options") or {}
    lines.append(
        f"\nOPTIONS TOURNAMENT (10-strategy, $150/trade cap):\n"
        f"  Today: ${tourn.get('realized_pnl', 0):+.2f} | halted: "
        f"{'YES' if tourn.get('halted') else 'no'} | "
        f"{opt_cap.get('open', 0)}/{opt_cap.get('max_concurrent', '?')} open"
    )
    board = sorted(snap.get("options_leaderboard") or [],
                    key=lambda r: r.get("realized_pnl", 0), reverse=True)
    traded = [r for r in board if r.get("trades", 0) > 0]
    if traded:
        top = traded[0]
        lines.append(
            f"    Leader (all-time): {top['strategy_id']} "
            f"(${top['realized_pnl']:+.2f}, {top['trades']} trades, "
            f"win~{top['post_mean'] * 100:.0f}%)"
        )
    elif full:
        lines.append("    No strategy has a closed trade yet.")

    # --- Halts, surfaced loudly since this is the thing heff actually needs to know fast ---
    halts = []
    if eq_halt.get("halted_for_day"):
        halts.append("EQUITY BOOK HALTED: " + "; ".join(eq_halt.get("reasons", [])))
    if tourn.get("halted"):
        halts.append(
            f"TOURNAMENT HALTED (today ${tourn.get('realized_pnl', 0):.2f} "
            f"vs limit ${tourn.get('limit', 0):.2f})"
        )
    if halts:
        lines.append("\n!! " + " | ".join(halts))

    # --- GEX flags (ghost walls / near-certain cascade breakdown) ---
    gex_rows = snap.get("gex_view") or []
    ghost = [r["ticker"] for r in gex_rows if r.get("ghost_wall")]
    hot_pc = [r["ticker"] for r in gex_rows if (r.get("p_c") or 0) >= 0.75]
    if ghost or hot_pc:
        bits = []
        if ghost:
            bits.append(f"Ghost Wall: {', '.join(ghost[:8])}")
        if hot_pc:
            bits.append(f"P(C)>=75%: {', '.join(hot_pc[:8])}")
        lines.append("\nGEX flags: " + " | ".join(bits))
    elif full:
        lines.append(f"\nGEX flags: none right now ({len(gex_rows)} tickers checked)")

    # --- Cross-signal confluence (only meaningful if the UW trial is still live -- expires 7/17) ---
    conf = snap.get("confluence") or {}
    if isinstance(conf, dict) and conf.get("available"):
        strong = [r for r in (conf.get("rows") or []) if r.get("n_total", 0) >= 4]
        if strong:
            names = ", ".join(f"{r['ticker']}({r['lean']})" for r in strong[:6])
            lines.append(f"\nConfluence (4+ signals agree): {names}")
    elif full:
        lines.append("\nConfluence: not available right now")

    # --- Wall-alert accuracy (standalone file, not yet folded into snapshot.json) ---
    wa = _load(WALL_ACCURACY)
    if wa:
        headline = f"{wa.get('overall_accuracy', 0) * 100:.0f}% overall"
        hi_bucket = next(
            (b for b in (wa.get("by_wall_confidence_bucket") or [])
             if "high" in b.get("key", "")),
            None,
        )
        if hi_bucket:
            headline += f", {hi_bucket['accuracy'] * 100:.0f}% on high-confidence calls ({hi_bucket['n']} events)"
        lines.append(
            f"\nWall-alert accuracy ({wa.get('n_days', '?')}d / {wa.get('n_events', '?')} events): {headline}"
        )

    if full:
        cap = snap.get("capacity") or {}
        lines.append(f"\nCapacity -- equity: {cap.get('equity')} | options: {cap.get('options')}")

    print("\n".join(lines))


if __name__ == "__main__":
    main()
