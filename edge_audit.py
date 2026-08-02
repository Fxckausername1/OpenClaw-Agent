#!/usr/bin/env python3
"""Leakage-resistant audit of actual MR/ORB paper trades.

Offline only: reads local CSV/JSONL files, never calls Alpaca or market-data APIs.
Candidate filters are exploratory and must not be promoted without new forward data.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
OUT_JSON = DATA / "edge_audit_latest.json"
OUT_MD = DATA / "edge_audit_latest.md"
RNG = np.random.default_rng(20260711)


def load_triggers() -> pd.DataFrame:
    rows: list[dict] = []
    for pattern in ("mr_triggers_*.jsonl", "orb_triggers_*.jsonl"):
        for path in sorted(DATA.glob(pattern)):
            for line in path.read_text(errors="ignore").splitlines():
                try:
                    row = json.loads(line)
                    row["_trigger_file"] = path.name
                    rows.append(row)
                except (json.JSONDecodeError, TypeError):
                    continue
    if not rows:
        return pd.DataFrame(columns=["trade_id"])
    out = pd.DataFrame(rows)
    return out.dropna(subset=["trade_id"]).drop_duplicates("trade_id", keep="last")


def load_trades() -> pd.DataFrame:
    frames = []
    for leg, filename in (("MR", "paper_trades.csv"), ("ORB", "orb_paper_trades.csv")):
        path = DATA / filename
        frame = pd.read_csv(path)
        frame["leg"] = leg
        frames.append(frame)
    trades = pd.concat(frames, ignore_index=True, sort=False)
    trades["outcome_r"] = pd.to_numeric(trades["outcome_r"], errors="coerce")
    trades["dollar_pnl"] = pd.to_numeric(trades.get("dollar_pnl"), errors="coerce")
    trades = trades[np.isfinite(trades["outcome_r"])].copy()
    trades["entry_dt"] = pd.to_datetime(trades["entry_time"], errors="coerce")
    trades["date"] = trades["entry_dt"].dt.date.astype(str)
    trades["hour"] = trades["entry_dt"].dt.hour
    trades["minute"] = trades["entry_dt"].dt.minute
    mins = trades["hour"] * 60 + trades["minute"]
    trades["time_bucket"] = pd.cut(
        mins,
        bins=[0, 629, 689, 809, 970, 1440],
        labels=["pre-10:30", "10:30-11:29", "11:30-13:29", "13:30-16:10", "other"],
    ).astype(str)

    triggers = load_triggers()
    keep = [
        "trade_id", "universe", "sector_hot", "sector_quadrant", "sector_etf",
        "z", "rsi", "vwap_dev", "relvol", "rng", "vwap", "lag_min", "detected_at",
    ]
    if not triggers.empty:
        available = [c for c in keep if c in triggers.columns]
        trades = trades.merge(triggers[available], on="trade_id", how="left", suffixes=("", "_trigger"))

    for col in ("z", "rsi", "vwap_dev", "relvol", "rng", "vwap", "lag_min", "entry"):
        if col in trades:
            trades[col] = pd.to_numeric(trades[col], errors="coerce")
    if "vwap" in trades:
        trades["orb_vwap_distance"] = (trades["entry"] / trades["vwap"] - 1).abs()
    return trades.sort_values(["entry_dt", "trade_id"]).reset_index(drop=True)


def block_bootstrap_ci(frame: pd.DataFrame, cap: float = 2.0, n_boot: int = 1500) -> list[float]:
    daily = frame.assign(r=frame["outcome_r"].clip(-cap, cap)).groupby("date")["r"].agg(list)
    days = list(daily.index)
    if len(days) < 3:
        return [None, None]
    means = np.empty(n_boot)
    for i in range(n_boot):
        sampled = RNG.choice(days, size=len(days), replace=True)
        vals = np.concatenate([np.asarray(daily[d], dtype=float) for d in sampled])
        means[i] = vals.mean()
    return [float(np.quantile(means, 0.025)), float(np.quantile(means, 0.975))]


def metrics(frame: pd.DataFrame) -> dict:
    r = frame["outcome_r"].dropna().astype(float)
    if r.empty:
        return {"n": 0}
    wins = r[r > 0]
    losses = r[r < 0]
    daily = frame.assign(r=frame["outcome_r"]).groupby("date")["r"].sum()
    equity = r.cumsum()
    dd = equity - equity.cummax()
    gross_profit = float(wins.sum())
    top = float(wins.nlargest(min(5, len(wins))).sum()) if len(wins) else 0.0
    return {
        "n": int(len(r)),
        "days": int(frame["date"].nunique()),
        "total_r": round(float(r.sum()), 4),
        "mean_r": round(float(r.mean()), 4),
        "median_r": round(float(r.median()), 4),
        "cap2_mean_r": round(float(r.clip(-2, 2).mean()), 4),
        "cap3_mean_r": round(float(r.clip(-3, 3).mean()), 4),
        "win_rate": round(float((r > 0).mean()), 4),
        "profit_factor": round(gross_profit / abs(float(losses.sum())), 4) if len(losses) else None,
        "positive_day_rate": round(float((daily > 0).mean()), 4),
        "max_drawdown_r": round(float(dd.min()), 4),
        "top5_profit_share": round(top / gross_profit, 4) if gross_profit > 0 else None,
        "day_bootstrap_cap2_ci95": [round(x, 4) if x is not None else None for x in block_bootstrap_ci(frame)],
    }


def grouped(frame: pd.DataFrame, columns: list[str], min_n: int = 5) -> list[dict]:
    out = []
    for key, part in frame.groupby(columns, dropna=False, observed=True):
        if len(part) < min_n:
            continue
        keys = key if isinstance(key, tuple) else (key,)
        row = {col: (None if pd.isna(val) else str(val)) for col, val in zip(columns, keys)}
        row.update(metrics(part))
        out.append(row)
    return sorted(out, key=lambda x: (x.get("cap2_mean_r", -999), x["n"]), reverse=True)


def chronology(frame: pd.DataFrame) -> dict:
    dates = sorted(frame["date"].dropna().unique())
    cut = max(1, int(len(dates) * 0.70))
    search_dates, holdout_dates = set(dates[:cut]), set(dates[cut:])
    return {
        "search_dates": sorted(search_dates),
        "holdout_dates": sorted(holdout_dates),
        "search": metrics(frame[frame["date"].isin(search_dates)]),
        "holdout": metrics(frame[frame["date"].isin(holdout_dates)]),
    }


def candidate_masks(frame: pd.DataFrame) -> dict[str, pd.Series]:
    masks: dict[str, pd.Series] = {}
    orb = frame["leg"].eq("ORB")
    mr = frame["leg"].eq("MR")
    masks["ORB baseline"] = orb
    masks["ORB sector_hot"] = orb & frame.get("sector_hot", pd.Series(False, index=frame.index)).eq(True)
    masks["ORB lag<=10m"] = orb & frame.get("lag_min", pd.Series(np.nan, index=frame.index)).le(10)
    masks["ORB relvol>=1.5"] = orb & frame.get("relvol", pd.Series(np.nan, index=frame.index)).ge(1.5)
    masks["ORB relvol>=2.0"] = orb & frame.get("relvol", pd.Series(np.nan, index=frame.index)).ge(2.0)
    masks["ORB range<=0.66%"] = orb & frame.get("rng", pd.Series(np.nan, index=frame.index)).le(0.0066)
    masks["ORB VWAP distance>=0.2%"] = orb & frame.get("orb_vwap_distance", pd.Series(np.nan, index=frame.index)).ge(0.002)
    masks["ORB pre-10:30"] = orb & frame["time_bucket"].eq("pre-10:30")
    masks["MR baseline"] = mr
    masks["MR |z|>=2"] = mr & frame.get("z", pd.Series(np.nan, index=frame.index)).abs().ge(2)
    masks["MR |vwap_dev|>=2%"] = mr & frame.get("vwap_dev", pd.Series(np.nan, index=frame.index)).abs().ge(0.02)
    masks["MR RSI extreme"] = mr & (
        frame.get("rsi", pd.Series(np.nan, index=frame.index)).le(25)
        | frame.get("rsi", pd.Series(np.nan, index=frame.index)).ge(75)
    )
    masks["MR sector_hot"] = mr & frame.get("sector_hot", pd.Series(False, index=frame.index)).eq(True)
    masks["MR pre-10:30"] = mr & frame["time_bucket"].eq("pre-10:30")
    masks["MR 10:30-13:29"] = mr & frame["time_bucket"].isin(["10:30-11:29", "11:30-13:29"])
    masks["MR after-13:30"] = mr & frame["time_bucket"].eq("13:30-16:10")
    return masks


def evaluate_candidates(frame: pd.DataFrame) -> list[dict]:
    dates = sorted(frame["date"].dropna().unique())
    cut = max(1, int(len(dates) * 0.70))
    search_dates, holdout_dates = set(dates[:cut]), set(dates[cut:])
    rows = []
    for name, mask in candidate_masks(frame).items():
        part = frame[mask]
        search = part[part["date"].isin(search_dates)]
        holdout = part[part["date"].isin(holdout_dates)]
        rows.append({
            "name": name,
            "all": metrics(part),
            "search": metrics(search),
            "holdout": metrics(holdout),
        })
    return rows


def leave_one_day_out(frame: pd.DataFrame) -> dict:
    vals = []
    for day in sorted(frame["date"].unique()):
        part = frame[frame["date"] != day]
        if len(part):
            vals.append(float(part["outcome_r"].clip(-2, 2).mean()))
    return {
        "min_cap2_mean": round(min(vals), 4) if vals else None,
        "max_cap2_mean": round(max(vals), 4) if vals else None,
        "all_positive": bool(vals and min(vals) > 0),
    }


def render_markdown(result: dict) -> str:
    lines = ["# Paper edge audit", "", "Offline analysis of actual MR/ORB paper trades.", ""]
    lines.append("## Core")
    for row in result["by_leg"]:
        lines.append(
            f"- {row['leg']}: n={row['n']}, mean={row['mean_r']:+.3f}R, "
            f"cap2={row['cap2_mean_r']:+.3f}R, median={row['median_r']:+.3f}R, "
            f"PF={row['profit_factor']}, CI={row['day_bootstrap_cap2_ci95']}"
        )
    lines.extend(["", "## Candidate filters (chronological 70/30 split)"])
    for row in result["candidates"]:
        s, h = row["search"], row["holdout"]
        lines.append(
            f"- {row['name']}: search n={s.get('n',0)} cap2={s.get('cap2_mean_r')}; "
            f"holdout n={h.get('n',0)} cap2={h.get('cap2_mean_r')}"
        )
    return "\n".join(lines) + "\n"


def main() -> None:
    trades = load_trades()
    result = {
        "generated_at": pd.Timestamp.now(tz="America/New_York").isoformat(),
        "warning": "Exploratory paper-trade analysis; idealized paper fills and multiple comparisons can overstate edge.",
        "all": metrics(trades),
        "by_leg": grouped(trades, ["leg"]),
        "by_leg_side": grouped(trades, ["leg", "side"]),
        "by_leg_time": grouped(trades, ["leg", "time_bucket"]),
        "by_leg_universe": grouped(trades, ["leg", "universe"]),
        "by_leg_sector_hot": grouped(trades, ["leg", "sector_hot"]),
        "by_leg_sector_quadrant": grouped(trades, ["leg", "sector_quadrant"]),
        "chronology": {leg: chronology(part) for leg, part in trades.groupby("leg")},
        "leave_one_day_out": {leg: leave_one_day_out(part) for leg, part in trades.groupby("leg")},
        "candidates": evaluate_candidates(trades),
    }
    OUT_JSON.write_text(json.dumps(result, indent=2, allow_nan=False))
    OUT_MD.write_text(render_markdown(result))
    print(render_markdown(result))


if __name__ == "__main__":
    main()
