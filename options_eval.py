#!/usr/bin/env python3
"""
options_eval.py — Autonomous Options Evaluation Engine (OPTIONS_EVAL spec, 2026-06-25).

The STATE MANAGER + LEDGER for the daily forward options tournament. It OWNS the options trade
record (never hand-written) — the orchestrator calls record_open() after a paper fill; the cron
calls reconcile() to resolve closes; the EOD batch runs the Deflated-Sharpe lifecycle.

Cron-safe by design (1.9GB box, 2-min cron, no daemon/Redis/Postgres):
  * SQLite in WAL mode, synchronous=NORMAL, busy_timeout=15s -> overlapping crons queue, never deadlock.
  * BEGIN IMMEDIATE for every state mutation -> write lock acquired upfront.
Math reused from options_lib.py (probabilistic_sharpe, expected_max_sharpe) — canonical López de
Prado PSR ( (γ4−1)/4 denominator ), NOT the spec PDF's (γ4−3)/4 transcription.

RECONCILIATION (heff's rules win): StrategyStatus.LIVE is ADVISORY — "promoted, surface for
confirm-every-trade," NOT auto real-money. Paper tournament is autonomous; real money stays gated.

Usage:
  ./venv/bin/python options_eval.py --init                 # create the DB/schema
  ./venv/bin/python options_eval.py --reconcile            # cron: resolve fills/closes (live Alpaca)
  ./venv/bin/python options_eval.py --dsr-batch            # EOD: DSR lifecycle (promote/paper/kill)
  ./venv/bin/python options_eval.py --leaderboard          # rank strategies
  ./venv/bin/python options_eval.py --selftest             # temp-DB end-to-end, $0, no broker
"""
import argparse
import json
import re
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import date
from enum import Enum
from pathlib import Path

import numpy as np
from scipy.stats import norm

import options_lib as ol

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "data" / "options_eval.db"
PENDING_ASSIGNMENT_PATH = ROOT / "data" / "options_assignment_pending.json"
PENDING_ASSIGNMENT_TTL_S = 900  # 15min self-expiring guard -- see _mark_assignment_pending


# ----------------------------------------------------------------- enums / config
class TradeStatus(str, Enum):
    PENDING = "PENDING"; OPEN = "OPEN"; PARTIAL_CLOSE = "PARTIAL_CLOSE"; CLOSED = "CLOSED"


class StrategyStatus(str, Enum):
    PAPER = "PAPER"; LIVE = "LIVE"; KILLED = "KILLED"     # LIVE = advisory (confirm-every-trade)


class RegimeTag(str, Enum):
    POS_GEX_LOW_VIX = "POS_GEX_LOW_VIX"; POS_GEX_HIGH_VIX = "POS_GEX_HIGH_VIX"
    NEG_GEX_LOW_VIX = "NEG_GEX_LOW_VIX"; NEG_GEX_HIGH_VIX = "NEG_GEX_HIGH_VIX"
    VOL_EXPANSION_SHOCK = "VOL_EXPANSION_SHOCK"; UNKNOWN = "UNKNOWN"


@dataclass
class TradeResolutionConfig:
    min_r_multiple: float = 0.0        # any strictly profitable resolved trade is a success
    decay_gamma: float = 0.98          # Discounted Thompson Sampling forgetting factor
    dsr_kill_threshold: float = 0.50
    dsr_promote_threshold: float = 0.95
    mintrl_confidence: float = 0.95


CFG = TradeResolutionConfig()

DDL = """
CREATE TABLE IF NOT EXISTS tournament_state (
    strategy_id   TEXT PRIMARY KEY,
    status        TEXT NOT NULL CHECK(status IN ('PAPER','LIVE','KILLED')),
    alpha_param   REAL DEFAULT 1.0,
    beta_param    REAL DEFAULT 1.0,
    trade_count   INTEGER DEFAULT 0,
    dsr_score     REAL DEFAULT 0.0,
    psr_score     REAL DEFAULT 0.0,
    last_eval_time INTEGER NULL
);
CREATE TABLE IF NOT EXISTS order_lifecycle_events (
    event_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id       TEXT NOT NULL,
    event_time     INTEGER NOT NULL,
    old_status     TEXT,
    broker_status  TEXT,
    reason         TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS trades_ledger (
    trade_id      TEXT PRIMARY KEY,
    strategy_id   TEXT NOT NULL,
    entry_time    INTEGER NOT NULL,
    exit_time     INTEGER NULL,
    legs_metadata TEXT NOT NULL,
    regime_tag    TEXT NOT NULL,
    initial_risk  REAL NOT NULL CHECK(initial_risk > 0),
    realized_pnl  REAL NULL,
    r_multiple    REAL NULL,
    status        TEXT NOT NULL CHECK(status IN ('PENDING','OPEN','PARTIAL_CLOSE','CLOSED')),
    FOREIGN KEY(strategy_id) REFERENCES tournament_state(strategy_id)
);
"""


# ----------------------------------------------------------------- connection
def connect(path=DB_PATH):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=20, isolation_level=None)  # autocommit; we BEGIN explicitly
    conn.row_factory = sqlite3.Row
    for pragma in ("journal_mode=WAL", "synchronous=NORMAL", "busy_timeout=15000",
                   "temp_store=MEMORY", "foreign_keys=ON", "mmap_size=134217728"):
        conn.execute(f"PRAGMA {pragma};")
    return conn


def init_db(conn):
    conn.executescript(DDL)


def ensure_strategy(conn, strategy_id):
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("INSERT OR IGNORE INTO tournament_state(strategy_id, status) VALUES(?, 'PAPER')",
                 (strategy_id,))
    conn.execute("COMMIT")


# ----------------------------------------------------------------- ledger writes
def record_open(conn, trade_id, strategy_id, legs, regime_tag, initial_risk,
                entry_time=None, status=TradeStatus.OPEN):
    """Called by the orchestrator after a paper fill. The ledger OWNS the trade record."""
    ensure_strategy(conn, strategy_id)
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        "INSERT OR REPLACE INTO trades_ledger("
        "trade_id, strategy_id, entry_time, legs_metadata, regime_tag, initial_risk, status) "
        "VALUES(?,?,?,?,?,?,?)",
        (trade_id, strategy_id, int(entry_time or time.time()), json.dumps(legs),
         str(getattr(regime_tag, "value", regime_tag)), float(initial_risk), str(status.value)))
    conn.execute("COMMIT")


def update_thompson_sampling(r_multiple, alpha, beta, cfg=CFG):
    """Discounted Thompson Sampling: decay then Bernoulli(R>R_min)."""
    alpha_t = alpha * cfg.decay_gamma
    beta_t = beta * cfg.decay_gamma
    success = 1.0 if r_multiple > cfg.min_r_multiple else 0.0
    return alpha_t + success, beta_t + (1.0 - success)


def resolve_trade(conn, trade_id, realized_pnl, exit_time=None, cfg=CFG):
    """Mark a trade CLOSED, compute R-multiple, apply the discounted Thompson update — atomically."""
    conn.execute("BEGIN IMMEDIATE")
    row = conn.execute("SELECT strategy_id, initial_risk, status FROM trades_ledger WHERE trade_id=?",
                       (trade_id,)).fetchone()
    if row is None or row["status"] == TradeStatus.CLOSED.value:
        conn.execute("COMMIT"); return None
    r_mult = realized_pnl / row["initial_risk"]
    conn.execute("UPDATE trades_ledger SET status='CLOSED', exit_time=?, realized_pnl=?, r_multiple=? "
                 "WHERE trade_id=?", (int(exit_time or time.time()), float(realized_pnl), float(r_mult), trade_id))
    st = conn.execute("SELECT alpha_param, beta_param, trade_count FROM tournament_state WHERE strategy_id=?",
                      (row["strategy_id"],)).fetchone()
    na, nb = update_thompson_sampling(r_mult, st["alpha_param"], st["beta_param"], cfg)
    conn.execute("UPDATE tournament_state SET alpha_param=?, beta_param=?, trade_count=trade_count+1 "
                 "WHERE strategy_id=?", (na, nb, row["strategy_id"]))
    conn.execute("COMMIT")
    return r_mult


# ----------------------------------------------------------------- DSR batch loop
def min_trl(sr, skew, kurt, sr_star, conf=0.95):
    """Minimum Track Record Length — burn-in shield. kurt is non-excess (normal=3)."""
    denom = sr - sr_star
    if abs(denom) < 1e-6:
        return 1e9
    za = norm.ppf(conf)
    return 1.0 + (1 - skew * sr + (kurt - 1) / 4.0 * sr * sr) * (za / denom) ** 2


def _moments(r):
    r = np.asarray(r, float)
    sd = r.std(ddof=0)
    if sd == 0 or len(r) < 2:
        return 0.0, 0.0, 3.0
    sr = r.mean() / r.std(ddof=1)
    m = r - r.mean()
    return sr, float(np.mean(m ** 3) / sd ** 3), float(np.mean(m ** 4) / sd ** 4)


def run_dsr_batch(conn, cfg=CFG):
    """EOD lifecycle: per-strategy PSR/DSR vs the multiple-testing-deflated benchmark SR_0,
    shielded by MinTRL; promote (advisory LIVE) / keep PAPER / KILL. Returns a summary list."""
    stron = conn.execute("SELECT strategy_id, status, trade_count FROM tournament_state "
                          "WHERE status != 'KILLED'").fetchall()
    series = {}
    for s in stron:
        rs = [row["r_multiple"] for row in conn.execute(
            "SELECT r_multiple FROM trades_ledger WHERE strategy_id=? AND status='CLOSED' "
            "AND r_multiple IS NOT NULL ORDER BY exit_time", (s["strategy_id"],)).fetchall()]
        series[s["strategy_id"]] = rs
    srs = [(_moments(rs)[0]) for rs in series.values() if len(rs) >= 2]
    sr0 = ol.expected_max_sharpe(srs) if len(srs) >= 2 else 0.0   # False-Strategy-Theorem benchmark
    out = []
    now = int(time.time())
    for s in stron:
        sid = s["strategy_id"]; rs = series[sid]; T = len(rs)
        sr, skew, kurt = _moments(rs)
        psr = ol.probabilistic_sharpe(rs, sr_star=0.0) if T >= 2 else 0.0
        dsr = ol.probabilistic_sharpe(rs, sr_star=sr0) if T >= 2 else 0.0
        mtrl = min_trl(sr, skew, kurt, sr0, cfg.mintrl_confidence) if T >= 2 else 1e9
        new_status = s["status"]
        if T >= 2 and T >= mtrl:                       # past burn-in -> lifecycle active
            if dsr < cfg.dsr_kill_threshold:
                new_status = StrategyStatus.KILLED.value
            elif dsr >= cfg.dsr_promote_threshold and s["status"] == StrategyStatus.PAPER.value:
                new_status = StrategyStatus.LIVE.value
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("UPDATE tournament_state SET status=?, dsr_score=?, psr_score=?, last_eval_time=? "
                     "WHERE strategy_id=?", (new_status, dsr, psr, now, sid))
        conn.execute("COMMIT")
        out.append({"strategy_id": sid, "T": T, "SR": round(sr, 3), "PSR": round(psr, 3),
                    "DSR": round(dsr, 3), "MinTRL": round(mtrl, 1), "status": new_status,
                    "shielded": T < mtrl})
    return out, sr0


def rebuild_thompson_state(conn, cfg=CFG):
    """Rebuild posterior state solely from CLOSED trades after correcting the success rule."""
    states = {r["strategy_id"]: [1.0, 1.0, 0] for r in
              conn.execute("SELECT strategy_id FROM tournament_state").fetchall()}
    rows = conn.execute("SELECT strategy_id, r_multiple FROM trades_ledger "
                        "WHERE status='CLOSED' AND r_multiple IS NOT NULL "
                        "ORDER BY COALESCE(exit_time, entry_time), trade_id").fetchall()
    for row in rows:
        states.setdefault(row["strategy_id"], [1.0, 1.0, 0])
        alpha, beta, count = states[row["strategy_id"]]
        alpha, beta = update_thompson_sampling(float(row["r_multiple"]), alpha, beta, cfg)
        states[row["strategy_id"]] = [alpha, beta, count + 1]
    conn.execute("BEGIN IMMEDIATE")
    for sid, (alpha, beta, count) in states.items():
        conn.execute("UPDATE tournament_state SET alpha_param=?, beta_param=?, trade_count=? "
                     "WHERE strategy_id=?", (alpha, beta, count, sid))
    conn.execute("COMMIT")
    return {"closed_replayed": len(rows), "strategies": len(states)}


def leaderboard(conn):
    rows = conn.execute("SELECT strategy_id, status, alpha_param, beta_param, trade_count, "
                        "dsr_score, psr_score FROM tournament_state ORDER BY "
                        "alpha_param/(alpha_param+beta_param) DESC").fetchall()
    out = []
    for r in rows:
        wr = r["alpha_param"] / (r["alpha_param"] + r["beta_param"])
        out.append({"strategy_id": r["strategy_id"], "status": r["status"],
                    "post_mean": round(wr, 3), "alpha": round(r["alpha_param"], 2),
                    "beta": round(r["beta_param"], 2), "trades": r["trade_count"],
                    "DSR": round(r["dsr_score"], 3)})
    return out


# ----------------------------------------------------------------- live reconcile (cron)
TERMINAL_UNFILLED = {"canceled", "expired", "rejected", "replaced", "suspended"}


def retire_pending(conn, trade_id, broker_status, reason):
    """Remove a never-filled order from capacity without teaching Thompson from a non-trade."""
    conn.execute("BEGIN IMMEDIATE")
    row = conn.execute("SELECT status FROM trades_ledger WHERE trade_id=?", (trade_id,)).fetchone()
    if row and row["status"] == "PENDING":
        conn.execute("INSERT INTO order_lifecycle_events"
                     "(trade_id,event_time,old_status,broker_status,reason) VALUES(?,?,?,?,?)",
                     (trade_id, int(time.time()), "PENDING", broker_status, reason))
        conn.execute("DELETE FROM trades_ledger WHERE trade_id=?", (trade_id,))
    conn.execute("COMMIT")


def reconcile(conn, arm=False, pending_ttl_s=1800):
    """Adopt fills and retire terminal/stale never-filled entries so capacity reflects reality."""
    import requests
    KEY = (ROOT / "credentials" / "alpaca_key.txt").read_text().strip()
    SEC = (ROOT / "credentials" / "alpaca_secret.txt").read_text().strip()
    h = {"APCA-API-KEY-ID": KEY, "APCA-API-SECRET-KEY": SEC}
    base = "https://paper-api.alpaca.markets"
    openrows = conn.execute("SELECT trade_id, status, entry_time FROM trades_ledger "
                            "WHERE status IN ('PENDING','OPEN','PARTIAL_CLOSE')").fetchall()
    if not openrows:
        return {"checked": 0, "opened": 0, "retired": 0}
    try:
        resp = requests.get(base + "/v2/orders", headers=h,
                            params={"status": "all", "nested": "true", "limit": 500}, timeout=25)
        resp.raise_for_status()
        orders = resp.json()
    except Exception as e:
        return {"error": str(e)[:120]}
    by_id = {o.get("id"): o for o in orders} if isinstance(orders, list) else {}
    opened = retired = 0
    for row in openrows:
        if row["status"] != "PENDING":
            continue
        o = by_id.get(row["trade_id"])
        missing_confirmed = False
        if not o:
            try:
                rr = requests.get(base + f"/v2/orders/{row['trade_id']}", headers=h, timeout=20)
                o = rr.json() if rr.status_code == 200 else None
                missing_confirmed = rr.status_code == 404
            except Exception:
                o = None
        status = (o or {}).get("status")
        filled_qty = float((o or {}).get("filled_qty") or 0)
        if status == "filled" or filled_qty > 0:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("UPDATE trades_ledger SET status='OPEN' WHERE trade_id=?", (row["trade_id"],))
            conn.execute("COMMIT")
            opened += 1
        elif status in TERMINAL_UNFILLED:
            retire_pending(conn, row["trade_id"], status, "broker terminal without fill")
            retired += 1
        elif int(time.time()) - int(row["entry_time"]) > pending_ttl_s and arm:
            if missing_confirmed:
                retire_pending(conn, row["trade_id"], "missing", "stale pending absent at broker")
                retired += 1
            elif o:
                cr = requests.delete(base + f"/v2/orders/{row['trade_id']}", headers=h, timeout=20)
                if cr.status_code in (204, 404):
                    retire_pending(conn, row["trade_id"], status or "missing", "stale pending canceled")
                    retired += 1
    return {"checked": len(openrows), "opened": opened, "retired": retired}


def apply_close(conn, trade_id, realized_pnl, filled_qty, total_qty, exec_price, cfg=CFG):
    """Close (full or partial) for the synthetic-IOC exit / EOD reconcile. Accumulates PnL across
    partials and decrements the surviving qty in legs_metadata. On FULL close: status=CLOSED,
    R-multiple = aggregate_pnl / (original) initial_risk, discounted Thompson update. Atomic."""
    conn.execute("BEGIN IMMEDIATE")
    row = conn.execute("SELECT strategy_id, initial_risk, realized_pnl, legs_metadata, status "
                       "FROM trades_ledger WHERE trade_id=?", (trade_id,)).fetchone()
    if row is None or row["status"] == TradeStatus.CLOSED.value:
        conn.execute("COMMIT"); return None
    total_pnl = (row["realized_pnl"] or 0.0) + realized_pnl
    meta = json.loads(row["legs_metadata"]) if row["legs_metadata"] else {}
    open_qty = int(meta.get("qty", total_qty))
    remaining = max(open_qty - filled_qty, 0)
    if remaining > 0:                                  # partial -> stay active for the next tick
        meta["qty"] = remaining
        conn.execute("UPDATE trades_ledger SET status='PARTIAL_CLOSE', realized_pnl=?, legs_metadata=? "
                     "WHERE trade_id=?", (total_pnl, json.dumps(meta), trade_id))
        conn.execute("COMMIT"); return None
    r_mult = total_pnl / row["initial_risk"]           # full close: R vs ORIGINAL defined risk
    conn.execute("UPDATE trades_ledger SET status='CLOSED', exit_time=?, realized_pnl=?, r_multiple=? "
                 "WHERE trade_id=?", (int(time.time()), total_pnl, r_mult, trade_id))
    st = conn.execute("SELECT alpha_param, beta_param FROM tournament_state WHERE strategy_id=?",
                      (row["strategy_id"],)).fetchone()
    na, nb = update_thompson_sampling(r_mult, st["alpha_param"], st["beta_param"], cfg)
    conn.execute("UPDATE tournament_state SET alpha_param=?, beta_param=?, trade_count=trade_count+1 "
                 "WHERE strategy_id=?", (na, nb, row["strategy_id"]))
    conn.execute("COMMIT")
    return r_mult


def _occ_expiry(occ):
    """Parse expiration date from an OCC symbol (e.g. F260731P00014000 -> 2026-07-31)."""
    if not occ:
        return None
    m = re.search(r"(\d{6})[CP]\d{8}$", occ)
    if not m:
        return None
    s = m.group(1)
    return f"20{s[:2]}-{s[2:4]}-{s[4:6]}"


def _occ_root(occ):
    """Underlying root from an OCC symbol (F260731P00014000 -> F)."""
    m = re.match(r"^([A-Z]+)\d{6}[CP]\d{8}$", occ or "")
    return m.group(1) if m else None


def _alpaca_headers():
    KEY = (ROOT / "credentials" / "alpaca_key.txt").read_text().strip()
    SEC = (ROOT / "credentials" / "alpaca_secret.txt").read_text().strip()
    return {"APCA-API-KEY-ID": KEY, "APCA-API-SECRET-KEY": SEC}


def _live_positions(symbol):
    """GET /v2/positions/{symbol} -> dict (qty, side, ...) or None (404 = no position)."""
    import requests
    r = requests.get(f"https://paper-api.alpaca.markets/v2/positions/{symbol}",
                     headers=_alpaca_headers(), timeout=20)
    return r.json() if r.status_code == 200 else None


def _live_submit(order):
    """POST /v2/orders -> dict."""
    import requests
    return requests.post("https://paper-api.alpaca.markets/v2/orders",
                         headers=_alpaca_headers(), json=order, timeout=25).json()


def _load_pending_assignments():
    if not PENDING_ASSIGNMENT_PATH.exists():
        return {}
    try:
        return json.loads(PENDING_ASSIGNMENT_PATH.read_text())
    except Exception:
        return {}


def _mark_assignment_pending(symbol):
    """Flags `symbol` as under options-assignment recovery so alpaca_executor.py's equity-book
    position filter can exclude it -- otherwise a freshly-assigned stock position (<=6-char
    symbol, same shape as any equity-book ticker) could get silently miscounted into the
    mean-reversion/ORB book's slot count or even selected by its "cut worst position" rotation
    logic before OUR liquidation completes. Self-expiring (PENDING_ASSIGNMENT_TTL_S) rather than
    requiring guaranteed cleanup, so a crash mid-liquidation can't permanently blacklist a symbol
    from the equity book."""
    if not symbol:
        return
    try:
        data = _load_pending_assignments()
        data[symbol] = time.time()
        PENDING_ASSIGNMENT_PATH.parent.mkdir(parents=True, exist_ok=True)
        PENDING_ASSIGNMENT_PATH.write_text(json.dumps(data))
    except Exception as e:
        print(f"  ! _mark_assignment_pending({symbol}) failed: {e}")


def _clear_assignment_pending(symbol):
    if not symbol:
        return
    try:
        data = _load_pending_assignments()
        if symbol in data:
            del data[symbol]
            PENDING_ASSIGNMENT_PATH.write_text(json.dumps(data))
    except Exception as e:
        print(f"  ! _clear_assignment_pending({symbol}) failed: {e}")


def opasn_liquidate(conn, trade_id, meta, arm=False, get_positions=None, submit=None, cfg=CFG):
    """OPASN auto-liquidation recovery (spec §2.2). Dry-run by default.
    1) market-liquidate the assigned equity (sell if long shares from a put assignment, buy-to-cover
       if short from a call assignment); 2) market sell-to-close the protective long option leg;
    3) resolve the trade as a FAILURE (R<0 -> Bernoulli 0 -> β+1) to penalize the bandit arm.
    `get_positions`/`submit` are injectable for dry-run/testing. Returns the action plan."""
    get_positions = get_positions or _live_positions
    submit = submit or _live_submit
    legs = meta.get("legs", [])
    short_leg = next((l for l in legs if l.get("side") == "SELL"), None)
    long_leg = next((l for l in legs if l.get("side") == "BUY"), None)
    underlying = _occ_root(short_leg["occ"]) if short_leg else None
    qty = int(meta.get("qty", 1))
    plan = {"trade_id": trade_id, "underlying": underlying, "arm": arm, "orders": [], "fills": []}
    _mark_assignment_pending(underlying)  # mark before touching the account -- the stock
                                          # position already exists the moment OPASN posts

    pos = get_positions(underlying) if underlying else None
    if pos:
        shares = abs(int(float(pos.get("qty", 0))))
        held_long = str(pos.get("side", "")).lower() == "long"
        if shares > 0:
            plan["orders"].append({"symbol": underlying, "qty": str(shares),
                                   "side": "sell" if held_long else "buy",   # neutralize the assigned equity
                                   "type": "market", "time_in_force": "day",
                                   "_intent": "liquidate_assigned_equity"})
    if long_leg:
        plan["orders"].append({"symbol": long_leg["occ"], "qty": str(qty), "side": "sell",
                               "type": "market", "time_in_force": "day",
                               "_intent": "sell_to_close_protective_long"})

    print(f"🚨 OPASN recovery {trade_id} underlying={underlying} arm={arm}: "
          f"{json.dumps([{k: v for k, v in o.items() if k != '_intent'} for o in plan['orders']])}")
    risk = float(conn.execute("SELECT initial_risk FROM trades_ledger WHERE trade_id=?",
                              (trade_id,)).fetchone()["initial_risk"])
    realized = -risk                                   # assignment = max-loss event (refined by fills if armed)
    if arm:
        for o in plan["orders"]:
            payload = {k: v for k, v in o.items() if not k.startswith("_")}
            plan["fills"].append(submit(payload))
        # NOTE: refine `realized` from actual fills (equity slippage + assignment fee) once filled.
        _clear_assignment_pending(underlying)  # liquidation orders submitted -- release the guard.
                                               # Not cleared on dry-run: the assigned stock position
                                               # genuinely still exists in the account either way, and
                                               # the TTL self-heals if this never gets armed.
    apply_close(conn, trade_id, realized, qty, qty, exec_price=0.0, cfg=cfg)  # resolve as FAILURE (β+1)
    plan["realized_pnl"] = realized
    return plan


# ----------------------------------------------------------------- DIRECTIVE 3 — EXPIRE-TO-ZERO
def reconcile_expirations(conn, today=None, cfg=CFG, arm=False):
    """Resolve only confirmed, fully expired, broker-flat structures; never infer through live legs."""
    import requests
    today = today or date.today().isoformat()
    rows = conn.execute("SELECT trade_id, legs_metadata, initial_risk FROM trades_ledger "
                        "WHERE status IN ('OPEN','PARTIAL_CLOSE')").fetchall()
    stale = []
    for r in rows:
        meta = json.loads(r["legs_metadata"]) if r["legs_metadata"] else {}
        exps = [_occ_expiry(l.get("occ")) for l in meta.get("legs", [])]
        exps = [e for e in exps if e]
        if exps and min(exps) <= today:
            stale.append((r["trade_id"], meta, exps))
    if not stale:
        return {"stale": 0, "resolved": 0, "assigned": 0, "unresolved": 0}
    try:
        h = _alpaca_headers()
        ar = requests.get("https://paper-api.alpaca.markets/v2/account/activities", headers=h,
                          params={"activity_types": "OPEXP,OPASN,OPXRC"}, timeout=25)
        ar.raise_for_status()
        acts = ar.json()
        pr = requests.get("https://paper-api.alpaca.markets/v2/positions", headers=h, timeout=25)
        pr.raise_for_status()
        positions = pr.json()
    except Exception as e:
        return {"error": str(e)[:140], "stale": len(stale)}
    by_sym = {}
    for a in (acts if isinstance(acts, list) else []):
        by_sym.setdefault(a.get("symbol"), []).append(a)
    live_occ = {p.get("symbol") for p in (positions if isinstance(positions, list) else [])
                if p.get("symbol")}
    resolved = assigned = unresolved = 0
    for tid, meta, exps in stale:
        occs = {l.get("occ") for l in meta.get("legs", []) if l.get("occ")}
        types = {a.get("activity_type") for s in occs for a in by_sym.get(s, [])}
        credit, qty = float(meta.get("entry_credit", 0)), int(meta.get("qty", 1))
        if "OPASN" in types:
            opasn_liquidate(conn, tid, meta, arm=arm, cfg=cfg)
            assigned += 1
            continue
        # Calendars/diagonals remain open at front expiry, and any live OCC leg is conclusive
        # broker evidence that the structure is not fully closed.
        if max(exps) > today or occs & live_occ:
            unresolved += 1
            continue
        # Absence of activity is not evidence of max profit/loss. Require explicit expiry.
        if "OPEXP" not in types:
            unresolved += 1
            continue
        struct = meta.get("structure", "credit")
        sign = 1.0 if struct == "credit" else -1.0
        apply_close(conn, tid, sign * credit * 100 * qty, qty, qty, exec_price=0.0, cfg=cfg)
        resolved += 1
    return {"stale": len(stale), "resolved": resolved, "assigned": assigned,
            "unresolved": unresolved}


def check_assignments(conn, cfg=CFG, arm=False):
    """Intraday OPASN (early-assignment) check across EVERY open/partial trade, not just ones
    near expiry. reconcile_expirations()'s `min(exps) <= today` filter structurally can never
    catch EARLY assignment -- a short leg assigned weeks before its own expiry never enters that
    check at all, no matter how often it's run, and that function only runs once/day (18:30 ET).
    This is meant to run every tournament tick (every ~2min during RTH) instead, so a freshly-
    assigned stock position gets caught and handed to opasn_liquidate() same-tick rather than
    sitting unmanaged (and miscounted by the equity book's <=6-char symbol filter, see
    alpaca_executor.py) for up to a day. Reuses opasn_liquidate() unchanged; a trade it resolves
    drops out of the OPEN/PARTIAL_CLOSE query on its own, so repeated calls are naturally
    idempotent -- no separate dedup bookkeeping needed."""
    rows = conn.execute("SELECT trade_id, legs_metadata FROM trades_ledger "
                        "WHERE status IN ('OPEN','PARTIAL_CLOSE')").fetchall()
    if not rows:
        return {"checked": 0, "assigned": 0}
    try:
        import requests
        KEY = (ROOT / "credentials" / "alpaca_key.txt").read_text().strip()
        SEC = (ROOT / "credentials" / "alpaca_secret.txt").read_text().strip()
        h = {"APCA-API-KEY-ID": KEY, "APCA-API-SECRET-KEY": SEC}
        today = date.today().isoformat()
        acts = requests.get("https://paper-api.alpaca.markets/v2/account/activities", headers=h,
                            params={"activity_types": "OPASN", "after": f"{today}T00:00:00Z"},
                            timeout=25).json()
    except Exception as e:
        return {"error": str(e)[:140], "checked": len(rows)}
    assigned_syms = {a.get("symbol") for a in (acts if isinstance(acts, list) else [])}
    if not assigned_syms:
        return {"checked": len(rows), "assigned": 0}
    assigned = 0
    for r in rows:
        meta = json.loads(r["legs_metadata"]) if r["legs_metadata"] else {}
        occs = {l.get("occ") for l in meta.get("legs", [])}
        if occs & assigned_syms:
            opasn_liquidate(conn, r["trade_id"], meta, arm=arm, cfg=cfg)
            assigned += 1
    return {"checked": len(rows), "assigned": assigned}


# ----------------------------------------------------------------- DIRECTIVE 4 — NIGHT LEADERBOARD
def generate_leaderboard(conn, regime=None):
    """Telegram-ready Markdown leaderboard (Rank · ID · Status · T · Win% · PSR · DSR) + alerts."""
    rows = conn.execute("SELECT * FROM tournament_state").fetchall()
    ranked = sorted(rows, key=lambda r: (r["status"] != "LIVE", -r["dsr_score"]))
    emoji = {"LIVE": "🟢", "PAPER": "🟡", "KILLED": "🔴"}
    out = ["📊 *Daily Forward Tournament — EOD Leaderboard*",
           f"Regime at EOD: *{regime or 'UNKNOWN'}*",
           "Capital Allocation: Discounted Thompson Sampling (γ=0.98)", ""]
    alerts = []
    for i, r in enumerate(ranked, 1):
        a, b = r["alpha_param"], r["beta_param"]
        wr = 100 * a / (a + b) if (a + b) else 0.0
        out.append(f"{i}. *{r['strategy_id']}*  {emoji.get(r['status'], '')} {r['status']}")
        out.append(f"   T={r['trade_count']} · Win {wr:.1f}% (α{a:.1f}/β{b:.1f}) · "
                   f"PSR {r['psr_score']:.3f} · DSR {r['dsr_score']:.3f}")
        if r["status"] == "KILLED":
            alerts.append(f"🔴 {r['strategy_id']} KILLED (DSR {r['dsr_score']:.3f} < 0.50).")
    if not ranked:
        out.append("_(no strategies registered yet)_")
    if alerts:
        out += ["", "⚠️ *System Alerts:*"] + [f"• {x}" for x in alerts]
    return "\n".join(out)


# ----------------------------------------------------------------- selftest
def selftest():
    import tempfile, os
    ok = True

    def check(name, good):
        nonlocal ok
        ok &= bool(good)
        print(f"  [{'OK' if good else 'FAIL'}] {name}")

    tmp = tempfile.mkdtemp()
    conn = connect(Path(tmp) / "t.db")
    init_db(conn)
    check("WAL pragma active", conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal")

    rng = np.random.default_rng(7)
    # winner: clearly +edge; loser: clearly -edge; newbie: only 3 trades (burn-in shield)
    for sid, mean in [("winner", 0.45), ("loser", -0.35)]:
        for i in range(35):
            tid = f"{sid}_{i}"
            record_open(conn, tid, sid, [{"k": 58, "t": "P", "s": "SELL"}],
                        RegimeTag.POS_GEX_LOW_VIX, initial_risk=80.0)
            pnl = float(rng.normal(mean, 0.5)) * 80.0     # R ~ N(mean,0.5) -> pnl = R*risk
            resolve_trade(conn, tid, pnl)
    for i in range(3):
        tid = f"newbie_{i}"
        record_open(conn, tid, "newbie", [{"k": 58}], RegimeTag.UNKNOWN, 80.0)
        resolve_trade(conn, tid, -40.0)                   # losing, but too few trades to kill

    st = {r["strategy_id"]: r for r in conn.execute("SELECT * FROM tournament_state").fetchall()}
    check("winner posterior mean > loser", st["winner"]["alpha_param"] / (st["winner"]["alpha_param"] + st["winner"]["beta_param"])
          > st["loser"]["alpha_param"] / (st["loser"]["alpha_param"] + st["loser"]["beta_param"]))
    check("trade_count tracked (winner=35)", st["winner"]["trade_count"] == 35)
    # discounted: with γ<1, totals are bounded — alpha+beta < raw trade count
    check("Thompson decay bounds mass", (st["winner"]["alpha_param"] + st["winner"]["beta_param"]) < 35)

    res, sr0 = run_dsr_batch(conn)
    by = {r["strategy_id"]: r for r in res}
    check(f"DSR benchmark SR_0 > 0 ({sr0:.3f})", sr0 > 0)
    check(f"winner DSR > loser DSR ({by['winner']['DSR']} vs {by['loser']['DSR']})",
          by["winner"]["DSR"] > by["loser"]["DSR"])
    check("loser KILLED (DSR<0.5, past MinTRL)", by["loser"]["status"] == "KILLED")
    check("winner NOT killed", by["winner"]["status"] != "KILLED")
    check("newbie shielded by MinTRL (still PAPER)", by["newbie"]["status"] == "PAPER" and by["newbie"]["shielded"])

    lb = leaderboard(conn)
    check("leaderboard ranks winner above loser",
          [x["strategy_id"] for x in lb].index("winner") < [x["strategy_id"] for x in lb].index("loser"))

    # apply_close: partial then full (synthetic-IOC exit path)
    meta = {"legs": [{"occ": "F260731P00014000", "side": "SELL"},
                     {"occ": "F260731P00013000", "side": "BUY"}], "entry_credit": 0.30, "qty": 2}
    record_open(conn, "pf1", "winner", meta, RegimeTag.POS_GEX_LOW_VIX, initial_risk=140.0)
    r1 = apply_close(conn, "pf1", realized_pnl=15.0, filled_qty=1, total_qty=2, exec_price=0.15)
    row = conn.execute("SELECT status, realized_pnl FROM trades_ledger WHERE trade_id='pf1'").fetchone()
    check("partial fill -> PARTIAL_CLOSE", row["status"] == "PARTIAL_CLOSE" and r1 is None)
    r2 = apply_close(conn, "pf1", realized_pnl=15.0, filled_qty=1, total_qty=1, exec_price=0.15)
    row = conn.execute("SELECT status, realized_pnl, r_multiple FROM trades_ledger WHERE trade_id='pf1'").fetchone()
    check("final fill -> CLOSED + aggregate pnl", row["status"] == "CLOSED" and abs(row["realized_pnl"] - 30.0) < 1e-6)

    check("OCC expiry parse", _occ_expiry("F260731P00014000") == "2026-07-31")
    check("OCC root parse", _occ_root("F260731P00014000") == "F")
    check("reconcile_expirations: none stale (future exp)", reconcile_expirations(conn, today="2026-06-01")["stale"] == 0)

    # OPASN auto-liquidation dry-run (injected position; no live orders)
    amon = {"legs": [{"occ": "F260620P00014000", "side": "SELL"},
                     {"occ": "F260620P00013000", "side": "BUY"}], "entry_credit": 0.30, "qty": 1}
    record_open(conn, "asn1", "winner", amon, RegimeTag.NEG_GEX_HIGH_VIX, initial_risk=70.0)
    b_before = conn.execute("SELECT beta_param FROM tournament_state WHERE strategy_id='winner'").fetchone()["beta_param"]
    plan = opasn_liquidate(conn, "asn1", amon, arm=False,
                           get_positions=lambda s: {"qty": "100", "side": "long"})
    b_after = conn.execute("SELECT beta_param FROM tournament_state WHERE strategy_id='winner'").fetchone()["beta_param"]
    row = conn.execute("SELECT status, r_multiple FROM trades_ledger WHERE trade_id='asn1'").fetchone()
    check("OPASN plan: equity liquidation + long-leg close", len(plan["orders"]) == 2
          and plan["orders"][0]["symbol"] == "F" and plan["orders"][0]["side"] == "sell")
    check("OPASN dry-run places no live orders", plan["fills"] == [])
    check("OPASN resolves CLOSED as loss (R<0)", row["status"] == "CLOSED" and row["r_multiple"] < 0)
    check("OPASN penalizes the arm (β increment)", b_after > b_before)
    check("OPASN marks the pending-assignment guard", "F" in _load_pending_assignments())
    _clear_assignment_pending("F")  # dry-run above never auto-clears (by design, see
                                    # opasn_liquidate) -- but this is a SYNTHETIC test symbol on
                                    # the SAME real file production reads, so scrub it here or a
                                    # selftest run could spuriously block "F" for the equity book
                                    # (F is in both the options-tournament AND S&P-100 universes)
    check("selftest cleaned up its pending-assignment mark", "F" not in _load_pending_assignments())

    md = generate_leaderboard(conn, regime="NEG_GEX_HIGH_VIX")
    check("leaderboard markdown well-formed", "Daily Forward Tournament" in md and "DSR" in md
          and "NEG_GEX_HIGH_VIX" in md and "KILLED" in md)
    conn.close()
    print("SELFTEST", "PASS" if ok else "FAIL")
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--init", action="store_true")
    ap.add_argument("--reconcile", action="store_true")
    ap.add_argument("--reconcile-exp", action="store_true", help="EOD expire-to-zero reconciliation")
    ap.add_argument("--check-assignments", action="store_true",
                    help="intraday OPASN check across all open trades, not just near-expiry ones")
    ap.add_argument("--dsr-batch", action="store_true")
    ap.add_argument("--rebuild-thompson", action="store_true",
                    help="replay CLOSED trades into corrected Thompson state")
    ap.add_argument("--leaderboard", action="store_true")
    ap.add_argument("--regime", default=None, help="regime label for the leaderboard header")
    ap.add_argument("--arm", action="store_true", help="actually submit liquidation orders (reconcile-exp)")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        sys.exit(0 if selftest() else 1)
    conn = connect()
    init_db(conn)
    if a.init:
        print(f"initialized {DB_PATH}")
    if a.reconcile:
        print("reconcile:", reconcile(conn, arm=a.arm))
    if a.reconcile_exp:
        print("reconcile_expirations:", reconcile_expirations(conn, arm=a.arm))
    if a.check_assignments:
        print("check_assignments:", check_assignments(conn, arm=a.arm))
    if a.rebuild_thompson:
        print("rebuild_thompson:", rebuild_thompson_state(conn))
    if a.dsr_batch:
        res, sr0 = run_dsr_batch(conn)
        print(f"DSR batch (SR_0={sr0:.3f}):")
        for r in res:
            print("  ", r)
    if a.leaderboard:
        print(generate_leaderboard(conn, regime=a.regime))
    conn.close()


if __name__ == "__main__":
    main()
