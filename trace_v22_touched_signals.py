"""One-off (not part of the reusable module): joins the v2.1-vs-v2.2 event
diff (30 truly-new + 6 reclassified + 5 removed SWEEP_RECLAIM/PULLBACK/
MA_FADE events -- see /tmp/v22_diff_detail.json) against the instrumented
per_signal.json produced by run_selector_audit.py, to trace each individually:
timestamp, trigger, candidate contracts checked, quote provenance/age, every
selector rule applied, and the exact terminal rejection reason.
"""
import json
import sys

RUN_DIR = sys.argv[1] if len(sys.argv) > 1 else None
if not RUN_DIR:
    print("usage: trace_v22_touched_signals.py <run_dir>")
    sys.exit(1)

per_signal = json.load(open(f"{RUN_DIR}/per_signal.json"))
by_ctx = {r["context_snapshot_id"]: r for r in per_signal}

diff = json.load(open("/tmp/v22_diff_detail.json"))

def ctx_id(e, symbol="QQQ"):
    return f"B1-HEFF-SMC:{symbol}:{e['session']}:{e['bar_index']}:{e['trigger']}"

out = {"truly_new": [], "reclassified": [], "truly_removed_not_in_v22": []}

for e in diff["truly_new"]:
    cid = ctx_id(e)
    rec = by_ctx.get(cid)
    out["truly_new"].append({"event": e, "context_snapshot_id": cid, "outcome_record": rec})

for pair in diff["reclassified"]:
    e_new = pair["v22_event"]
    cid = ctx_id(e_new)
    rec = by_ctx.get(cid)
    out["reclassified"].append({
        "old_trigger": pair["old_trigger"], "new_trigger": pair["new_trigger"],
        "event": e_new, "context_snapshot_id": cid, "outcome_record": rec,
    })

for e in diff["truly_removed"]:
    out["truly_removed_not_in_v22"].append({"event": e, "note": "no longer fires in v2.2 at all -- not part of the 1804-signal set, no selector record exists"})

missing_lookups = sum(1 for grp in ("truly_new", "reclassified") for item in out[grp] if item["outcome_record"] is None)
summary = {
    "n_truly_new": len(out["truly_new"]),
    "n_reclassified": len(out["reclassified"]),
    "n_truly_removed": len(out["truly_removed_not_in_v22"]),
    "n_lookup_misses": missing_lookups,
}
print(json.dumps(summary, indent=2))
json.dump(out, open(f"{RUN_DIR}/v22_touched_signals_traced.json", "w"), indent=2, default=str)
print(f"wrote {RUN_DIR}/v22_touched_signals_traced.json")
