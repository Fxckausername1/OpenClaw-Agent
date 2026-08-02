#!/usr/bin/env python3
"""Retrospective ORB side-asymmetry cross-check on cached walk-forward data.

Offline only. The SHORT hypothesis was selected from paper results, so this is not a
pristine locked test; require new forward paper observations before changing live gates.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

import mean_reversion_scanner as mr
import walkforward_search as wf

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "data" / "orb_side_crosscheck.json"


def stats(frame: pd.DataFrame, region: set[str]) -> dict:
    x = frame[frame["date"].isin(region)].copy()
    r = pd.to_numeric(x["net_r"], errors="coerce").dropna()
    if r.empty:
        return {"n": 0}
    daily = x.assign(net_r=pd.to_numeric(x["net_r"], errors="coerce")).groupby("date")["net_r"].sum()
    wins, losses = r[r > 0], r[r < 0]
    return {
        "n": int(len(r)),
        "days": int(x["date"].nunique()),
        "total_r": round(float(r.sum()), 4),
        "per_trade_r": round(float(r.mean()), 4),
        "cap2_per_trade_r": round(float(r.clip(-2, 2).mean()), 4),
        "median_r": round(float(r.median()), 4),
        "win_rate": round(float((r > 0).mean()), 4),
        "profit_factor": round(float(wins.sum() / abs(losses.sum())), 4) if len(losses) else None,
        "positive_day_rate": round(float((daily > 0).mean()), 4),
        "daily_sharpe": round(float(daily.mean() / daily.std() * np.sqrt(252)), 4)
        if len(daily) > 1 and daily.std() > 0 else 0.0,
    }


def portfolio_stats(frames: list[pd.DataFrame], region: set[str]) -> dict:
    return stats(pd.concat(frames, ignore_index=True), region)


def main() -> None:
    syms = wf.wide_universe.load_universe(rebuild_if_stale=False) or mr.fetch_sp100()
    cached = wf.load_cached(syms)
    if not cached:
        raise SystemExit("no cached bars available")

    mr_comp = wf.comp_mr("mr_live", z=1.5, vdev=0.015, min_rr=1.5)
    orb_comp = wf.comp_orbsec(
        "orb_live", max_range_frac=0.0066, vol_mult=1.5, use_vwap=True, use_vol=True
    )
    mr_trades = wf.generate_component(mr_comp, cached)
    orb_trades = wf.generate_component(orb_comp, cached)
    all_dates = sorted(set(mr_trades["date"]) | set(orb_trades["date"]))
    search, holdout = wf.date_split(all_dates)

    orb_short = orb_trades[orb_trades["side"].eq("SHORT")].copy()
    orb_long = orb_trades[orb_trades["side"].eq("LONG")].copy()
    mr_short = mr_trades[mr_trades["side"].eq("SHORT")].copy()
    mr_long = mr_trades[mr_trades["side"].eq("LONG")].copy()

    result = {
        "warning": (
            "Retrospective cross-check: SHORT was selected after viewing paper results. "
            "Do not promote without fresh forward paper data."
        ),
        "universe_n": len(syms),
        "cached_n": len(cached),
        "date_range": [all_dates[0], all_dates[-1]],
        "search_days": len(search),
        "holdout_days": len(holdout),
        "standalone": {
            "ORB_ALL": {"search": stats(orb_trades, search), "holdout": stats(orb_trades, holdout)},
            "ORB_SHORT": {"search": stats(orb_short, search), "holdout": stats(orb_short, holdout)},
            "ORB_LONG": {"search": stats(orb_long, search), "holdout": stats(orb_long, holdout)},
            "MR_ALL": {"search": stats(mr_trades, search), "holdout": stats(mr_trades, holdout)},
            "MR_SHORT": {"search": stats(mr_short, search), "holdout": stats(mr_short, holdout)},
            "MR_LONG": {"search": stats(mr_long, search), "holdout": stats(mr_long, holdout)},
        },
        "portfolio": {
            "MR_PLUS_ORB_ALL": {
                "search": portfolio_stats([mr_trades, orb_trades], search),
                "holdout": portfolio_stats([mr_trades, orb_trades], holdout),
            },
            "MR_PLUS_ORB_SHORT": {
                "search": portfolio_stats([mr_trades, orb_short], search),
                "holdout": portfolio_stats([mr_trades, orb_short], holdout),
            },
            "MR_SHORT_PLUS_ORB_ALL": {
                "search": portfolio_stats([mr_short, orb_trades], search),
                "holdout": portfolio_stats([mr_short, orb_trades], holdout),
            },
        },
        "quarters": [],
    }

    for label, dates in zip(("Q1", "Q2", "Q3", "Q4"), np.array_split(np.asarray(all_dates), 4)):
        region = set(dates.tolist())
        result["quarters"].append({
            "quarter": label,
            "dates": [min(region), max(region)],
            "ORB_SHORT": stats(orb_short, region),
            "ORB_LONG": stats(orb_long, region),
        })

    s_short = result["standalone"]["ORB_SHORT"]["search"]["cap2_per_trade_r"]
    h_short = result["standalone"]["ORB_SHORT"]["holdout"]["cap2_per_trade_r"]
    s_long = result["standalone"]["ORB_LONG"]["search"]["cap2_per_trade_r"]
    h_long = result["standalone"]["ORB_LONG"]["holdout"]["cap2_per_trade_r"]
    result["verdict"] = {
        "short_positive_both_regions": bool(s_short > 0 and h_short > 0),
        "short_beats_long_both_regions": bool(s_short > s_long and h_short > h_long),
        "summary": (
            "Historical cache supports paper-observed SHORT asymmetry"
            if s_short > 0 and h_short > 0 and s_short > s_long and h_short > h_long
            else "Historical cache does not robustly support the paper-observed SHORT asymmetry"
        ),
    }

    OUT.write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
