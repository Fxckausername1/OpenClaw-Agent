"""Atomic microstructure_snapshot.json writer -- TD-STD Section 3.

Isolation rule, enforced by construction: this module's only write target
is SNAPSHOT_PATH (data/thetadata/microstructure_snapshot.json) plus its own
small CVD-continuity state files under data/thetadata/. It never opens
live_gex_snapshot.json, vex_history.json, or any other production file for
writing -- those are read-only inputs consumed inside features.py.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import os
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from . import SCHEMA_VERSION
from . import aggregate as agg
from . import features as feat
from .contracts import get_spot_price

ET = ZoneInfo("America/New_York")
ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "thetadata"
DATA.mkdir(parents=True, exist_ok=True)
SNAPSHOT_PATH = DATA / "microstructure_snapshot.json"


def sanitize_for_json(obj: Any) -> Any:
    """Recursively convert numpy/pandas scalar types to native Python and
    NaN/Inf to None, so the snapshot is valid, portable JSON rather than
    relying on Python json's permissive (non-standard) NaN handling."""
    if isinstance(obj, dict):
        return {k: sanitize_for_json(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [sanitize_for_json(v) for v in obj]
    if isinstance(obj, (np.generic,)):
        obj = obj.item()
    if isinstance(obj, float):
        return None if (math.isnan(obj) or math.isinf(obj)) else obj
    if isinstance(obj, (pd.Timestamp, dt.datetime, dt.date)):
        return obj.isoformat()
    if isinstance(obj, (np.bool_, bool)):
        return bool(obj)
    return obj


def _atomic_write_json(path: Path, payload: dict) -> None:
    tmp = path.with_suffix(f".tmp{os.getpid()}")
    tmp.write_text(json.dumps(sanitize_for_json(payload), indent=2))
    os.replace(tmp, path)


def _cvd_state_path(symbol: str, today: dt.date) -> Path:
    return DATA / f"cvd_state_{symbol}_{today.isoformat()}.json"


def _load_cvd_state(symbol: str, today: dt.date) -> dict:
    path = _cvd_state_path(symbol, today)
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            pass
    return {"cumulative_cvd": 0.0, "prev_spot": None}


def _save_cvd_state(symbol: str, today: dt.date, state: dict) -> None:
    tmp = _cvd_state_path(symbol, today).with_suffix(f".tmp{os.getpid()}")
    tmp.write_text(json.dumps(sanitize_for_json(state)))
    os.replace(tmp, _cvd_state_path(symbol, today))


def _iv_state_path(symbol: str, today: dt.date) -> Path:
    return DATA / f"iv_state_{symbol}_{today.isoformat()}.json"


def _load_iv_state(symbol: str, today: dt.date) -> dict:
    """Prior cycle's atm_iv_by_expiration -- same persisted-state pattern as
    _load_cvd_state, needed for iv_skew_features()'s atm_iv_change."""
    path = _iv_state_path(symbol, today)
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            pass
    return {}


def _save_iv_state(symbol: str, today: dt.date, atm_iv_by_expiration: dict) -> None:
    tmp = _iv_state_path(symbol, today).with_suffix(f".tmp{os.getpid()}")
    tmp.write_text(json.dumps(sanitize_for_json(atm_iv_by_expiration)))
    os.replace(tmp, _iv_state_path(symbol, today))


def build_symbol_snapshot(symbol: str, cycle_result: dict, today: dt.date) -> dict:
    classified = cycle_result["classified_trades"]  # this cycle's small batch -- for minute_bars/CVD only
    cumulative_stats = cycle_result.get("cumulative_stats", {})  # session-to-date -- for V/OI/wall state
    universe = cycle_result["universe"]
    oi_map = cycle_result["oi"]
    greeks_map = cycle_result.get("greeks", {})  # per-contract {delta, implied_vol, strike, right, expiration}
    delta_map = {cid: g["delta"] for cid, g in greeks_map.items() if g.get("delta") is not None}
    health = cycle_result["health"]

    wall_df = agg.wall_aggregates_from_stats(cumulative_stats, oi_map)
    if not wall_df.empty:
        wall_df["wall_state"] = wall_df.apply(agg.classify_wall_state, axis=1)

    pc = feat.full_pc(symbol, wall_df)
    ghost = feat.ghost_wall_summary(wall_df)

    minute_df = agg.minute_bars(classified) if not classified.empty else pd.DataFrame()
    contract_meta = {c["contract_id"]: {"right": c["right"]} for c in universe.get("contracts", [])}
    spot = get_spot_price(symbol) or universe.get("spot")
    cvd = feat.options_cvd(minute_df, contract_meta, delta_map, spot)

    cvd_state = _load_cvd_state(symbol, today)
    this_cycle_flow = cvd.get("delta_notional_flow") or 0.0
    cumulative = cvd_state.get("cumulative_cvd", 0.0) + this_cycle_flow
    price_change = (spot - cvd_state["prev_spot"]) if (spot is not None and cvd_state.get("prev_spot") is not None) else None
    divergence = feat.divergence_flags(price_change, this_cycle_flow)
    _save_cvd_state(symbol, today, {"cumulative_cvd": cumulative, "prev_spot": spot})
    cvd["options_cvd"] = cumulative

    prior_atm_iv = _load_iv_state(symbol, today)
    iv_skew = feat.iv_skew_features(greeks_map, spot, prior_atm_iv)
    _save_iv_state(symbol, today, iv_skew.get("atm_iv_by_expiration", {}))

    live_gex_row = feat.load_live_gex_row(symbol)  # read-only, same production file full_pc() already reads
    control_map = feat.control_map_verdict(live_gex_row, ghost["walls"], cvd, iv_skew)

    return {
        "spot": spot,
        "as_of": dt.datetime.now(ET).isoformat(),
        "source_health": health,
        "walls": ghost["walls"],
        "ghost_wall": ghost["ghost_wall"],
        "p_c": pc,
        "options_cvd": cvd,
        "divergence_flags": divergence,
        "iv_skew": iv_skew,
        "control_map": control_map,
        "subscription_set": {
            "expirations": universe.get("expirations", []),
            "contract_count": universe.get("contract_count", 0),
        },
    }


def write_snapshot(cycle: dict) -> dict:
    """cycle is collector.run_cycle()'s return value. Writes SNAPSHOT_PATH
    atomically and returns the payload written (for callers/tests that want
    to inspect it without a re-read)."""
    if cycle.get("skipped"):
        return {"skipped": cycle["skipped"], "generated_at": cycle.get("now")}

    now = dt.datetime.fromisoformat(cycle["now"])
    today = now.date()
    symbols_out = {}
    for symbol, result in cycle["results"].items():
        symbols_out[symbol] = build_symbol_snapshot(symbol, result, today)

    payload = {
        "generated_at": now.isoformat(),
        "schema_version": SCHEMA_VERSION,
        "provider": "thetadata",
        "symbols": symbols_out,
    }
    _atomic_write_json(SNAPSHOT_PATH, payload)
    return payload


def load_snapshot() -> dict | None:
    if not SNAPSHOT_PATH.exists():
        return None
    try:
        return json.loads(SNAPSHOT_PATH.read_text())
    except Exception:
        return None
