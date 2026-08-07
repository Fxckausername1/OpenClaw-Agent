#!/usr/bin/env python3
"""Run the canonical frozen-live MR/ORB contracts over cached historical bars."""

from __future__ import annotations

import argparse
import glob
import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from canonical_mr_contract import detect_mr
from canonical_strategy_contracts import CONTRACT_VERSION, detect_orb, simulate_boundary_limit


ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
CACHE = DATA / "wf_cache"
OUT_DIR = DATA / "research" / "canonical_replay"


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    temp.write_text(text)
    os.chmod(temp, 0o600)
    os.replace(temp, path)


def clean(value):
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    if hasattr(value, "item"):
        value = value.item()
    return value


def bars_from_frame(frame: pd.DataFrame) -> list[dict]:
    bars = []
    for ts, row in frame.iterrows():
        item = {str(k): clean(v) for k, v in row.to_dict().items()}
        item["time"] = pd.Timestamp(ts).to_pydatetime().replace(tzinfo=None)
        bars.append(item)
    return bars


def load_params() -> tuple[dict, dict]:
    raw = json.loads((DATA / "live_params.json").read_text())
    portfolio = raw.get("portfolio", {})
    capital = float(portfolio.get("total_capital", 1000.0))
    slots = int(portfolio.get("num_slots", 4))
    mr_raw = raw.get("mr", {})
    orb_raw = raw.get("orb", {})
    mr = {
        "z": float(mr_raw.get("z", 1.5)),
        "vdev": float(mr_raw.get("vdev", 0.015)),
        "min_rr": float(mr_raw.get("min_rr", 1.5)),
        "max_price": capital / slots,
    }
    orb = {
        "vol_mult": float(orb_raw.get("vol_mult", 1.5)),
        "max_range_frac": float(orb_raw.get("max_range_frac", 0.0066)),
        "use_vwap": bool(orb_raw.get("use_vwap", True)),
        "use_vol": bool(orb_raw.get("use_vol", True)),
        "use_sector_gate": bool(orb_raw.get("use_sector_gate", True)),
    }
    return mr, orb


def load_earnings() -> dict[str, set[str]]:
    path = DATA / "bt_earnings.json"
    if not path.exists():
        return {}
    raw = json.loads(path.read_text())
    source = raw.get("tickers", raw) if isinstance(raw, dict) else {}
    return {str(symbol): {str(d) for d in dates} for symbol, dates in source.items() if isinstance(dates, list)}


class SectorHistory:
    def __init__(self):
        map_path = DATA / "sector_map.json"
        rot_path = DATA / "sector_rotation.csv"
        self.mapping = json.loads(map_path.read_text()) if map_path.exists() else {}
        self.by_date = {}
        self.dates = []
        if rot_path.exists():
            frame = pd.read_csv(rot_path, dtype={"date": str})
            self.by_date = {
                str(day): dict(zip(group["sector_etf"], group["quadrant"]))
                for day, group in frame.groupby("date")
            }
            self.dates = sorted(self.by_date)

    def hot_before(self, symbol: str, day: str) -> bool | None:
        etf = self.mapping.get(symbol)
        prior = [d for d in self.dates if d < day]
        if not etf or not prior:
            return None
        quadrant = self.by_date.get(prior[-1], {}).get(etf)
        if quadrant is None:
            return None
        return quadrant in {"Leading", "Improving"}


def summarize(rows: list[dict], strategy: str, cost_bps: list[int]) -> dict:
    group = [r for r in rows if r["strategy"] == strategy]
    filled = [r for r in group if r["fill_state"] == "filled_closed"]
    result = {
        "intents": len(group),
        "unique_days": len({r["date"] for r in group}),
        "filled": len(filled),
        "never_filled": sum(r["fill_state"] == "never_filled" for r in group),
        "fill_rate": len(filled) / len(group) if group else None,
        "gross_filled_avg_r": sum(r["outcome_r"] for r in filled) / len(filled) if filled else None,
        "gross_intent_avg_r": sum(r["outcome_r"] for r in group) / len(group) if group else None,
        "cost_scenarios": {},
    }
    for bps in cost_bps:
        values = [r[f"net_r_{bps}bp"] for r in group]
        result["cost_scenarios"][str(bps)] = {
            "intent_avg_r": sum(values) / len(values) if values else None,
            "total_r": sum(values),
            "positive": sum(v > 0 for v in values) / len(values) if values else None,
        }
    return result


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--symbols", help="comma-separated subset")
    ap.add_argument("--max-symbols", type=int)
    ap.add_argument("--start")
    ap.add_argument("--end")
    ap.add_argument("--strategy", choices=["MR", "ORB", "both"], default="both")
    ap.add_argument("--cost-bps", default="6,12,20,30")
    args = ap.parse_args()

    wanted = {s.strip().upper() for s in args.symbols.split(",")} if args.symbols else None
    files = sorted(Path(p) for p in glob.glob(str(CACHE / "*.parquet")))
    if wanted is not None:
        files = [p for p in files if p.stem.upper() in wanted]
    if args.max_symbols:
        files = files[: args.max_symbols]
    if not files:
        raise SystemExit("no cached symbols selected")

    mr_params, orb_params = load_params()
    earnings = load_earnings()
    sectors = SectorHistory()
    costs = [int(x) for x in args.cost_bps.split(",") if x.strip()]
    rows = []
    errors = []

    for path in files:
        symbol = path.stem.upper()
        try:
            frame = pd.read_parquet(path).sort_index()
        except Exception as exc:
            errors.append({"symbol": symbol, "error": str(exc)})
            continue
        if args.start:
            frame = frame[frame.index >= pd.Timestamp(args.start)]
        if args.end:
            frame = frame[frame.index < pd.Timestamp(args.end) + pd.Timedelta(days=1)]
        for day, daily in frame.groupby(frame.index.date, sort=True):
            day_s = str(day)
            if day_s in earnings.get(symbol, set()):
                continue
            bars = bars_from_frame(daily)
            intents = []
            if args.strategy in {"MR", "both"}:
                intents.extend(detect_mr(bars, mr_params))
            if args.strategy in {"ORB", "both"}:
                hot = sectors.hot_before(symbol, day_s)
                intents.extend(detect_orb(bars, orb_params, sector_hot=hot))
            for intent in intents:
                outcome = simulate_boundary_limit(
                    bars,
                    intent["signal_index"],
                    intent["side"],
                    intent["entry"],
                    intent["stop"],
                    intent.get("target"),
                )
                row = {**intent, **outcome, "symbol": symbol, "date": day_s}
                risk_frac = abs(float(intent["entry"]) - float(intent["stop"])) / float(intent["entry"])
                row["risk_frac"] = risk_frac
                for bps in costs:
                    friction_r = (bps / 10000.0) / max(risk_frac, 1e-9) if outcome["fill_state"] == "filled_closed" else 0.0
                    row[f"net_r_{bps}bp"] = float(outcome["outcome_r"]) - friction_r
                rows.append(row)

    created = datetime.now(timezone.utc)
    run_id = f"canonical_{created.strftime('%Y%m%dT%H%M%SZ')}"
    report = {
        "run_id": run_id,
        "created_at": created.isoformat(timespec="seconds"),
        "contract_version": CONTRACT_VERSION,
        "mode": "offline_boundary_limit_intent_level",
        "symbols_requested": len(files),
        "symbols_failed": len(errors),
        "date_filter": {"start": args.start, "end": args.end},
        "parameters": {"MR": mr_params, "ORB": orb_params},
        "cost_bps": costs,
        "results": {
            "MR": summarize(rows, "MR", costs),
            "ORB": summarize(rows, "ORB", costs),
        },
        "errors": errors[:100],
        "limitations": [
            "current cached/static universe, not point-in-time membership",
            "five-minute OHLCV uses pessimistic stop-first same-bar ordering",
            "boundary-limit fills use touch-at-limit and do not model quote queue position",
            "signal-level results do not yet apply portfolio admission/replacement overlay",
        ],
    }
    payload = "".join(json.dumps(r, sort_keys=True, default=str, separators=(",", ":")) + "\n" for r in rows)
    atomic_write(OUT_DIR / f"{run_id}.trades.jsonl", payload)
    atomic_write(OUT_DIR / f"{run_id}.summary.json", json.dumps(report, indent=2, sort_keys=True) + "\n")
    atomic_write(OUT_DIR / "latest.trades.jsonl", payload)
    atomic_write(OUT_DIR / "latest.summary.json", json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
