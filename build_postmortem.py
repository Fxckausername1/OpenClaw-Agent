"""READ-ONLY: assembles the per-trade attribution table for the 10 paper
entries of 2026-07-31 by joining four local sources with the Alpaca PAPER
order records already pulled by paper_loss_postmortem.py.

Sources, and what each is authoritative for:
  triggers.jsonl               -- bar time, trigger type, side, score, detected_at
  phase2_shadow_decisions.jsonl-- selected contract, delta, bid/ask at decision, decided_at
  phase3_entered.json          -- which signal keys actually became entries
  executor / exit-manager logs -- submission, exit reason, retry behavior
  PAPER_LOSS_POSTMORTEM_..json -- Alpaca fills (AUTHORITATIVE for prices/times)

Writes nothing but its own report. Places no orders.
"""
from __future__ import annotations

import datetime as dt
import json
import re
from collections import defaultdict
from pathlib import Path

D = Path("data/live_heff_smc")
DAY = "2026-07-31"


def ts(x):
    if not x:
        return None
    return dt.datetime.fromisoformat(str(x).replace("Z", "+00:00"))


def load_jsonl(p):
    out = []
    for line in Path(p).read_text().splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return out


def main():
    entered = set(json.loads((D / "phase3_entered.json").read_text()))
    shadow = {d.get("key"): d for d in load_jsonl(D / "phase2_shadow_decisions.jsonl")}
    alp = json.loads(Path("PAPER_LOSS_POSTMORTEM_20260731_raw.json").read_text())["orders"]

    # Alpaca options orders only, grouped by OCC.
    opt = [o for o in alp if str(o["symbol"]).startswith("QQQ26")]
    buys = defaultdict(list)
    sells = defaultdict(list)
    for o in sorted(opt, key=lambda x: x["submitted_at"]):
        (buys if o["side"] == "buy" else sells)[o["symbol"]].append(o)

    # Exit-manager reasons per OCC in time order.
    exit_re = re.compile(r"^(?P<ts>\S+) (?P<occ>QQQ\S+): (?P<kind>STOP|TARGET|TIME|EOD) triggered \(entry=(?P<entry>\S+) bid=(?P<bid>\S+)\)")
    retry_re = re.compile(r"^(?P<ts>\S+) (?P<occ>QQQ\S+): close order \S+ did not fill within (?P<buf>\S+)s buffer")
    exits = defaultdict(list)
    retries = defaultdict(list)
    for line in Path("logs/live_heff_smc_exit_manager.log").read_text().splitlines():
        if not line.startswith(DAY):
            continue
        m = exit_re.match(line)
        if m:
            exits[m.group("occ")].append(m.groupdict())
        r = retry_re.match(line)
        if r:
            retries[r.group("occ")].append(r.groupdict())

    # Executor submissions per OCC in time order.
    sub_re = re.compile(r"^(?P<ts>\S+) ORDER SUBMITTED \(paper\): (?P<occ>\S+) limit=(?P<limit>\S+) order_id=(?P<oid>\S+)")
    subs = defaultdict(list)
    for line in Path("logs/live_heff_smc_executor.log").read_text().splitlines():
        if not line.startswith(DAY):
            continue
        m = sub_re.match(line)
        if m:
            subs[m.group("occ")].append(m.groupdict())

    rows = []
    used = defaultdict(int)
    for key in sorted(entered, key=lambda k: int(k.split(":")[1])):
        sh = shadow.get(key) or {}
        trig = sh.get("trigger") or {}
        sel = sh.get("selected") or sh.get("contract") or {}
        bar_time = trig.get("time")
        detected = ts(trig.get("detected_at"))
        decided = ts(sh.get("decided_at"))
        occ = sel.get("occ")
        # Fall back to matching submissions in chronological order when the
        # shadow record does not carry the OCC.
        rows.append({
            "key": key, "bar_time_et": bar_time, "trigger": trig.get("trigger"),
            "side": trig.get("side"), "score": trig.get("score"),
            "detected_at": detected, "decided_at": decided,
            "sel_occ": occ, "sel_delta": sel.get("delta"),
            "sel_bid": sel.get("bid"), "sel_ask": sel.get("ask"),
            "found": sh.get("found"), "reason": sh.get("reason"),
        })

    print("=" * 100)
    print("SIGNAL -> DECISION CHAIN (the 10 entered signals)")
    print("=" * 100)
    print(f"{'key':<26}{'trig':<14}{'side':<6}{'bar_time':<21}{'detect_lag':>11}{'decide_lag':>11}")
    for r in rows:
        dl = (r["detected_at"] - dt.datetime.fromisoformat(r["bar_time_et"]).replace(
            tzinfo=dt.timezone(dt.timedelta(hours=-4)))).total_seconds() if (r["detected_at"] and r["bar_time_et"]) else None
        cl = (r["decided_at"] - r["detected_at"]).total_seconds() if (r["decided_at"] and r["detected_at"]) else None
        print(f"{r['key']:<26}{str(r['trigger']):<14}{str(r['side']):<6}{str(r['bar_time_et']):<21}"
              f"{('%.0fs' % dl) if dl is not None else 'n/a':>11}{('%.0fs' % cl) if cl is not None else 'n/a':>11}")

    print()
    print("=" * 100)
    print("PER-CONTRACT EXECUTION (Alpaca authoritative)")
    print("=" * 100)
    for occ in sorted(set(list(buys) + list(sells))):
        print(f"\n--- {occ} ---")
        for b in buys[occ]:
            print(f"  BUY  submitted {b['submitted_at'][:19]} limit={b['limit_price']:>5} "
                  f"filled={b['filled_avg_price'] or '-':>5} at {str(b['filled_at'])[:19]} "
                  f"status={b['status']}")
        for e in exits[occ]:
            print(f"  EXIT-SIGNAL {e['ts'][:19]} {e['kind']} entry={e['entry']} bid={e['bid']}")
        for s in sells[occ]:
            print(f"  SELL submitted {s['submitted_at'][:19]} limit={s['limit_price']:>5} "
                  f"filled={s['filled_avg_price'] or '-':>5} at {str(s['filled_at'])[:19]} "
                  f"status={s['status']}")
        if retries[occ]:
            print(f"  !! {len(retries[occ])} deferred-exit retries (2.0s buffer misses)")

    json.dump({"chain": rows}, open("PAPER_LOSS_POSTMORTEM_20260731_chain.json", "w"),
              indent=2, default=str)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
