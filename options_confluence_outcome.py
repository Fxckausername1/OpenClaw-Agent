#!/usr/bin/env python3
"""options_confluence_outcome.py -- READ-ONLY analysis: does the GEX/confluence tag
recorded at entry (options_confluence_tag.py, wired into options_orchestrator.py's
run_tournament()) actually correlate with CLOSED-trade outcomes?

Groups CLOSED trades_ledger rows by confluence_agrees / gex_regime_agrees
(True/False/None) and reports count + avg realized_pnl + avg r_multiple per group.

Deliberately just a report, never a gate -- same posture as the tag itself. Does
NOT write anything, does NOT touch options_orchestrator.py's selection logic.

Honesty requirement (heff's spec): with ~1 real trade ever in this tournament,
any stat here is almost certainly n=0 or n=1. Say so plainly rather than
printing a misleadingly confident average on no real sample size.

Usage:
    ./venv/bin/python options_confluence_outcome.py
"""
import json
import sqlite3
from collections import defaultdict

from options_eval import DB_PATH

MIN_N_FOR_A_READ = 10  # arbitrary but explicit -- nowhere near "validated," just "worth a first look"


def load_closed_with_tags():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT trade_id, strategy_id, realized_pnl, r_multiple, legs_metadata "
        "FROM trades_ledger WHERE status='CLOSED'"
    ).fetchall()
    conn.close()

    out = []
    for r in rows:
        try:
            meta = json.loads(r["legs_metadata"]) if r["legs_metadata"] else {}
        except (ValueError, TypeError):
            meta = {}
        tag = meta.get("confluence")  # None for any trade recorded before this change
        out.append({
            "trade_id": r["trade_id"],
            "strategy_id": r["strategy_id"],
            "realized_pnl": r["realized_pnl"],
            "r_multiple": r["r_multiple"],
            "tag": tag,
        })
    return out


def _group_and_report(trades, key_fn, label):
    groups = defaultdict(list)
    for t in trades:
        groups[key_fn(t)].append(t)

    print(f"\n-- grouped by {label} --")
    for key in (True, False, None):
        g = groups.get(key, [])
        if not g:
            print(f"  {key!s:5}: 0 trades")
            continue
        pnls = [t["realized_pnl"] for t in g if t["realized_pnl"] is not None]
        rs = [t["r_multiple"] for t in g if t["r_multiple"] is not None]
        avg_pnl = sum(pnls) / len(pnls) if pnls else None
        avg_r = sum(rs) / len(rs) if rs else None
        avg_pnl_s = f"{avg_pnl:.2f}" if avg_pnl is not None else "n/a"
        avg_r_s = f"{avg_r:.2f}" if avg_r is not None else "n/a"
        print(f"  {key!s:5}: n={len(g)}  avg_realized_pnl=${avg_pnl_s}  avg_r_multiple={avg_r_s}")


def main():
    all_closed = load_closed_with_tags()
    tagged = [t for t in all_closed if t["tag"] is not None]

    print(f"CLOSED trades in ledger: {len(all_closed)}")
    print(f"CLOSED trades with a confluence tag: {len(tagged)}")

    if len(tagged) == 0:
        print("\nN=0 closed trades with a confluence tag (need more history before this "
              "means anything). This is expected: the tag was only just wired in "
              "(2026-07-05) and this tournament has ~1 real trade ever. Re-run this "
              "script after more tagged trades have gone through a full entry/exit cycle.")
        return

    if len(tagged) < MIN_N_FOR_A_READ:
        print(f"\nOnly {len(tagged)} closed trade(s) with a confluence tag (need more "
              f"history before this means anything -- {MIN_N_FOR_A_READ}+ is a bare minimum "
              "for even a first informal look, and real statistical confidence needs far "
              "more than that). Printing the raw per-trade breakdown below for visibility, "
              "NOT as a validated stat.")
        for t in tagged:
            print(f"  {t['trade_id']}  strat={t['strategy_id']}  pnl={t['realized_pnl']}  "
                  f"r={t['r_multiple']}  tag={t['tag']}")
        return

    _group_and_report(tagged, lambda t: t["tag"].get("confluence_agrees"), "confluence_agrees")
    _group_and_report(tagged, lambda t: t["tag"].get("gex_regime_agrees"), "gex_regime_agrees")


if __name__ == "__main__":
    main()
