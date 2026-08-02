#!/usr/bin/env python3
"""One-off (2026-07-02): close the tournament's one open position, then reset the whole
tournament to a clean slate at heff's explicit request ("start fresh from 0" -- the
historical numbers reflect the pre-fix friction bug, not real strategy performance).

Steps: (1) force-close the open O spread via the same synthetic-IOC pattern process_exits
uses, verified filled; (2) back up options_eval.db to a timestamped file (quiet safety net,
not shown anywhere -- heff asked for a full wipe, this doesn't compromise that); (3) DELETE
all trades_ledger rows; (4) reset tournament_state to Beta(1,1)/0 trades/0 DSR/0 PSR for all
10 strategies; (5) clear data/options_sl_pending.json (referenced now-deleted trade ids).
"""
import json
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

import options_orchestrator as orch
from options_eval import connect as eval_connect, init_db as eval_init

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "data" / "options_eval.db"


def close_open_positions():
    conn = eval_connect(); eval_init(conn)
    rows = conn.execute("SELECT trade_id, legs_metadata, status FROM trades_ledger "
                        "WHERE status IN ('OPEN','PARTIAL_CLOSE')").fetchall()
    print(f"open/partial trades: {len(rows)}")
    for row in rows:
        tid = row["trade_id"]
        meta = json.loads(row["legs_metadata"])
        legs, qty = meta["legs"], int(meta["qty"])
        raw = orch.spread_close_cost(legs)
        if raw is None:
            print(f"  {tid}: legs unquoted, cannot price a close -- ABORTING, do not proceed blind")
            conn.close(); raise SystemExit(1)
        payload = orch.closing_payload(legs, qty, abs(raw))
        print(f"  {tid}: closing @ natural {abs(raw):.2f} -> {json.dumps(payload)}")
        resp = requests.post(orch.PAPER + "/v2/orders", headers=orch.H, json=payload, timeout=25)
        print(f"  submit HTTP {resp.status_code}: {resp.text[:200]}")
        if resp.status_code not in (200, 201):
            conn.close(); raise SystemExit(1)
        oid = resp.json().get("id")
        time.sleep(3)
        chk = requests.get(orch.PAPER + f"/v2/orders/{oid}", headers=orch.H, timeout=20).json()
        filled = int(float(chk.get("filled_qty", 0) or 0))
        print(f"  post-buffer status={chk.get('status')} filled={filled}/{qty}")
        if filled < qty:
            d = requests.delete(orch.PAPER + f"/v2/orders/{oid}", headers=orch.H, timeout=20)
            print(f"  DELETE remainder -> HTTP {d.status_code}")
            time.sleep(2)
            chk2 = requests.get(orch.PAPER + f"/v2/orders/{oid}", headers=orch.H, timeout=20).json()
            filled2 = int(float(chk2.get("filled_qty", 0) or 0))
            print(f"  after cancel: filled={filled2}/{qty}")
            if filled2 < qty:
                print(f"  {tid}: DID NOT FULLY CLOSE ({filled2}/{qty}) -- ABORTING reset, retry manually")
                conn.close(); raise SystemExit(1)
    conn.close()
    # broker-side confirmation: no open option positions remaining
    pos = requests.get(orch.PAPER + "/v2/positions", headers=orch.H, timeout=20).json()
    opt_positions = [p for p in pos if len(p.get("symbol", "")) > 6]
    print(f"broker option positions remaining: {len(opt_positions)}")
    if opt_positions:
        print("  NOT CLEAN -- aborting reset:", opt_positions)
        raise SystemExit(1)


def reset_db():
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    backup = DB_PATH.with_name(f"options_eval.db.bak_{ts}_prereset")
    shutil.copy2(DB_PATH, backup)
    print(f"backed up -> {backup}")

    conn = eval_connect(); eval_init(conn)
    conn.execute("BEGIN IMMEDIATE")
    n_deleted = conn.execute("DELETE FROM trades_ledger").rowcount
    conn.execute("UPDATE tournament_state SET status='PAPER', alpha_param=1.0, beta_param=1.0, "
                "trade_count=0, dsr_score=0.0, psr_score=0.0")
    conn.commit()
    strategies = conn.execute("SELECT strategy_id, status, alpha_param, beta_param, "
                              "trade_count, dsr_score, psr_score FROM tournament_state").fetchall()
    conn.close()
    print(f"deleted {n_deleted} ledger rows; tournament_state reset:")
    for r in strategies:
        print(" ", dict(r))

    pending = ROOT / "data" / "options_sl_pending.json"
    if pending.exists():
        pending.write_text("{}")
        print(f"cleared {pending}")


if __name__ == "__main__":
    print("=== step 1: close open positions ===")
    close_open_positions()
    print("\n=== step 2: reset ledger + tournament_state ===")
    reset_db()
    print("\nDONE")
