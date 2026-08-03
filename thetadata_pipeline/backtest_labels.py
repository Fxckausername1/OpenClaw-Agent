"""TD-5 / TD-STD Section 7's calibration requirement: "Define wall break/hold
outcomes at 5, 15 and 30 minutes using point-in-time levels." Pure, testable
labeling functions -- no ThetaData/Alpaca calls here, no side effects. The
calibration script (calibrate_thetadata_thresholds.py) owns fetching and
normalizing the underlying bars; this module only reasons about levels
already in hand, so it stays decoupled from any specific bar-data provider's
raw response shape.
"""

from __future__ import annotations

import datetime as dt
from typing import Optional

import pandas as pd

HOLD = "hold"
BREAK = "break"
UNAVAILABLE = "unavailable"

DEFAULT_HORIZONS = (5, 15, 30)


def label_wall_break_hold(
    strike: float,
    spot_at_observation: float,
    observed_at: dt.datetime,
    minute_bars: pd.DataFrame,
    horizons: tuple[int, ...] = DEFAULT_HORIZONS,
) -> dict[int, str]:
    """Whether price traded through `strike` within each horizon (minutes)
    after `observed_at`.

    `minute_bars` must have `timestamp` (tz-aware, comparable to
    `observed_at`), `high`, and `low` columns -- one row per minute, any
    order. Direction is derived from the wall's position relative to spot
    at observation time, not from call/put: a strike above spot is acting
    as resistance (break = a subsequent high >= strike); a strike below
    spot is acting as support (break = a subsequent low <= strike). A
    strike equal to spot at observation is a degenerate case (the wall
    isn't actually containing anything yet) and every horizon is
    UNAVAILABLE rather than an arbitrary guess at direction.

    Each horizon is UNAVAILABLE independently if there simply aren't
    enough completed bars yet to answer it (e.g. the wall was observed at
    15:50 -- a 30-minute horizon has no data, but 5-minute might)."""
    if strike == spot_at_observation or minute_bars is None or minute_bars.empty:
        return {h: UNAVAILABLE for h in horizons}

    resistance = strike > spot_at_observation
    bars = minute_bars[minute_bars["timestamp"] > observed_at].sort_values("timestamp")

    out: dict[int, str] = {}
    for horizon in horizons:
        cutoff = observed_at + dt.timedelta(minutes=horizon)
        window = bars[bars["timestamp"] <= cutoff]
        if window.empty:
            out[horizon] = UNAVAILABLE
            continue
        if resistance:
            crossed = bool((window["high"] >= strike).any())
        else:
            crossed = bool((window["low"] <= strike).any())
        out[horizon] = BREAK if crossed else HOLD
    return out
