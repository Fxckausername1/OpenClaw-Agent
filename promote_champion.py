#!/usr/bin/env python3
"""promote_champion.py - the last-mile wire from continuous_search.py's holdout-validated
champion to the actually-live mean_reversion_scanner.py / orb_scanner.py parameters.

continuous_search.py already enforces the only gate that matters (a candidate becomes
champion ONLY if it beats the current champion on search AND carries on the locked 75/25
holdout) -- this script does not re-validate anything, it just notices when the champion
changed and pushes it into data/live_params.json, which both live scanners read at startup.
Without this, a holdout-confirmed improvement just sits in continuous_champion.json forever
until someone notices and hand-edits the live scanner (the gap heff flagged 2026-06-28).

Idempotent: no-ops if the champion hasn't changed since the last promotion. Keeps a full
promotion history (data/promotion_history.jsonl) so any promotion can be traced/rolled back.

Only promotes the dimensions continuous_search.py's grid actually varies (mr: z/vdev/min_rr;
orb: vol_mult/max_range_frac/use_vwap/use_vol, plus use_sector_gate -- derived from the
champion's "base" field, "orb" vs "orb_sector", 2026-07-05) -- NOT max_price, which is a
deliberate strategy-level cap (ORB's $250 cap specifically does not scale with account size,
per mean-reversion-strategy discipline) that is never part of the search grid, so it can
never be silently changed here. If a key isn't present in the champion's params dict (e.g. the
baseline orb_cap component predates continuous_search.py and has no max_range_frac key at
all), that key is left untouched in live_params.json rather than overwritten/cleared.

Usage: ./venv/bin/python promote_champion.py
"""
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CHAMP_PATH = ROOT / "data" / "continuous_champion.json"
LIVE_PARAMS_PATH = ROOT / "data" / "live_params.json"
HISTORY_PATH = ROOT / "data" / "promotion_history.jsonl"

MR_KEYS = ("z", "vdev", "min_rr")
ORB_KEYS = ("vol_mult", "max_range_frac", "use_vwap", "use_vol")


def tg(msg):
    try:
        subprocess.run(["openclaw", "message", "send", "--channel", "telegram",
                        "--target", "7590346809", "--message", msg], timeout=30, check=False)
    except Exception:
        pass


def main():
    if not CHAMP_PATH.exists():
        print("no continuous_champion.json yet -- nothing to promote")
        return

    champ = json.loads(CHAMP_PATH.read_text())
    mr_p = champ.get("mr", {}).get("p", {})
    orb_p = champ.get("orb", {}).get("p", {})
    new_mr = {k: mr_p[k] for k in MR_KEYS if k in mr_p}
    new_orb = {k: orb_p[k] for k in ORB_KEYS if k in orb_p}
    # SECTOR-GATE AXIS (2026-07-05): champ["orb"]["base"] is "orb" or "orb_sector" (comp_orb vs
    # comp_orbsec -- see continuous_search.py's build_grid()). ORB_KEYS above only ever carried
    # the 4 numeric params -- a promoted orb_sector champion would silently leave live's
    # use_sector_gate flag stale since nothing here read "base" at all. Carry it explicitly.
    orb_base = champ.get("orb", {}).get("base")
    if orb_base in ("orb", "orb_sector"):
        new_orb["use_sector_gate"] = (orb_base == "orb_sector")

    if LIVE_PARAMS_PATH.exists():
        live = json.loads(LIVE_PARAMS_PATH.read_text())
    else:
        live = {"mr": {}, "orb": {}}

    changed_mr = {k: v for k, v in new_mr.items() if live.get("mr", {}).get(k) != v}
    changed_orb = {k: v for k, v in new_orb.items() if live.get("orb", {}).get(k) != v}

    if not changed_mr and not changed_orb:
        print("champion unchanged -- nothing to promote")
        return

    live["mr"] = {**live.get("mr", {}), **new_mr}
    live["orb"] = {**live.get("orb", {}), **new_orb}
    live["promoted_at"] = datetime.now(timezone.utc).isoformat()
    live["source"] = (f"continuous_search champion: "
                      f"mr={champ.get('mr', {}).get('name')} orb={champ.get('orb', {}).get('name')}")
    LIVE_PARAMS_PATH.write_text(json.dumps(live, indent=2))

    record = {"ts": live["promoted_at"], "changed_mr": changed_mr, "changed_orb": changed_orb,
              "mr_name": champ.get("mr", {}).get("name"), "orb_name": champ.get("orb", {}).get("name"),
              "search": champ.get("search"), "holdout": champ.get("holdout")}
    with HISTORY_PATH.open("a") as f:
        f.write(json.dumps(record) + "\n")

    lines = ["PROMOTED to live (holdout-confirmed):"]
    if changed_mr:
        lines.append(f"  mean-rev: {changed_mr}")
    if changed_orb:
        lines.append(f"  ORB: {changed_orb}")
    h = champ.get("holdout") or {}
    if h:
        lines.append(f"  holdout: {h.get('total_r', 0):+.1f}R | {h.get('n', 0)} tr | "
                      f"{h.get('per_trade', 0):+.3f}R/tr | Sharpe {h.get('sharpe', 0):.2f}")
    msg = "\n".join(lines)
    print(msg)
    tg(msg)


if __name__ == "__main__":
    main()
