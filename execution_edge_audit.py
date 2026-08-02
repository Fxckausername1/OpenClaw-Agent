#!/usr/bin/env python3
"""Offline strategy/side breakdown of the latest Alpaca reconciliation snapshot."""
import json
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
OUT = DATA / "execution_edge_audit_latest.json"


def metric(frame, col):
    r = pd.to_numeric(frame[col], errors="coerce").dropna()
    if r.empty:
        return {"n": 0}
    wins, losses = r[r > 0], r[r < 0]
    return {
        "n": int(len(r)),
        "total_r": round(float(r.sum()), 4),
        "mean_r": round(float(r.mean()), 4),
        "cap2_mean_r": round(float(r.clip(-2, 2).mean()), 4),
        "median_r": round(float(r.median()), 4),
        "win_rate": round(float((r > 0).mean()), 4),
        "profit_factor": round(float(wins.sum() / abs(losses.sum())), 4) if len(losses) else None,
    }


def main():
    snap = json.loads((DATA / "alpaca_recon_snapshot.json").read_text())
    rows = pd.DataFrame(snap["rows"])
    mr = pd.read_csv(DATA / "paper_trades.csv")[["trade_id", "ticker", "side", "entry_time"]]
    orb = pd.read_csv(DATA / "orb_paper_trades.csv")[["trade_id", "ticker", "side", "entry_time"]]
    meta = pd.concat([mr.assign(leg="MR"), orb.assign(leg="ORB")], ignore_index=True)
    rows = rows.merge(meta, left_on="tid", right_on="trade_id", how="left")
    rows["leg"] = rows["leg"].fillna(rows["tid"].str.startswith("ORB:").map({True: "ORB", False: "MR"}))
    closed = rows[rows["open"].eq(False) & rows["real_r"].notna()].copy()

    groups = []
    for keys, part in closed.groupby(["leg", "side"], dropna=False):
        leg, side = keys
        groups.append({
            "leg": leg,
            "side": side,
            "actual": metric(part, "real_r"),
            "simulated_same_trades": metric(part, "sim_r"),
            "avg_entry_slip_bps": round(float(pd.to_numeric(part["entry_slip_bps"], errors="coerce").mean()), 3),
        })
    groups.sort(key=lambda x: x["actual"].get("cap2_mean_r", -999), reverse=True)
    result = {
        "generated_at": snap["generated_at"],
        "aggregate": snap["aggregate"],
        "groups": groups,
        "warning": "Small selected execution cohort; do not infer production expectancy without more forward trades.",
    }
    OUT.write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
