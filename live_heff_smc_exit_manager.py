"""Phase 3b of live validated-strategy wiring: exit management for
SMC_TRIANGLE positions opened by live_heff_smc_executor.py -- the gap
flagged as top priority in the 2026-07-30 handoff (entries were live and
armed, exits were never built).

Same --arm convention as every other order-placing script in this codebase:
DRY RUN by default, only --arm submits real (paper) closing orders. Exit
rule is bt2_exits.ExitConfig's own unmodified defaults -- the SAME
target(+25%)/stop(-20%)/time-stop(30min no-progress)/forced-close(15:30 ET)
research decided to keep 2026-07-29 (see
HEFF_SMC_B1_BACKTEST_REPORT_EXTENDED_2026-07-29.md), reused here via
live_heff_smc_executor's own constants rather than a second copy that could
drift out of sync with the entry side.

Trades_ledger rows for SMC_TRIANGLE reach OPEN via the SAME
`options_eval.py --reconcile --arm` cron the S1-S10 tournament already runs
every 2min (orb_options_tournament_wrapper.sh) -- this script doesn't
duplicate fill-detection, it just reads the ledger's status column. A
companion guard was added to options_orchestrator.process_exits() so that
existing credit/debit-spread TP/SL sweep skips structure='single_leg_long'
rows -- without it, that sweep would have crashed (KeyError on
meta["entry_credit"], which single-leg positions never set) the moment the
first SMC_TRIANGLE trade reached OPEN.

Position state read from data/live_heff_smc/open_positions.json (written by
the executor at entry); this script enriches it in place with the real
entry_fill_price/filled_at once the broker confirms the fill, and removes
an entry once its exit closes -- same cron-tick, no-daemon persistence
pattern as the rest of this wiring.

Keyed by trade_id (2026-07-31 fix, see live_heff_smc_executor.open_new_positions'
own docstring for the real incident this closes): three signals traded the SAME
contract within 30 minutes while occ-keying was still in place, and the second/
third entries silently overwrote the first's tracking record, orphaning a real
open (paper) position that then sat unmanaged and down -57.6% until manually
caught. Every `occ` used below is read from `pos["occ"]`, not the dict key.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import time
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

import options_orchestrator as oo
from options_eval import connect as eval_connect, init_db as eval_init, resolve_trade as eval_resolve
from live_heff_smc_executor import (
    DB_PATH, POSITIONS_PATH, STRATEGY_ID,
    TARGET_RETURN, STOP_PCT, TIME_STOP_MINUTES, FORCED_CLOSE,
)

ET = ZoneInfo("America/New_York")
ROOT = Path(__file__).resolve().parent
LOG_PATH = ROOT / "logs" / "live_heff_smc_exit_manager.log"
IOC_BUFFER_S = 2.0  # same synthetic-IOC latency buffer as options_orchestrator._submit_close


def _log(msg: str) -> None:
    line = f"{dt.datetime.now(dt.timezone.utc).isoformat()} {msg}"
    print(line, flush=True)
    LOG_PATH.parent.mkdir(exist_ok=True)
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


def _ledger_status(conn, trade_id: str):
    row = conn.execute("SELECT status FROM trades_ledger WHERE trade_id=?", (trade_id,)).fetchone()
    return row["status"] if row else None


def _entry_fill_info(order_id: str):
    """Real filled_avg_price/filled_at from Alpaca for the entry order -- the limit-at-ask entry
    price can differ slightly from the actual fill, and exit math must be anchored to what was
    actually paid, not the order's limit price."""
    try:
        resp = requests.get(oo.PAPER + f"/v2/orders/{order_id}", headers=oo.H, timeout=20)
        o = resp.json()
    except Exception as e:
        _log(f"WARNING: entry order lookup failed for {order_id}: {e}")
        return None, None
    px, ts = o.get("filled_avg_price"), o.get("filled_at")
    if px is None or ts is None:
        return None, None
    return float(px), ts


STOP_EXIT_CUSHION = 0.05  # 5% below the quoted bid for urgency exits (anything but TARGET).

def submit_close(occ: str, bid: float, qty: int, arm: bool, urgent: bool = False) -> dict:
    """Single-leg sell-to-close. Synthetic-IOC: submit -> short blocking buffer -> check fill ->
    cancel any unfilled remainder, exactly options_orchestrator's _submit_close pattern, just
    for a plain single-leg order instead of an mleg spread.

    Pricing: TARGET exits price a marketable limit exactly at the live bid (mirrors
    submit_entry's 'limit at the live ask' convention) -- these aren't urgent and every one so
    far has filled instantly near that price. `urgent=True` (STOP/TIME_STOP/FORCED_CLOSE) prices
    STOP_EXIT_CUSHION below the quoted bid instead. Real incident, 2026-07-31: `oo.leg_quote`
    reads Alpaca's INDICATIVE options feed, which can lag a genuinely fast-moving market by more
    than the couple seconds it takes an order to reach the book. A limit priced exactly at an
    already-stale bid isn't marketable anymore once the real bid has kept falling underneath it
    -- it just rests unfilled. One position's stop retried 6 times over 10 minutes, each retry
    quoting a new (lower) bid that was ALSO already stale by the time it posted, and by the time
    it finally filled the loss was ~3x the intended -20% stop. A marketable sell still fills at
    whatever the real current bid actually is (often better than this limit) -- pricing below
    the last quote doesn't force a worse fill, it just keeps the order aggressive enough to
    actually cross the spread instead of chasing a falling market one stale tick at a time."""
    cushion = STOP_EXIT_CUSHION if urgent else 0.0
    limit_price = round(max(bid * (1 - cushion), 0.01), 2)
    payload = {"symbol": occ, "qty": str(qty), "side": "sell",
               "type": "limit", "limit_price": f"{limit_price:.2f}", "time_in_force": "day"}
    if not arm:
        _log(f"DRY RUN close (not submitted): {occ} limit={limit_price} (cushion={cushion:.0%})")
        return {"submitted": False, "exec_price": None}

    try:
        resp = requests.post(oo.PAPER + "/v2/orders", headers=oo.H, json=payload, timeout=25)
        order = resp.json()
    except Exception as e:
        _log(f"CLOSE SUBMIT FAILED (exception) for {occ}: {e}")
        return {"submitted": False, "exec_price": None}

    oid = order.get("id")
    if resp.status_code not in (200, 201) or not oid:
        _log(f"CLOSE SUBMIT FAILED (status {resp.status_code}) for {occ}: {resp.text[:300]}")
        return {"submitted": False, "exec_price": None}

    time.sleep(IOC_BUFFER_S)
    try:
        chk = requests.get(oo.PAPER + f"/v2/orders/{oid}", headers=oo.H, timeout=20).json()
    except Exception as e:
        _log(f"WARNING: post-submit status check failed for {occ}/{oid}: {e}")
        chk = {}
    filled_qty = int(float(chk.get("filled_qty", 0) or 0))
    if filled_qty < qty:
        try:
            requests.delete(oo.PAPER + f"/v2/orders/{oid}", headers=oo.H, timeout=20)
        except Exception as e:
            _log(f"WARNING: cancel-remainder failed for {occ}/{oid}: {e}")
    if filled_qty == 0:
        _log(f"{occ}: close order {oid} did not fill within {IOC_BUFFER_S}s buffer, left OPEN for next tick")
        return {"submitted": True, "exec_price": None}

    exec_price = float(chk.get("filled_avg_price") or limit_price)
    _log(f"{occ}: CLOSED {filled_qty}/{qty} @ {exec_price:.2f} order_id={oid}")
    return {"submitted": True, "exec_price": exec_price}


def decide_exit(entry_fill_price: float, bid: float, opened_at: dt.datetime, now: dt.datetime):
    """Same rule set as bt2_exits.resolve_exit's validated baseline, evaluated once against the
    live bid instead of replayed minute-by-minute against historical ticks. Priority: premium
    target/stop first (mutually exclusive at any single instant, since target_level > entry >
    stop_level), then the unconditional 15:30 ET forced close, then the no-progress time-stop.
    Forced-close is checked before time-stop here (reversed from the backtest's literal walk
    order) because in that walk time-stop can only ever fire BEFORE the session-end fallback is
    reached -- if we're already past 15:30 with neither leg having triggered, that's the walk's
    'reached the end' branch, not an intermediate time-stop tick. Only affects which reason
    string gets logged when both conditions happen to be true in the same check; never changes
    the exit price or the P&L."""
    target_level = entry_fill_price * (1 + TARGET_RETURN)
    stop_level = entry_fill_price * (1 + STOP_PCT)
    if bid >= target_level:
        return "TARGET"
    if bid <= stop_level:
        return "STOP"
    if now.astimezone(ET).time() >= FORCED_CLOSE:
        return "FORCED_CLOSE"
    elapsed_minutes = (now - opened_at).total_seconds() / 60.0
    if elapsed_minutes >= TIME_STOP_MINUTES and bid <= entry_fill_price:
        return "TIME_STOP"
    return None


def manage_positions(arm: bool) -> dict:
    positions = _load_json(POSITIONS_PATH, {})
    if not positions:
        return {"open": 0, "closed": 0}

    conn = eval_connect(DB_PATH)
    eval_init(conn)
    now = dt.datetime.now(dt.timezone.utc)
    closed = 0
    dirty = False

    for trade_id, pos in list(positions.items()):
        occ = pos["occ"]
        status = _ledger_status(conn, trade_id)
        if status is None:
            _log(f"WARNING: {occ} has no trades_ledger row for trade_id={trade_id}, leaving for manual review")
            continue
        if status == "CLOSED":
            _log(f"{occ}: already CLOSED in ledger, removing from open_positions")
            positions.pop(trade_id)
            dirty = True
            continue
        if status == "PENDING":
            continue  # entry not yet confirmed filled; options_eval --reconcile promotes this within 2min

        if "entry_fill_price" not in pos:
            px, ts = _entry_fill_info(trade_id)
            if px is None:
                _log(f"{occ}: OPEN in ledger but entry fill price not yet available from broker, skipping this tick")
                continue
            pos["entry_fill_price"], pos["filled_at"], pos["status"] = px, ts, "OPEN"
            dirty = True

        entry_fill_price = pos["entry_fill_price"]
        opened_at = dt.datetime.fromisoformat(pos["filled_at"].replace("Z", "+00:00"))

        quote = oo.leg_quote(occ)
        if not quote:
            _log(f"{occ}: no live quote this tick, skipping")
            continue
        bid = float(quote["bp"])

        reason = decide_exit(entry_fill_price, bid, opened_at, now)
        if reason is None:
            continue

        _log(f"{occ}: {reason} triggered (entry={entry_fill_price:.2f} bid={bid:.2f})")
        result = submit_close(occ, bid, qty=1, arm=arm, urgent=(reason != "TARGET"))
        if not arm or result["exec_price"] is None:
            continue  # dry run, or didn't fill this tick -- re-evaluate next tick either way

        realized_pnl = (result["exec_price"] - entry_fill_price) * 100 * 1
        eval_resolve(conn, trade_id, realized_pnl, exit_time=int(now.timestamp()))
        pct = (result["exec_price"] / entry_fill_price - 1) * 100
        oo.notify_telegram(
            f"REAL EXIT (paper): {occ}\n"
            f"{reason} — entry ${entry_fill_price:.2f} -> exit ${result['exec_price']:.2f} ({pct:+.1f}%)\n"
            f"Realized P&L: ${realized_pnl:+.2f}"
        )
        positions.pop(trade_id)
        dirty = True
        closed += 1

    if dirty:
        _save_json(POSITIONS_PATH, positions)
    conn.close()
    return {"open": len(positions), "closed": closed}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--arm", action="store_true", help="submit real paper closing orders; omit for dry run")
    args = ap.parse_args()

    result = manage_positions(arm=args.arm)
    _log(f"tick complete: {result} mode={'ARMED' if args.arm else 'DRY_RUN'}")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
