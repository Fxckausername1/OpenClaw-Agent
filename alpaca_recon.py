#!/usr/bin/env python3
"""alpaca_recon.py — REAL-FILL reconciliation = THE GO-LIVE GATE.

For every order alpaca_executor submitted (data/alpaca_orders.jsonl), pull the ACTUAL Alpaca
fills (entry + the bracket/OTO exit leg) and compute realized R and $ from real prices. Compare
to (a) the order's INTENDED entry/stop (entry slippage in bps) and (b) the idealized sim's
outcome for the same trade_id (paper_trades.csv / orb_paper_trades.csv). Aggregate verdict:
does the edge survive real fills? That number is what gates flipping to live money.

Until the paper bot is armed there are no fills to reconcile — this just reports "ready".

Also writes structured output for the dashboard: data/alpaca_recon_snapshot.json (latest
state, every run) and appends a compact summary line to data/alpaca_recon_history.jsonl
(one row per run that has >=1 paired closed trade) so the dashboard can chart the go-live
slippage trend over time, not just today's read. This runs once/day (folded into
night_report.py at the close) so the history is naturally a daily trend series.

FIXED 2026-07-10: a trade_id used to be reported "open" FOREVER once its own entry order's
nested legs showed no exit fill -- but positions also close via two paths that never touch
that order at all: (b) the EOD flatten (`alpaca_executor.py --flatten`, cron 15:58 ET, ALL
equity positions incl. mean-reversion brackets, not just ORB) and (c) the cut-loser rotation
(`evaluate_replacement()`), both of which call `api.close_position()` and submit a brand-new
market order that is never written back to alpaca_orders.jsonl. Fix: when an order's own legs
show no exit fill, cross-check the symbol against CURRENT live positions. Still held -> still
open, unchanged. Not held -> it closed some other way; search order history for the real
closing fill (find_external_close()) and reconcile off that price instead of guessing. If no
matching fill can be found, mark it closed (it isn't in current positions, so it isn't open)
but leave real_r/real_d null rather than fabricate a number. See HANDOFF.md / the 2026-07-10
recon-open-fix note for the audit trail (KHC/MRNA/DXCM hand-checked before generalizing).

Same fix day, second finding: a chunk of the "open" pile was never even a real position --
the ENTRY leg itself (the limit order) expired unfilled (day order timed out, no shares ever
bought) and Alpaca's own order status already says so (`status` in TERMINAL_UNFILLED_STATUSES
below). The old code short-circuited on `entry_fill is None` and reported "open: true" with no
further check at all, so a dead 16-day-old order sat there forever looking like a live trade.
No position/money was ever involved (this is a labeling bug, not a hidden-fill bug), but it's
the same "permanently wrong state" flavor, so it's fixed the same way: check the entry order's
own terminal status and report it honestly instead of defaulting to "open".

Run: ./venv/bin/python alpaca_recon.py [--live] [--selftest]
"""
import csv
import json
import argparse
import sys
import statistics as st
from datetime import datetime, timezone
from pathlib import Path

import alpaca_executor as ax   # reuse creds / endpoints / Alpaca client

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
ORDERS_LOG = DATA / "alpaca_orders.jsonl"
SNAPSHOT_PATH = DATA / "alpaca_recon_snapshot.json"
HISTORY_PATH = DATA / "alpaca_recon_history.jsonl"
SIM_CSVS = ["paper_trades.csv", "orb_paper_trades.csv"]

# Alpaca order statuses that mean "will never fill" -- if the entry leg is stuck in one of
# these with no filled_avg_price, no position was ever opened, so it isn't "open" in any
# meaningful sense. (Deliberately NOT included: new/accepted/pending_new/partially_filled/
# accepted_for_bidding/pending_replace/calculated -- those are still live and correctly stay
# "open" until they resolve one way or the other.)
TERMINAL_UNFILLED_STATUSES = {"expired", "canceled", "rejected", "done_for_day", "stopped"}


def load_sim_outcomes():
    out = {}
    for n in SIM_CSVS:
        p = DATA / n
        if not p.exists():
            continue
        with p.open() as f:
            for r in csv.DictReader(f):
                try:
                    out[r["trade_id"]] = (float(r["outcome_r"]), float(r.get("dollar_pnl") or 0))
                except Exception:
                    pass
    return out


def find_external_close(api, symbol, after_iso, closing_side, limit=50):
    """A trade's own entry order shows no exit fill on its nested legs, but the symbol is no
    longer in current live positions -- it was closed via EOD flatten or cut-loser rotation,
    which submit a brand-new, unlinked market (or limit) order. Search order history for the
    first FILLED order on this symbol, on the closing side, after the entry fill -- that's the
    real close. Returns {"price", "at", "order_id"} or None if nothing matches.

    NOTE: Alpaca's `symbols=` filter does not reliably exclude multi-leg (mleg) option orders
    from other systems sharing this account (they come back with a blank top-level `symbol`,
    populated only on their sub-legs) -- always re-check `o.get("symbol") == symbol` client-side
    rather than trusting the query param alone.
    """
    try:
        orders = api._get(
            f"/v2/orders?symbols={symbol}&status=closed&after={after_iso}"
            f"&direction=asc&limit={limit}"
        )
    except Exception as e:
        print(f"    external-close lookup failed for {symbol}: {e}")
        return None
    for o in orders:
        if o.get("symbol") != symbol:
            continue  # mleg noise -- see docstring
        if o.get("status") != "filled":
            continue
        if o.get("side") != closing_side:
            continue
        if not o.get("filled_avg_price"):
            continue
        return {"price": float(o["filled_avg_price"]), "at": o.get("filled_at"), "order_id": o.get("id")}
    return None


def compute_real_r(entry_fill, exit_fill, d, risk):
    """Same R/$ math the leg-detected path always used -- pulled out standalone so both the
    leg-fill and external-close paths share one formula, and so --selftest can exercise it
    without a network call."""
    real_r = ((exit_fill - entry_fill) * d / risk) if (exit_fill and risk > 0) else None
    return real_r


def _write_snapshot(snap):
    DATA.mkdir(parents=True, exist_ok=True)
    SNAPSHOT_PATH.write_text(json.dumps(snap, indent=2))


def _append_history(snap):
    """One compact trend row per run that had >=1 paired closed trade -- this only runs
    once/day (night_report.py at the close), so the file is naturally a daily trend series."""
    agg = snap.get("aggregate") or {}
    if not agg.get("slippage"):
        return
    row = {
        "ts": snap["generated_at"],
        "closed_n": agg["closed"],
        "paired_n": agg["ideal"]["paired_n"],
        "real_avg_r": agg["real"]["avg_r"],
        "real_total_d": agg["real"]["total_d"],
        "ideal_avg_r": agg["ideal"]["avg_r"],
        "avg_entry_slip_bps": agg["slippage"]["avg_entry_bps"],
        "per_trade_r_diff": agg["slippage"]["per_trade_r_diff"],
        "verdict": agg["verdict"],
    }
    DATA.mkdir(parents=True, exist_ok=True)
    with HISTORY_PATH.open("a") as f:
        f.write(json.dumps(row) + "\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        sys.exit(0 if selftest() else 1)
    generated_at = datetime.now(timezone.utc).isoformat()

    if not ORDERS_LOG.exists():
        print("no data/alpaca_orders.jsonl yet — paper bot not armed. Recon is READY; "
              "it will populate once alpaca_executor --arm starts filling.")
        _write_snapshot({"generated_at": generated_at, "armed": False, "aggregate": None, "rows": []})
        return

    api = ax.Alpaca(live=a.live)
    sim = load_sim_outcomes()

    try:
        live_positions = {p.get("symbol") for p in api.positions()}
    except Exception as e:
        print(f"WARNING: could not fetch live positions ({e}) -- falling back to leg-only "
              f"open/closed detection for this run, open flags may be stale")
        live_positions = None

    rows = []
    for line in ORDERS_LOG.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        rec = json.loads(line)
        oid = rec.get("alpaca")
        if not isinstance(oid, str):
            continue  # error/skipped submission record
        try:
            o = api._get(f"/v2/orders/{oid}?nested=true")
        except Exception as e:
            print("skip", rec.get("trade_id"), e)
            continue
        entry_fill = float(o["filled_avg_price"]) if o.get("filled_avg_price") else None
        if entry_fill is None:
            if o.get("status") in TERMINAL_UNFILLED_STATUSES:
                # Dead order, never became a position -- not "open", just never happened.
                rows.append(dict(tid=rec["trade_id"], open=False, entry_slip_bps=None,
                                 real_r=None, real_d=None, sim_r=None, sim_d=None,
                                 close_via=f"never_filled:{o.get('status')}"))
            else:
                # Still genuinely working (new/accepted/partially_filled/...) -- unchanged.
                rows.append(dict(tid=rec["trade_id"], open=True, entry_slip_bps=None,
                                 real_r=None, real_d=None, sim_r=None, sim_d=None, close_via=None))
            continue
        intended_entry = float(rec["payload"]["limit_price"])
        intended_stop = float(rec["payload"]["stop_loss"]["stop_price"])
        d = 1 if rec["payload"]["side"] == "buy" else -1
        risk = abs(intended_entry - intended_stop)
        symbol = rec["payload"]["symbol"]

        exit_fill = None
        for leg in (o.get("legs") or []):
            if leg.get("filled_avg_price"):
                exit_fill = float(leg["filled_avg_price"])
        close_via = "leg" if exit_fill is not None else None
        is_open = exit_fill is None

        if exit_fill is None and live_positions is not None and symbol not in live_positions:
            # Not on this order's own legs, AND the account no longer holds the symbol -- it
            # closed via EOD flatten or cut-loser rotation (see module docstring). Go find the
            # real closing fill instead of leaving this a permanent ghost-open.
            is_open = False
            closing_side = "sell" if rec["payload"]["side"] == "buy" else "buy"
            found = find_external_close(api, symbol, o.get("filled_at"), closing_side)
            if found:
                exit_fill = found["price"]
                close_via = f"external:{found['order_id'][:8]}"
                print(f"  {rec['trade_id']}: resolved external close (flatten/rotation) -- "
                      f"{closing_side} {exit_fill} @ {found['at']}")
            else:
                close_via = "external:unresolved"
                print(f"  {rec['trade_id']}: not in live positions but no matching closing "
                      f"fill found in order history for {symbol} after {o.get('filled_at')} -- "
                      f"marking closed, real_r/real_d left null")
        # else: live_positions fetch failed (None), or symbol IS still held -> genuinely open,
        # leave is_open True / exit_fill None exactly like the old behavior (no regression).

        real_r = compute_real_r(entry_fill, exit_fill, d, risk)
        real_d = ((exit_fill - entry_fill) * d * int(rec["payload"]["qty"])) if exit_fill else None
        sim_r, sim_d = sim.get(rec["trade_id"], (None, None))
        rows.append(dict(tid=rec["trade_id"], open=is_open,
                         entry_slip_bps=(entry_fill - intended_entry) * d / intended_entry * 1e4,
                         real_r=real_r, real_d=real_d, sim_r=sim_r, sim_d=sim_d, close_via=close_via))

    closed = [r for r in rows if r["real_r"] is not None]
    still_open = [r for r in rows if r["open"]]
    never_filled = [r for r in rows if not r["open"]
                     and (r.get("close_via") or "").startswith("never_filled")]
    unresolved = [r for r in rows if not r["open"] and r["real_r"] is None and r not in never_filled]
    print(f"orders: {len(rows)} | filled+closed(r known): {len(closed)} | "
          f"closed(fill unresolved): {len(unresolved)} | never filled (dead order): "
          f"{len(never_filled)} | still open: {len(still_open)}")
    aggregate = {"orders_total": len(rows), "closed": len(closed), "open": len(still_open),
                 "closed_unresolved": len(unresolved), "never_filled": len(never_filled)}
    if closed:
        rr = [r["real_r"] for r in closed]
        rd = [r["real_d"] for r in closed]
        print(f"REAL fills : total {sum(rr):+.2f}R | avg {st.mean(rr):+.3f}R/tr | $ {sum(rd):+.2f}")
        aggregate["real"] = {"total_r": sum(rr), "avg_r": st.mean(rr), "total_d": sum(rd)}
        paired = [r for r in closed if r["sim_r"] is not None]
        if paired:
            sr = [r["sim_r"] for r in paired]
            print(f"IDEALIZED  : total {sum(sr):+.2f}R | avg {st.mean(sr):+.3f}R/tr | (same {len(paired)} trades)")
            print(f"slippage   : avg entry {st.mean([r['entry_slip_bps'] for r in closed]):+.1f} bps | "
                  f"per-trade R real-vs-ideal {st.mean(rr) - st.mean(sr):+.3f}")
            verdict = "✅ edge SURVIVES real fills" if st.mean(rr) > 0 else "⚠️ edge GONE on real fills — DO NOT go live"
            print(f"GO-LIVE READ: {verdict}")
            aggregate["ideal"] = {"total_r": sum(sr), "avg_r": st.mean(sr), "paired_n": len(paired)}
            aggregate["slippage"] = {
                "avg_entry_bps": st.mean([r["entry_slip_bps"] for r in closed]),
                "per_trade_r_diff": st.mean(rr) - st.mean(sr),
            }
            aggregate["verdict"] = "edge_survives" if st.mean(rr) > 0 else "edge_gone"
    for r in rows:
        close_via = r.get("close_via") or ""
        if r["open"]:
            tag = "OPEN"
        elif r["real_r"] is not None:
            tag = f"realR={r['real_r']:+.2f} simR={r['sim_r']}"
        elif close_via.startswith("never_filled"):
            tag = f"NEVER FILLED ({close_via.split(':', 1)[1]})"
        else:
            tag = "CLOSED(unresolved)"
        slip = f" slip={r['entry_slip_bps']:+.0f}bps" if r["entry_slip_bps"] is not None else ""
        print(f"  {r['tid']:<28} {tag}{slip}")

    snap = {"generated_at": generated_at, "armed": True, "aggregate": aggregate, "rows": rows}
    _write_snapshot(snap)
    _append_history(snap)


# ===================================================== SELFTEST
def selftest():
    ok = True

    def check(name, good):
        nonlocal ok
        ok &= bool(good)
        print(f"  [{'OK' if good else 'FAIL'}] {name}")

    # compute_real_r: same formula as the leg-detected path, just fed an external fill price.
    check("compute_real_r LONG winner", abs(compute_real_r(100.0, 102.0, 1, 1.0) - 2.0) < 1e-9)
    check("compute_real_r SHORT winner", abs(compute_real_r(100.0, 98.0, -1, 1.0) - 2.0) < 1e-9)
    check("compute_real_r LONG loser", abs(compute_real_r(100.0, 99.0, 1, 1.0) - (-1.0)) < 1e-9)
    check("compute_real_r zero risk -> None", compute_real_r(100.0, 102.0, 1, 0.0) is None)
    check("compute_real_r no exit -> None", compute_real_r(100.0, None, 1, 1.0) is None)

    # find_external_close: fake api._get stub, no network. Exercises the mleg-noise filter,
    # the closing-side filter, the status filter, and "first match wins" ordering.
    class FakeApi:
        def __init__(self, orders):
            self._orders = orders

        def _get(self, path):
            return self._orders

    # Case 1: real close exists, buried behind an mleg order (blank symbol) and a canceled
    # duplicate of the order's own SL leg -- must still find the market sell.
    orders = [
        {"symbol": "", "status": "filled", "side": "sell", "filled_avg_price": "9.99",
         "filled_at": "2026-01-01T00:00:00Z", "id": "mleg-noise-should-be-skipped"},
        {"symbol": "KHC", "status": "canceled", "side": "sell", "filled_avg_price": None,
         "filled_at": None, "id": "own-sl-leg-should-be-skipped"},
        {"symbol": "KHC", "status": "filled", "side": "sell", "filled_avg_price": "23.67",
         "filled_at": "2026-06-25T17:39:22Z", "id": "8ad096cc-real-close"},
        {"symbol": "KHC", "status": "filled", "side": "buy", "filled_avg_price": "24.00",
         "filled_at": "2026-06-26T00:00:00Z", "id": "later-reentry-should-be-skipped"},
    ]
    found = find_external_close(FakeApi(orders), "KHC", "2026-06-24T15:52:21Z", "sell")
    check("find_external_close skips mleg noise + own canceled leg, finds real close",
          found is not None and abs(found["price"] - 23.67) < 1e-9
          and found["order_id"] == "8ad096cc-real-close")

    # Case 2: nothing matches (still resting / no closing fill yet) -> None, not a guess.
    found2 = find_external_close(FakeApi([{"symbol": "XYZ", "status": "new", "side": "sell",
                                            "filled_avg_price": None, "filled_at": None,
                                            "id": "still-open"}]),
                                  "XYZ", "2026-01-01T00:00:00Z", "sell")
    check("find_external_close returns None when nothing matches", found2 is None)

    # Case 3: _get raising (network error) is swallowed, returns None rather than crashing the run.
    class ErrApi:
        def _get(self, path):
            raise RuntimeError("boom")

    found3 = find_external_close(ErrApi(), "KHC", "2026-01-01T00:00:00Z", "sell")
    check("find_external_close survives a lookup exception", found3 is None)

    # TERMINAL_UNFILLED_STATUSES: the set the main loop checks entry-order status against.
    check("expired entry is terminal-unfilled", "expired" in TERMINAL_UNFILLED_STATUSES)
    check("canceled entry is terminal-unfilled", "canceled" in TERMINAL_UNFILLED_STATUSES)
    check("a still-live status is NOT terminal-unfilled", "new" not in TERMINAL_UNFILLED_STATUSES)
    check("partially_filled is NOT terminal-unfilled",
          "partially_filled" not in TERMINAL_UNFILLED_STATUSES)

    print("selftest:", "PASS" if ok else "FAIL")
    return ok


if __name__ == "__main__":
    main()
