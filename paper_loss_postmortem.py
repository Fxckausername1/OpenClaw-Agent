"""READ-ONLY postmortem of the 2026-07-31 paper trades (~-$101 over 10
entries). Operational attribution only -- explicitly NOT an optimization
input, and nothing here tunes Variant B.

Strictly read-only: issues only HTTP GETs against the Alpaca PAPER order
history, and reads local logs. Places, cancels and modifies nothing. The
paper-endpoint guard runs first so this cannot be pointed at live by
accident.

Alpaca's own order records are treated as authoritative for fills; local
logs supply signal/trigger/selector context Alpaca never sees.
"""
from __future__ import annotations

import json
import re
import sys
from collections import defaultdict

import options_orchestrator as oo
import requests

from smc.paper_guard import enforce_paper_mode

DAY = "2026-07-31"
LOG_DIR = "logs"


def fetch_orders():
    host = enforce_paper_mode(oo.PAPER, (oo.H or {}).get("APCA-API-KEY-ID"),
                              exit_on_violation=False)
    print(f"# paper guard OK -> {host}")
    out, after = [], f"{DAY}T00:00:00Z"
    while True:
        r = requests.get(
            oo.PAPER + "/v2/orders",
            headers=oo.H,
            params={"status": "all", "after": after, "until": "2026-08-01T00:00:00Z",
                    "limit": 500, "direction": "asc", "nested": "true"},
            timeout=30)
        r.raise_for_status()
        batch = r.json()
        if not batch:
            break
        out.extend(batch)
        if len(batch) < 500:
            break
        after = batch[-1]["submitted_at"]
    return out


def parse_logs():
    """Pulls trigger + submitted limit per order_id out of the executor log,
    and any exit-side context out of the exit-manager log."""
    ctx = {}
    sub_re = re.compile(
        r"^(?P<ts>\S+) ORDER SUBMITTED \(paper\): (?P<occ>\S+) limit=(?P<limit>\S+) order_id=(?P<oid>\S+)")
    tg_re = re.compile(r"REAL ENTRY \(paper\): (?P<occ>\S+)\\n(?P<side>\w+) (?P<trig>\w+) trigger")
    pending_trigger = {}
    try:
        with open(f"{LOG_DIR}/live_heff_smc_executor.log") as fh:
            lines = fh.readlines()
    except OSError:
        lines = []
    for line in lines:
        if not line.startswith(DAY[:4]):
            continue
        m = sub_re.match(line)
        if m and m.group("ts").startswith(DAY):
            ctx[m.group("oid")] = {"submit_ts": m.group("ts"), "occ": m.group("occ"),
                                    "submitted_limit": float(m.group("limit")),
                                    "trigger": None}
        t = tg_re.search(line)
        if t:
            pending_trigger.setdefault(t.group("occ"), []).append(
                (t.group("side"), t.group("trig")))
    # Telegram lines follow their own entry, so attach by OCC in order.
    used = defaultdict(int)
    for oid, c in sorted(ctx.items(), key=lambda kv: kv[1]["submit_ts"]):
        opts = pending_trigger.get(c["occ"], [])
        i = used[c["occ"]]
        if i < len(opts):
            c["side"], c["trigger"] = opts[i]
            used[c["occ"]] += 1
    return ctx


def main():
    orders = fetch_orders()
    ctx = parse_logs()
    print(f"# alpaca orders returned for {DAY}: {len(orders)}")

    # Group by symbol so entries and their exits sit together.
    by_symbol = defaultdict(list)
    for o in orders:
        by_symbol[o.get("symbol")].append(o)

    rows = []
    for sym, os_ in sorted(by_symbol.items()):
        for o in sorted(os_, key=lambda x: x["submitted_at"]):
            rows.append({
                "symbol": sym,
                "id": o.get("id"),
                "client_order_id": o.get("client_order_id"),
                "side": o.get("side"),
                "type": o.get("type"),
                "qty": o.get("qty"),
                "filled_qty": o.get("filled_qty"),
                "limit_price": o.get("limit_price"),
                "filled_avg_price": o.get("filled_avg_price"),
                "status": o.get("status"),
                "submitted_at": o.get("submitted_at"),
                "filled_at": o.get("filled_at"),
                "canceled_at": o.get("canceled_at"),
                "expired_at": o.get("expired_at"),
                "replaced_by": o.get("replaced_by"),
                "log_trigger": (ctx.get(o.get("id")) or {}).get("trigger"),
                "log_submitted_limit": (ctx.get(o.get("id")) or {}).get("submitted_limit"),
            })

    with open("PAPER_LOSS_POSTMORTEM_20260731_raw.json", "w") as fh:
        json.dump({"orders": rows, "log_context": ctx}, fh, indent=2, default=str)

    print(f"{'symbol':<22}{'side':<5}{'type':<8}{'st':<11}{'qty':>4}{'fill':>5}"
          f"{'lim':>7}{'avg':>7}  submitted            filled")
    for r in rows:
        print(f"{r['symbol']:<22}{str(r['side']):<5}{str(r['type']):<8}"
              f"{str(r['status']):<11}{str(r['qty']):>4}{str(r['filled_qty']):>5}"
              f"{str(r['limit_price'] or ''):>7}{str(r['filled_avg_price'] or ''):>7}  "
              f"{str(r['submitted_at'])[:19]}  {str(r['filled_at'])[:19]}")

    entries = [r for r in rows if r["side"] == "buy"]
    exits = [r for r in rows if r["side"] == "sell"]
    print(f"\n# buy orders: {len(entries)}   sell orders: {len(exits)}")
    realized = 0.0
    for sym in sorted(by_symbol):
        b = sum(float(r["filled_avg_price"] or 0) * float(r["filled_qty"] or 0) * 100
                for r in rows if r["symbol"] == sym and r["side"] == "buy")
        s = sum(float(r["filled_avg_price"] or 0) * float(r["filled_qty"] or 0) * 100
                for r in rows if r["symbol"] == sym and r["side"] == "sell")
        if b or s:
            print(f"  {sym:<22} bought ${b:>8.2f}  sold ${s:>8.2f}  net ${s-b:>8.2f}")
            realized += s - b
    print(f"\n# TOTAL REALIZED (excl. fees): ${realized:.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
