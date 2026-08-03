"""Ghost Wall, full P(C), and options CVD -- TD-STD Sections 6-8.

Read-only reuse of the existing, already math-audited gex_quant_engine.py
(CascadeBreakdownEngine, VexExtremeTracker, VolatilityTermStructureMonitor)
and the already-published live_gex_snapshot.json / vex_history.json /
vix_vxv_cache.json. Nothing in this module writes to any of those files --
see the package docstring's isolation rule. G/T/V come from the SAME
formulas and SAME default weights (0.2/0.3/0.3/0.2, matching
advanced_gex.py's own CascadeBreakdownEngine() defaults, confirmed by
reading the source) the production engine already uses; the only new
component is F_state, computed for real from ThetaData flow instead of the
hard-pinned 0.0 the live path still carries.
"""

from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path
from typing import Optional

import pandas as pd

from .schemas import (
    CLASS_ASK, CLASS_BID, WALL_GHOST_CANDIDATE, WALL_REINFORCED,
)

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import gex_quant_engine as gqe  # noqa: E402  (path insert must happen first)

LIVE_GEX_SNAPSHOT = ROOT / "data" / "live_gex_snapshot.json"
VEX_HISTORY = ROOT / "data" / "vex_history.json"
VIX_VXV_CACHE = ROOT / "data" / "vix_vxv_cache.json"


def _read_json(path: Path) -> Optional[dict]:
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def load_live_gex_row(symbol: str) -> Optional[dict]:
    """Read-only lookup into the ALREADY-PUBLISHED live_gex_snapshot.json.
    Never opened for writing anywhere in this package."""
    snap = _read_json(LIVE_GEX_SNAPSHOT)
    if not snap:
        return None
    for row in snap.get("results", []):
        if row.get("ticker") == symbol:
            return row
    return None


def load_vex_history_copy(symbol: str) -> list[float]:
    """Read-only copy of the persisted rolling-max |VEX| history. A copy,
    not a reference -- feeding it into a fresh VexExtremeTracker() below
    mutates only that local, throwaway instance, never the file on disk."""
    hist = _read_json(VEX_HISTORY) or {}
    values = hist.get(symbol, [])
    return list(values) if isinstance(values, list) else []


def load_vix_vxv_ratio() -> Optional[float]:
    cache = _read_json(VIX_VXV_CACHE) or {}
    return cache.get("ratio")


def aggregate_flow_state(wall_df: pd.DataFrame) -> tuple[float, bool]:
    """Book-level V/OI and bid-dominance for CascadeBreakdownEngine.f_state,
    aggregated across every contract currently in the active universe for
    one symbol (not a single strike) -- P(C) is meant to read overall
    dealer-book destabilization, matching how G/T/V are also whole-book
    reads, not per-strike ones."""
    if wall_df.empty:
        return 0.0, False
    established = wall_df[wall_df["established_oi"]]
    if established.empty:
        return 0.0, False
    total_volume = established["intraday_volume"].sum()
    total_oi = established["oi"].sum()
    v_oi = (total_volume / total_oi) if total_oi else 0.0
    # bid-dominance from aggregate classified bid vs ask volume, derived from
    # each row's own bid_fraction * classified_volume (weighted, not a naive
    # mean of fractions).
    weighted_bid = (established["bid_fraction"].fillna(0) * established["classified_volume"]).sum()
    total_classified = established["classified_volume"].sum()
    bid_dominant = (weighted_bid / total_classified) > 0.5 if total_classified else False
    return float(v_oi), bool(bid_dominant)


def full_pc(symbol: str, wall_df: pd.DataFrame, now: Optional[dt.datetime] = None) -> dict:
    """p_c_raw_full / p_c_raw_partial per CB-V4 Section 5's dual-output
    contract. Degrades honestly (components None, quality UNAVAILABLE) if
    the live GEX row isn't published yet rather than fabricating a flip/spot
    reading of its own."""
    row = load_live_gex_row(symbol)
    if row is None or row.get("spot") is None:
        return {
            "p_c_raw_full": None, "p_c_raw_partial": None,
            "g_state": None, "t_state": None, "f_state": None, "v_state": None,
            "quality": "UNAVAILABLE", "reason": "no_live_gex_row",
        }

    engine = gqe.CascadeBreakdownEngine()
    spot = row["spot"]
    gamma_flip = row.get("flip")
    net_vex = row.get("net_vex", 0.0)

    g = engine.g_state(spot, gamma_flip)

    ratio = load_vix_vxv_ratio()
    t = gqe.VolatilityTermStructureMonitor.t_state_from_ratio(ratio) if ratio is not None else 0.0

    tracker = gqe.VexExtremeTracker(window=90)
    tracker._history = load_vex_history_copy(symbol)
    v = tracker.v_state(net_vex)

    v_oi, bid_dominant = aggregate_flow_state(wall_df)
    f = gqe.CascadeBreakdownEngine.f_state(v_oi, bid_dominant)

    p_c_full = engine.probability(g, t, f, v, f_state_tracked=True)
    p_c_partial = engine.probability(g, t, f, v, f_state_tracked=False)

    return {
        "p_c_raw_full": round(float(p_c_full), 4),
        "p_c_raw_partial": round(float(p_c_partial), 4),
        "g_state": round(float(g), 4),
        "t_state": round(float(t), 4),
        "f_state": round(float(f), 4),
        "v_state": round(float(v), 4),
        "f_state_inputs": {"v_oi": round(v_oi, 4), "bid_dominant": bid_dominant},
        "quality": "FRESH",
        "reason": None,
    }


def ghost_wall_summary(wall_df: pd.DataFrame) -> dict:
    """Boolean ghost_wall flag (matching the existing engine's field
    convention) plus the full per-strike wall record list for audit /
    next-day confirmation."""
    if wall_df.empty:
        return {"ghost_wall": False, "walls": []}
    ghost_rows = wall_df[wall_df["wall_state"] == WALL_GHOST_CANDIDATE]
    return {
        "ghost_wall": not ghost_rows.empty,
        "walls": wall_df.to_dict(orient="records"),
    }


def options_cvd(minute_df: pd.DataFrame, contract_meta: dict[str, dict], greeks: dict[str, float], underlying_price: Optional[float]) -> dict:
    """Options-flow CVD -- TD-STD Section 8 / CB-V4 Section 5.
    contract_meta: contract_id -> {"right": "C"/"P"}.
    greeks: contract_id -> delta (nearest prior snapshot; estimated_delta=True
    always, since Standard has no trade-level Greeks)."""
    if minute_df.empty or underlying_price is None:
        return {
            "contract_imbalance": None, "premium_imbalance": None,
            "delta_notional_flow": None, "options_cvd": None, "coverage": None,
            "estimated_delta": True,
        }

    df = minute_df.copy()
    df["right_sign"] = df["contract_id"].map(lambda c: 1.0 if contract_meta.get(c, {}).get("right") == "C" else -1.0)
    df["delta"] = df["contract_id"].map(greeks)

    contract_imbalance = (df["ask_contracts"] - df["bid_contracts"]).sum()
    premium_imbalance = (df["ask_premium"] - df["bid_premium"]).sum()

    has_delta = df["delta"].notna()
    signed_ask = df.loc[has_delta, "ask_contracts"] * df.loc[has_delta, "right_sign"] * 100 * df.loc[has_delta, "delta"].abs() * underlying_price
    signed_bid = -df.loc[has_delta, "bid_contracts"] * df.loc[has_delta, "right_sign"] * 100 * df.loc[has_delta, "delta"].abs() * underlying_price
    delta_notional_flow = float((signed_ask + signed_bid).sum())

    directional = df["ask_contracts"].sum() + df["bid_contracts"].sum()
    eligible = directional + df["mid_contracts"].sum() + df["excluded_contracts"].sum()
    coverage = float(directional) / eligible if eligible else None

    return {
        "contract_imbalance": float(contract_imbalance),
        "premium_imbalance": float(premium_imbalance),
        "delta_notional_flow": delta_notional_flow,
        "options_cvd": delta_notional_flow,  # single-cycle read; snapshot.py accumulates across the session
        "coverage": coverage,
        "estimated_delta": True,
    }


SMILE_JUMP_THRESHOLD = 0.15  # initial research threshold (15 vol points between adjacent strikes), not calibrated


def _nearest_strike_iv(greeks_map: dict, expiration: str, right: str, spot: float):
    candidates = [
        (g["strike"], g["implied_vol"]) for g in greeks_map.values()
        if g["expiration"] == expiration and g["right"] == right and g["implied_vol"] is not None
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda sv: abs(sv[0] - spot))


def iv_skew_features(greeks_map: dict, spot: Optional[float], prior_atm_iv: Optional[dict] = None) -> dict:
    """ATM IV, ATM IV change, put/call skew, 0DTE-vs-next-expiry IV, and a
    smile-stability quality flag -- TD-STD Section 9 / CB-V4 Section 4's
    'IV and skew change' row. Pure function: `prior_atm_iv` (this same
    function's own atm_iv_by_expiration from the previous cycle) is passed
    in and the new reading is returned for the CALLER to persist -- state
    I/O stays centralized in snapshot.py, matching options_cvd's/full_pc's
    existing pattern (this module has no file-write side effects anywhere,
    per the package isolation rule)."""
    if not greeks_map or spot is None:
        return {
            "atm_iv_by_expiration": {}, "atm_iv_change": {}, "put_call_skew": {},
            "zero_dte_vs_next_iv": None, "smile_stability": {"max_adjacent_jump": None, "flag": False},
            "quality": "UNAVAILABLE",
        }

    expirations = sorted({g["expiration"] for g in greeks_map.values()})
    atm_iv_by_expiration: dict[str, dict] = {}
    put_call_skew: dict[str, Optional[float]] = {}
    for exp in expirations:
        call = _nearest_strike_iv(greeks_map, exp, "C", spot)
        put = _nearest_strike_iv(greeks_map, exp, "P", spot)
        atm_iv_by_expiration[exp] = {
            "call": call[1] if call else None,
            "put": put[1] if put else None,
        }
        put_call_skew[exp] = (put[1] - call[1]) if (call and put) else None

    prior_atm_iv = prior_atm_iv or {}
    atm_iv_change = {}
    for exp, ivs in atm_iv_by_expiration.items():
        prior = prior_atm_iv.get(exp) or {}
        changes = {}
        for right in ("call", "put"):
            cur, prev = ivs.get(right), prior.get(right)
            changes[right] = (cur - prev) if (cur is not None and prev is not None) else None
        atm_iv_change[exp] = changes

    zero_dte_vs_next = None
    if len(expirations) >= 2:
        near, nxt = atm_iv_by_expiration[expirations[0]], atm_iv_by_expiration[expirations[1]]
        near_avg = [v for v in (near.get("call"), near.get("put")) if v is not None]
        next_avg = [v for v in (nxt.get("call"), nxt.get("put")) if v is not None]
        if near_avg and next_avg:
            zero_dte_vs_next = (sum(near_avg) / len(near_avg)) - (sum(next_avg) / len(next_avg))

    # Smile stability on the nearest expiration only (most decision-relevant, CB-V4's own 0DTE emphasis).
    nearest_exp = expirations[0]
    for right in ("C", "P"):
        chain = sorted(
            ((g["strike"], g["implied_vol"]) for g in greeks_map.values()
             if g["expiration"] == nearest_exp and g["right"] == right and g["implied_vol"] is not None),
            key=lambda sv: sv[0],
        )
        if len(chain) >= 2:
            jumps = [abs(chain[i + 1][1] - chain[i][1]) for i in range(len(chain) - 1)]
            max_jump = max(jumps)
            break
    else:
        max_jump = None

    return {
        "atm_iv_by_expiration": atm_iv_by_expiration,
        "atm_iv_change": atm_iv_change,
        "put_call_skew": put_call_skew,
        "zero_dte_vs_next_iv": zero_dte_vs_next,
        "smile_stability": {
            "max_adjacent_jump": max_jump,
            "flag": (max_jump is not None and max_jump > SMILE_JUMP_THRESHOLD),
        },
        "quality": "FRESH",
    }


def _wall_state_at(wall_records: list[dict], strike: Optional[float], right: str) -> Optional[str]:
    if strike is None:
        return None
    for w in wall_records:
        if w.get("strike") == strike and w.get("right") == right:
            return w.get("wall_state")
    return None


def dealer_positioning_lean(live_gex_row: Optional[dict], wall_records: list[dict]) -> dict:
    """Evidence family 1/3: dealer positioning, from GEX walls + this
    package's own wall-state classification. Directional only when there's
    a real asymmetry between the call-wall and put-wall states (cracking
    vs. reinforced) -- CB-V4 Section 6's own dealer-positioning family."""
    if not live_gex_row:
        return {"lean": "unavailable", "note": "no live GEX row"}
    call_wall, put_wall = live_gex_row.get("call_wall"), live_gex_row.get("put_wall")
    call_state = _wall_state_at(wall_records, call_wall, "C")
    put_state = _wall_state_at(wall_records, put_wall, "P")
    call_cracking, put_cracking = call_state == WALL_GHOST_CANDIDATE, put_state == WALL_GHOST_CANDIDATE
    call_defended, put_defended = call_state == WALL_REINFORCED, put_state == WALL_REINFORCED

    if call_cracking and not put_cracking:
        return {"lean": "bullish", "note": f"call wall {call_wall} showing ghost-candidate flow"}
    if put_cracking and not call_cracking:
        return {"lean": "bearish", "note": f"put wall {put_wall} showing ghost-candidate flow"}
    if put_defended and not call_defended:
        return {"lean": "bullish", "note": f"put wall {put_wall} reinforced, call side not"}
    if call_defended and not put_defended:
        return {"lean": "bearish", "note": f"call wall {call_wall} reinforced, put side not"}
    if call_state is None and put_state is None:
        return {"lean": "unavailable", "note": "no wall-state read at either wall strike"}
    return {"lean": "neutral", "note": "no directional wall-state asymmetry"}


def options_flow_lean(cvd: dict) -> dict:
    """Evidence family 2/3: options flow, from cumulative options CVD."""
    value = cvd.get("options_cvd") if cvd else None
    coverage = cvd.get("coverage") if cvd else None
    if value is None:
        return {"lean": "unavailable", "note": "no CVD reading"}
    if coverage is not None and coverage < 0.3:
        return {"lean": "unavailable", "note": f"classification coverage too low ({coverage:.0%})"}
    if value > 0:
        return {"lean": "bullish", "note": f"cumulative options CVD +{value:,.0f}"}
    if value < 0:
        return {"lean": "bearish", "note": f"cumulative options CVD {value:,.0f}"}
    return {"lean": "neutral", "note": "cumulative options CVD flat"}


def volatility_lean(iv_skew: dict) -> dict:
    """Evidence family 3/3: volatility, from nearest-expiry put/call skew.
    CB-V4 treats volatility as context more than a hard directional vote
    (matching this codebase's own prior finding that IV-derived signals
    need non-naive treatment, see confluence_score.py's iv_stress) -- richer
    puts (positive skew) read as hedging-driven bearish demand, the mirror
    as bullish; deliberately a lower-confidence lean than the other two."""
    skew_by_exp = iv_skew.get("put_call_skew") if iv_skew else None
    if not skew_by_exp:
        return {"lean": "unavailable", "note": "no IV/skew reading"}
    nearest_exp = sorted(skew_by_exp.keys())[0]
    skew = skew_by_exp.get(nearest_exp)
    if skew is None:
        return {"lean": "unavailable", "note": "no ATM put/call pair for nearest expiration"}
    if skew > 0.02:
        return {"lean": "bearish", "note": f"put/call skew +{skew:.3f} (puts richer)"}
    if skew < -0.02:
        return {"lean": "bullish", "note": f"put/call skew {skew:.3f} (calls richer)"}
    return {"lean": "neutral", "note": f"put/call skew {skew:.3f}, no meaningful tilt"}


def _synthesize_verdict(leans: dict[str, str]) -> str:
    available = {k: v for k, v in leans.items() if v != "unavailable"}
    if len(available) < 2:
        return "WAIT - DATA"
    bullish = sum(1 for v in available.values() if v == "bullish")
    bearish = sum(1 for v in available.values() if v == "bearish")
    if bullish > bearish and bullish >= 2:
        return "CALL WATCH"
    if bearish > bullish and bearish >= 2:
        return "PUT WATCH"
    if bullish >= 1 and bearish >= 1:
        return "TWO-SIDED"
    return "NO TRADE - STRUCTURE"


def control_map_verdict(live_gex_row: Optional[dict], wall_records: list[dict], cvd: dict, iv_skew: dict) -> dict:
    """CB-V4 Section 4's Control Map verdict + Section 6's evidence-family
    synthesis, scoped honestly to the THREE families this options-
    microstructure package actually has direct evidence for (dealer
    positioning, options flow, volatility) -- price acceptance, cross-
    market, catalyst/news, and contract quality are CB-V4's remaining four
    families, owned by catalyst_brief.py's existing equity-bar/news/macro
    pipeline, not duplicated here. Pure synthesis, no new data pull.

    Verdict vocabulary matches CB-V4 Section 4 exactly: CALL WATCH / PUT
    WATCH need >=2 of 3 available families agreeing; TWO-SIDED on a real
    bullish-vs-bearish conflict; WAIT - DATA when fewer than 2 families
    have a reading at all; NO TRADE - STRUCTURE when none are unavailable
    but none are directional either. NO TRADE - CONTRACT is never emitted
    here -- contract viability is explicitly out of this phase's scope."""
    families = {
        "dealer_positioning": dealer_positioning_lean(live_gex_row, wall_records),
        "options_flow": options_flow_lean(cvd),
        "volatility": volatility_lean(iv_skew),
    }
    leans = {k: v["lean"] for k, v in families.items()}
    verdict = _synthesize_verdict(leans)

    # Narrative test (CB-V4 Section 6): recompute with each family forced to
    # neutral one at a time; report which removal(s) actually flip the verdict.
    flips = {}
    for name in families:
        zeroed = dict(leans)
        zeroed[name] = "neutral"
        alt_verdict = _synthesize_verdict(zeroed)
        if alt_verdict != verdict:
            flips[name] = alt_verdict

    return {
        "verdict": verdict,
        "families": families,
        "narrative_test": {
            "hinge_families": list(flips.keys()),
            "supporting_context_families": [n for n in families if n not in flips],
            "detail": flips,
        },
    }


def divergence_flags(price_change: Optional[float], cvd_change: Optional[float]) -> list[str]:
    """TD-STD Section 8 divergence library -- informational only, not a
    decision gate."""
    flags = []
    if price_change is None or cvd_change is None:
        return flags
    if price_change > 0 and cvd_change < 0:
        flags.append("bearish_divergence")
    elif price_change < 0 and cvd_change > 0:
        flags.append("bullish_divergence")
    elif abs(price_change) < 1e-9 and abs(cvd_change) > 0:
        flags.append("absorption_candidate")
    return flags
