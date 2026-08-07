#!/usr/bin/env python3
"""Pure, offline MR/ORB decision and fill contracts.

This module is the canonical research specification for the frozen live behavior. It has
no filesystem, network, broker, logging, or notification side effects. Callers provide
already-closed chronological bars and receive deterministic intents/outcomes.
"""

from __future__ import annotations

from datetime import datetime, time
from typing import Any


CONTRACT_VERSION = "2026-07-13.1"

MR_DEFAULTS = {
    "z": 1.5,
    "vdev": 0.015,
    "rsi_oversold": 30.0,
    "rsi_overbought": 70.0,
    "min_rr": 1.5,
    "stop_buffer": 0.0015,
    "max_watch_checks": 12,
    "max_price": 250.0,
}

ORB_DEFAULTS = {
    "or_end": "09:45",
    "vol_mult": 1.5,
    "max_range_frac": 0.0066,
    "use_vwap": True,
    "use_vol": True,
    "use_sector_gate": True,
    "max_price": 250.0,
}


def _value(bar: dict, *names: str, default=None):
    for name in names:
        if name in bar and bar[name] is not None:
            return bar[name]
    return default


def _float(bar: dict, *names: str, default=None):
    value = _value(bar, *names, default=default)
    return float(value) if value is not None else None


def _bar_time(bar: dict) -> datetime:
    raw = _value(bar, "time", "timestamp", "ts")
    if isinstance(raw, datetime):
        return raw
    return datetime.fromisoformat(str(raw))


def _clock(value: str) -> time:
    hour, minute = value.split(":", 1)
    return time(int(hour), int(minute))


def _side_r(side: str, entry: float, stop: float, exit_price: float) -> float:
    risk = abs(entry - stop)
    if risk <= 0:
        raise ValueError("entry and stop must differ")
    direction = 1.0 if side == "LONG" else -1.0
    return (exit_price - entry) * direction / risk


def simulate_boundary_limit(
    bars: list[dict],
    signal_index: int,
    side: str,
    entry: float,
    stop: float,
    target: float | None,
) -> dict:
    """Simulate the actual delayed boundary-limit design.

    The signal is known only after `signal_index` closes, so the order cannot fill inside
    that signal bar. It becomes active on the next bar. A touched limit fills exactly at
    the limit; if stop and target are both reachable in the fill/exit bar, stop wins.
    """
    side = side.upper()
    if side not in {"LONG", "SHORT"}:
        raise ValueError(f"bad side: {side}")
    future = bars[signal_index + 1 :]
    if not future:
        return {"fill_state": "never_filled", "outcome_r": 0.0, "exit_reason": "no_future_bars"}

    filled = False
    fill_time = None
    for bar in future:
        high = _float(bar, "high", "High")
        low = _float(bar, "low", "Low")
        if high is None or low is None:
            continue
        if not filled:
            touched = low <= entry if side == "LONG" else high >= entry
            if not touched:
                continue
            filled = True
            fill_time = _bar_time(bar).isoformat()

        stop_hit = low <= stop if side == "LONG" else high >= stop
        target_hit = False
        if target is not None:
            target_hit = high >= target if side == "LONG" else low <= target
        if stop_hit:
            return {
                "fill_state": "filled_closed",
                "fill_price": entry,
                "fill_time": fill_time,
                "exit_price": stop,
                "exit_time": _bar_time(bar).isoformat(),
                "exit_reason": "stop",
                "outcome_r": -1.0,
            }
        if target_hit:
            return {
                "fill_state": "filled_closed",
                "fill_price": entry,
                "fill_time": fill_time,
                "exit_price": target,
                "exit_time": _bar_time(bar).isoformat(),
                "exit_reason": "target",
                "outcome_r": _side_r(side, entry, stop, target),
            }

    if not filled:
        return {"fill_state": "never_filled", "outcome_r": 0.0, "exit_reason": "limit_not_touched"}
    last = future[-1]
    close = _float(last, "close", "Close")
    return {
        "fill_state": "filled_closed",
        "fill_price": entry,
        "fill_time": fill_time,
        "exit_price": close,
        "exit_time": _bar_time(last).isoformat(),
        "exit_reason": "eod",
        "outcome_r": _side_r(side, entry, stop, close),
    }


def detect_orb(day_bars: list[dict], params: dict | None = None, sector_hot: bool | None = None) -> list[dict]:
    """Return zero or one close-confirmed ORB intent for a symbol-day."""
    p = {**ORB_DEFAULTS, **(params or {})}
    if p["use_sector_gate"] and sector_hot is False:
        return []
    end = _clock(str(p["or_end"]))
    opening = [b for b in day_bars if _bar_time(b).time() < end]
    post = [(i, b) for i, b in enumerate(day_bars) if _bar_time(b).time() >= end]
    if not opening or len(post) < 2:
        return []
    orh = max(_float(b, "high", "High") for b in opening)
    orl = min(_float(b, "low", "Low") for b in opening)
    risk = orh - orl
    if risk <= 0 or orh > float(p["max_price"]):
        return []
    if risk / orh > float(p["max_range_frac"]):
        return []

    cumulative_pv = 0.0
    cumulative_volume = 0.0
    cumulative_bars = 0
    derived = {}
    for index, bar in enumerate(day_bars):
        high = _float(bar, "high", "High")
        low = _float(bar, "low", "Low")
        close = _float(bar, "close", "Close")
        volume = _float(bar, "volume", "Volume", default=0.0) or 0.0
        typical = (high + low + close) / 3.0
        cumulative_pv += typical * volume
        cumulative_volume += volume
        cumulative_bars += 1
        derived[index] = {
            "vwap": cumulative_pv / cumulative_volume if cumulative_volume > 0 else close,
            "avgvol": cumulative_volume / cumulative_bars,
        }

    for index, bar in post:
        close = _float(bar, "close", "Close")
        open_price = _float(bar, "open", "Open")
        up = close > orh
        down = close < orl
        if not (up or down):
            continue
        side = "LONG" if up else "SHORT"
        if up and down:  # impossible for one close, retained as a defensive contract
            side = "LONG" if close >= open_price else "SHORT"
        vwap = _float(bar, "vwap", default=derived[index]["vwap"])
        avgvol = _float(bar, "avgvol", default=derived[index]["avgvol"])
        volume = _float(bar, "volume", "Volume", default=0.0) or 0.0
        relvol = volume / avgvol if avgvol and avgvol > 0 else 0.0
        take_vwap = close > vwap if side == "LONG" else close < vwap
        take_vol = relvol >= float(p["vol_mult"])
        if p["use_vwap"] and not take_vwap:
            return []
        if p["use_vol"] and not take_vol:
            return []
        entry = orh if side == "LONG" else orl
        stop = orl if side == "LONG" else orh
        range_pct = risk / orh * 100.0
        return [
            {
                "strategy": "ORB",
                "side": side,
                "signal_index": index,
                "signal_time": _bar_time(bar).isoformat(),
                "entry": entry,
                "stop": stop,
                "target": None,
                "range_frac": risk / entry,
                "range_pct": range_pct,
                "relvol": relvol,
                "vwap": vwap,
                "planned_rr_proxy": min(5.0, 1.0 / range_pct) if range_pct > 0 else 2.0,
            }
        ]
    return []


def detect_mr(day_bars: list[dict], params: dict | None = None) -> list[dict]:
    """Replay the live full-scan MR state machine on every closed five-minute bar."""
    p = {**MR_DEFAULTS, **(params or {})}
    states: dict[str, dict[str, Any]] = {}
    intents = []
    for index, bar in enumerate(day_bars):
        close = _float(bar, "close", "Close")
        high = _float(bar, "high", "High")
        low = _float(bar, "low", "Low")
        rsi = _float(bar, "rsi")
        vwap = _float(bar, "vwap")
        vdev = _float(bar, "vwap_dev")
        sma = _float(bar, "sma20")
        std = _float(bar, "std20")
        z = _float(bar, "z")
        if None in (close, high, low, rsi, vwap, vdev, sma, std, z) or std <= 0:
            continue
        upper = sma + float(p["z"]) * std
        lower = sma - float(p["z"]) * std
        setups = {
            "LONG": z <= -float(p["z"]) and close < lower and rsi < float(p["rsi_oversold"]) and vdev <= -float(p["vdev"]),
            "SHORT": z >= float(p["z"]) and close > upper and rsi > float(p["rsi_overbought"]) and vdev >= float(p["vdev"]),
        }

        for side in ("SHORT", "LONG"):
            state = states.get(side)
            if state is None:
                if setups[side] and close <= float(p["max_price"]):
                    states[side] = (
                        {"stage": "watch", "signal_low": low, "extreme_high": high, "checks": 0}
                        if side == "SHORT"
                        else {"stage": "watch", "signal_high": high, "extreme_low": low, "checks": 0}
                    )
                continue
            if state["stage"] != "watch":
                continue

            triggered = False
            if side == "SHORT":
                state["extreme_high"] = max(state["extreme_high"], high)
                if close > upper:
                    state["signal_low"] = low
                    state["checks"] += 1
                elif low < state["signal_low"]:
                    entry = state["signal_low"]
                    stop = state["extreme_high"] * (1.0 + float(p["stop_buffer"]))
                    rr = (entry - vwap) / (stop - entry) if stop > entry else 0.0
                    triggered = rr >= float(p["min_rr"])
                else:
                    state["checks"] += 1
            else:
                state["extreme_low"] = min(state["extreme_low"], low)
                if close < lower:
                    state["signal_high"] = high
                    state["checks"] += 1
                elif high > state["signal_high"]:
                    entry = state["signal_high"]
                    stop = state["extreme_low"] * (1.0 - float(p["stop_buffer"]))
                    rr = (vwap - entry) / (entry - stop) if entry > stop else 0.0
                    triggered = rr >= float(p["min_rr"])
                else:
                    state["checks"] += 1

            if triggered:
                state["stage"] = "triggered"
                intents.append(
                    {
                        "strategy": "MR",
                        "side": side,
                        "signal_index": index,
                        "signal_time": _bar_time(bar).isoformat(),
                        "entry": entry,
                        "stop": stop,
                        "target": vwap,
                        "planned_rr": rr,
                        "z": z,
                        "rsi": rsi,
                        "vwap_dev": vdev,
                    }
                )
            elif "entry" in locals() and rr < float(p["min_rr"]):
                state["stage"] = "expired"
            elif state.get("stage") == "watch" and state["checks"] > int(p["max_watch_checks"]):
                state["stage"] = "expired"
    return intents
