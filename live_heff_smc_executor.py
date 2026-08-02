"""Phase 3 of live validated-strategy wiring: real order submission +
exit management, for triangle signals that found a real contract in Phase 2.

Same --arm convention as every other order-placing script in this codebase
(alpaca_executor.py, options_orchestrator.py): defaults to DRY RUN (logs
exactly what it would submit, submits nothing), only --arm places a real
(paper) order. Entry: single-leg option BUY, limit at the live ask (same
"marketable limit" convention bt2_fills.py's base_realistic model assumes).
Exit: baseline target(+25%)/stop(-20%)/time-stop(30min no-progress)/forced-
close(15:30 ET) -- the SAME unmodified defaults tonight's research decided
to keep, not a new invented rule.

Position state persisted to data/live_heff_smc/open_positions.json so exit
management survives across cron ticks (this box's established cron-driven,
no-daemon pattern).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
import uuid
from pathlib import Path
from zoneinfo import ZoneInfo

import options_orchestrator as oo
from options_eval import connect as eval_connect, init_db as eval_init, \
    record_open as eval_record_open, TradeStatus

ET = ZoneInfo("America/New_York")
ROOT = Path(__file__).resolve().parent
SHADOW_LOG_PATH = ROOT / "data" / "live_heff_smc" / "phase2_shadow_decisions.jsonl"
ENTERED_PATH = ROOT / "data" / "live_heff_smc" / "phase3_entered.json"
POSITIONS_PATH = ROOT / "data" / "live_heff_smc" / "open_positions.json"
LOG_PATH = ROOT / "logs" / "live_heff_smc_executor.log"
DB_PATH = ROOT / "data" / "options_eval.db"
STRATEGY_ID = "SMC_TRIANGLE"

TARGET_RETURN = 0.25
STOP_PCT = -0.20
TIME_STOP_MINUTES = 30
FORCED_CLOSE = dt.time(15, 30)


def _log(msg: str) -> None:
    line = f"{dt.datetime.now(dt.timezone.utc).isoformat()} {msg}"
    print(line, flush=True)
    ROOT_LOGS = ROOT / "logs"
    ROOT_LOGS.mkdir(exist_ok=True)
    with open(LOG_PATH, "a") as f:
        f.write(line + "\n")


def _load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def _save_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2, default=str))
    tmp.replace(path)


def _occ_symbol(underlying: str, expiration: str, strike: float, right: str) -> str:
    exp = dt.date.fromisoformat(expiration)
    return f"{underlying}{exp:%y%m%d}{right}{int(round(strike * 1000)):08d}"


def _notify_telegram(msg: str) -> None:
    try:
        subprocess.run(
            ["/usr/bin/openclaw", "message", "send", "--channel", "telegram",
             "--target", "7590346809", "--message", msg],
            capture_output=True, timeout=150,
        )
    except Exception as e:
        _log(f"WARNING: telegram notify failed: {e}")


def record_to_dashboard_ledger(decision: dict, order_id: str, entry_ask: float) -> None:
    """Writes into options_eval.db's trades_ledger with strategy_id=SMC_TRIANGLE --
    the SAME table/schema the existing S1-S10 tournament panel already reads
    (dashboard_snapshot.py's options_leaderboard()/_tournament_realized_today()/
    _tournament_peak_drawdown()), so this shows up on the live dashboard with
    ZERO frontend changes (the dashboard already fetches data at runtime, no
    Netlify deploy needed -- see TD6_SCOPING_NOTES.md's own finding on this).
    A tournament_state row for SMC_TRIANGLE was seeded once (2026-07-30) so it
    appears as its own leaderboard entry, not blended anonymously into S1-S10.

    Uses options_eval.record_open() (the SAME helper run_tournament() uses for
    S1-S10) instead of a hand-rolled INSERT: status=PENDING, not OPEN -- the
    order hasn't actually been confirmed filled yet, and the existing
    `options_eval.py --reconcile --arm` cron (already running every 2min for
    the tournament, orb_options_tournament_wrapper.sh) promotes PENDING->OPEN
    on confirmed fill, exactly like every other strategy in this table. The
    original version here inserted status='OPEN' directly AND manually bumped
    tournament_state.trade_count at entry -- record_open never does the
    latter (only resolve_trade does, at CLOSE), so leaving that manual
    increment in place would have double-counted every trade once exit
    management (live_heff_smc_exit_manager.py) started calling resolve_trade
    on the same strategy_id. Fixed here rather than carried forward."""
    c = decision["contract"]
    ticker = decision["trigger"]["ticker"]
    occ = _occ_symbol(ticker, c["expiration"], c["strike"], c["right"])
    legs = {
        "legs": [{"strike": c["strike"], "type": c["right"], "side": "BUY", "occ": occ}],
        "entry_debit": entry_ask, "qty": 1, "max_loss": round(entry_ask * 100, 2),
        "kind": "debit", "structure": "single_leg_long",
        "direction": 1 if decision["trigger"]["side"] == "long" else -1,
        "trigger": decision["trigger"]["trigger"], "score": decision["trigger"]["score"],
    }
    try:
        conn = eval_connect(DB_PATH)
        eval_init(conn)
        eval_record_open(conn, order_id, STRATEGY_ID, legs, "UNKNOWN",
                          initial_risk=round(entry_ask * 100, 2), status=TradeStatus.PENDING)
        conn.close()
    except Exception as e:
        _log(f"WARNING: dashboard ledger write failed for {occ}: {e}")


def find_new_entries(entered: set) -> list:
    if not SHADOW_LOG_PATH.exists():
        return []
    out = []
    for line in SHADOW_LOG_PATH.read_text().splitlines():
        if not line.strip():
            continue
        d = json.loads(line)
        if d.get("found") and d["key"] not in entered:
            out.append(d)
    return out


def submit_entry(decision: dict, arm: bool) -> dict:
    c = decision["contract"]
    ticker = decision["trigger"]["ticker"]
    occ = _occ_symbol(ticker, c["expiration"], c["strike"], c["right"])
    limit_price = round(c["ask"], 2)
    payload = {
        "symbol": occ, "qty": "1", "side": "buy",
        "type": "limit", "limit_price": f"{limit_price:.2f}", "time_in_force": "day",
    }

    if not arm:
        _log(f"DRY RUN entry (not submitted): {occ} limit={limit_price} trigger={decision['key']}")
        return {"submitted": False, "mode": "DRY_RUN", "payload": payload}

    resp = None
    try:
        import requests
        resp = requests.post(oo.PAPER + "/v2/orders", headers=oo.H, json=payload, timeout=25)
    except Exception as e:
        _log(f"ORDER SUBMIT FAILED (exception) for {occ}: {e}")
        return {"submitted": False, "mode": "ARMED", "error": str(e), "payload": payload}

    if resp.status_code not in (200, 201):
        _log(f"ORDER SUBMIT FAILED (status {resp.status_code}) for {occ}: {resp.text[:300]}")
        return {"submitted": False, "mode": "ARMED", "status": resp.status_code, "payload": payload}

    order = resp.json()
    _log(f"ORDER SUBMITTED (paper): {occ} limit={limit_price} order_id={order.get('id')}")
    return {"submitted": True, "mode": "ARMED", "order": order, "payload": payload}


def open_new_positions(arm: bool) -> int:
    """Real incident, 2026-07-31: `positions` used to be keyed by `occ` (the contract
    symbol). Three separate triangle signals all selected the SAME contract
    (QQQ260731C00690000) within a 30-minute span; each new entry's `positions[occ] = {...}`
    silently OVERWROTE the previous one's tracking record, orphaning it from
    open_positions.json entirely even though its real (paper) position and its
    trades_ledger row were both still open. The exit manager never saw it again --
    it sat unmanaged, down -57.6%, until manually caught and closed. Fixed two ways:
    keyed by order_id now (unique per trade, never collides), AND a new entry is
    skipped outright if that occ is already being tracked, so the SAME contract can't
    stack multiple concurrent, mutually-invisible-to-each-other positions in the
    first place -- not just "tracked correctly if it happens" but "can't happen"."""
    entered = set(_load_json(ENTERED_PATH, []))
    new = find_new_entries(entered)
    if not new:
        return 0

    positions = _load_json(POSITIONS_PATH, {})
    already_open_occs = {p["occ"] for p in positions.values()}
    opened = 0
    for decision in new:
        c = decision["contract"]
        occ = _occ_symbol(decision["trigger"]["ticker"], c["expiration"], c["strike"], c["right"])
        entered.add(decision["key"])
        if occ in already_open_occs:
            _log(f"SKIP entry: {occ} already has an open SMC_TRIANGLE position, trigger={decision['key']}")
            continue

        result = submit_entry(decision, arm)
        if result["submitted"]:
            order_id = result["order"].get("id") or str(uuid.uuid4())
            positions[order_id] = {
                "occ": occ, "trigger_key": decision["key"], "contract": c,
                "entry_ask": c["ask"], "opened_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "order_id": order_id, "status": "PENDING_FILL",
            }
            already_open_occs.add(occ)
            opened += 1
            record_to_dashboard_ledger(decision, order_id, c["ask"])
            _notify_telegram(
                f"REAL ENTRY (paper): {occ}\n"
                f"{decision['trigger']['side'].upper()} {decision['trigger']['trigger']} "
                f"trigger, limit ${c['ask']:.2f}\n"
                f"Now showing on the dashboard under SMC_TRIANGLE."
            )
    _save_json(ENTERED_PATH, sorted(entered))
    _save_json(POSITIONS_PATH, positions)
    return opened


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--arm", action="store_true", help="submit real paper orders; omit for dry run")
    args = ap.parse_args()

    n = open_new_positions(arm=args.arm)
    _log(f"tick complete: {n} new entries processed, mode={'ARMED' if args.arm else 'DRY_RUN'}")
    print(json.dumps({"new_entries": n, "mode": "ARMED" if args.arm else "DRY_RUN"}))


if __name__ == "__main__":
    main()
