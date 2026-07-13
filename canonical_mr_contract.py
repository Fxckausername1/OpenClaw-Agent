#!/usr/bin/env python3
"""Pure offline contract for the frozen live mean-reversion state machine."""

from __future__ import annotations

from datetime import datetime


DEFAULTS = {
    "z": 1.5,
    "vdev": 0.015,
    "rsi_oversold": 30.0,
    "rsi_overbought": 70.0,
    "min_rr": 1.5,
    "stop_buffer": 0.0015,
    "max_watch_checks": 12,
    "max_price": 250.0,
}


def _number(bar, *names):
    for name in names:
        if bar.get(name) is not None:
            return float(bar[name])
    return None


def _timestamp(bar):
    raw = bar.get("time", bar.get("timestamp", bar.get("ts")))
    return raw if isinstance(raw, datetime) else datetime.fromisoformat(str(raw))


def detect_mr(day_bars: list[dict], params: dict | None = None) -> list[dict]:
    """Replay every closed five-minute bar, matching the live */5 full-scan lifecycle."""
    p = {**DEFAULTS, **(params or {})}
    states = {}
    intents = []
    for index, bar in enumerate(day_bars):
        close = _number(bar, "close", "Close")
        high = _number(bar, "high", "High")
        low = _number(bar, "low", "Low")
        rsi = _number(bar, "rsi")
        vwap = _number(bar, "vwap")
        vdev = _number(bar, "vwap_dev")
        sma = _number(bar, "sma20")
        std = _number(bar, "std20")
        z = _number(bar, "z")
        if any(v is None for v in (close, high, low, rsi, vwap, vdev, sma, std, z)) or std <= 0:
            continue
        upper = sma + float(p["z"]) * std
        lower = sma - float(p["z"]) * std
        setup = {
            "LONG": z <= -float(p["z"]) and close < lower and rsi < float(p["rsi_oversold"]) and vdev <= -float(p["vdev"]),
            "SHORT": z >= float(p["z"]) and close > upper and rsi > float(p["rsi_overbought"]) and vdev >= float(p["vdev"]),
        }

        for side in ("SHORT", "LONG"):
            state = states.get(side)
            if state is None:
                if setup[side] and close <= float(p["max_price"]):
                    states[side] = (
                        {"stage": "watch", "signal_low": low, "extreme_high": high, "checks": 0}
                        if side == "SHORT"
                        else {"stage": "watch", "signal_high": high, "extreme_low": low, "checks": 0}
                    )
                continue
            if state["stage"] != "watch":
                continue

            entry = stop = rr = None
            crossed = False
            if side == "SHORT":
                state["extreme_high"] = max(state["extreme_high"], high)
                if close > upper:
                    state["signal_low"] = low
                    state["checks"] += 1
                elif low < state["signal_low"]:
                    crossed = True
                    entry = state["signal_low"]
                    stop = state["extreme_high"] * (1.0 + float(p["stop_buffer"]))
                    rr = (entry - vwap) / (stop - entry) if stop > entry else 0.0
                else:
                    state["checks"] += 1
            else:
                state["extreme_low"] = min(state["extreme_low"], low)
                if close < lower:
                    state["signal_high"] = high
                    state["checks"] += 1
                elif high > state["signal_high"]:
                    crossed = True
                    entry = state["signal_high"]
                    stop = state["extreme_low"] * (1.0 - float(p["stop_buffer"]))
                    rr = (vwap - entry) / (entry - stop) if entry > stop else 0.0
                else:
                    state["checks"] += 1

            if crossed:
                if rr >= float(p["min_rr"]):
                    state["stage"] = "triggered"
                    intents.append(
                        {
                            "strategy": "MR",
                            "side": side,
                            "signal_index": index,
                            "signal_time": _timestamp(bar).isoformat(),
                            "entry": entry,
                            "stop": stop,
                            "target": vwap,
                            "planned_rr": rr,
                            "z": z,
                            "rsi": rsi,
                            "vwap_dev": vdev,
                        }
                    )
                else:
                    state["stage"] = "expired"
            elif state["checks"] > int(p["max_watch_checks"]):
                state["stage"] = "expired"
    return intents
