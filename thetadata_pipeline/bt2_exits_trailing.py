"""BT-3 trailing-exit variant (heff's 2026-07-30 ask, following the reclaim
gap he found by re-watching the live chart: "sometimes QQQ moves $10 off a
triangle... 200-300% on a con worth $100" -- and reclaim doesn't reliably
fire to capture that, so a trade can ride a huge favorable move all the way
back down with no signal to lock it in).

Replaces BOTH the fixed +25% target AND the fixed -20% stop-from-entry with
ONE mechanism: exit if price pulls back trail_pct (default -20%, same
magnitude as the existing stop, just measured from the trade's own PEAK
instead of a fixed entry price) since entry. 30-min no-progress time stop
and forced 15:30 ET close are UNCHANGED, same "replace only what needs
replacing" discipline as the reclaim variant.

Key property, worth being explicit about: if a trade never goes green,
peak_bid stays at entry_premium the whole time, so trailing_level ==
entry_premium * (1 + trail_pct) -- IDENTICAL to the current fixed stop in
that case. This is a strict generalization of the existing stop, not an
unrelated new mechanism: same floor when there's no favorable move, adapts
upward (protecting more) once there is one.

Simpler than the reclaim variant on purpose: no bar-level event to
reconcile against tick-precise premium data, so none of resolve_exit's
intrabar-ordering/ambiguity machinery is needed here -- this is a pure,
tick-by-tick premium walk, same shape as the ORIGINAL target/stop checks
in bt2_exits.py, just with a moving level instead of two fixed ones.

Deliberately a NEW, isolated module (same reasoning as bt2_exits_reclaim.py)
-- doesn't touch bt2_exits.py, which the currently-running indicator holdout
sweep still imports.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
from typing import Optional

import pandas as pd

from thetadata_pipeline.bt2_exits import (
    ET, EXIT_FORCED_CLOSE, EXIT_TIME_STOP, EXIT_DATA_BLOCKED,
    FLAG_DATA_GAP, FLAG_PARTIAL_DATA_GAP, PARTIAL_GAP_MINUTES, ExitDecision,
)

EXIT_TRAILING_STOP = "TRAILING_STOP"


@dataclasses.dataclass(frozen=True)
class TrailingExitConfig:
    """Same defaults as bt2_exits.ExitConfig for every leg that's unchanged.
    No target_return field -- the trailing mechanism replaces target AND
    the fixed stop both. trail_pct reuses the existing stop's own -0.20
    magnitude deliberately (same number, just measured from peak instead
    of entry) -- not a new, undisclosed tuning choice."""
    trail_pct: float = -0.20
    time_stop_minutes: int = 30
    forced_close_time: dt.time = dt.time(15, 30)


def resolve_exit_trailing(
    option_ticks: pd.DataFrame,
    entry_ts, entry_premium: float, session_date: dt.date,
    config: TrailingExitConfig = TrailingExitConfig(),
) -> ExitDecision:
    """Mirrors bt2_exits.resolve_exit's walk, minus target/fixed-stop, plus
    a trailing-from-peak stop. Pure premium-tick walk -- no underlying_bars
    input needed at all for this variant."""
    forced_close_ts = pd.Timestamp(dt.datetime.combine(session_date, config.forced_close_time), tz=ET)
    entry_ts = pd.Timestamp(entry_ts)

    ticks = (
        option_ticks[option_ticks["trade_timestamp"] > entry_ts].sort_values("trade_timestamp").reset_index(drop=True)
        if option_ticks is not None and not option_ticks.empty else pd.DataFrame()
    )

    mae = 0.0
    mfe = 0.0
    peak_bid = entry_premium  # trailing reference -- max bid observed since entry, floor at entry
    time_to_target: Optional[float] = None  # "target" here = first time trailing_level would have been at/above entry (informational)
    rule_flags: list = []
    last_bid = entry_premium
    consecutive_empty_minutes = 0

    n_ticks = len(ticks)
    tick_idx = 0
    minute = entry_ts.floor("min")

    while minute <= forced_close_ts.floor("min"):
        minute_end = minute + pd.Timedelta(minutes=1)
        minute_ticks = []
        while tick_idx < n_ticks and ticks.loc[tick_idx, "trade_timestamp"] < minute_end:
            minute_ticks.append(ticks.loc[tick_idx])
            tick_idx += 1

        if minute_ticks:
            consecutive_empty_minutes = 0
        else:
            consecutive_empty_minutes += 1
            if consecutive_empty_minutes == PARTIAL_GAP_MINUTES and FLAG_PARTIAL_DATA_GAP not in rule_flags:
                rule_flags.append(FLAG_PARTIAL_DATA_GAP)

        trail_event_ts = None
        for tick in minute_ticks:
            bid = tick.get("bid")
            if bid is None or pd.isna(bid):
                continue
            bid = float(bid)
            last_bid = bid
            mae = max(mae, entry_premium - bid)
            mfe = max(mfe, bid - entry_premium)
            # update peak BEFORE checking the trailing level against this same tick --
            # a tick that itself sets a new peak cannot also be the tick that breaches
            # the (now-higher) trailing level off that same peak
            peak_bid = max(peak_bid, bid)
            trailing_level = round(peak_bid * (1 + config.trail_pct), 4)
            if time_to_target is None and peak_bid > entry_premium:
                time_to_target = round((tick["trade_timestamp"] - entry_ts).total_seconds(), 1)
            if trail_event_ts is None and bid <= trailing_level:
                trail_event_ts = tick["trade_timestamp"]

        if trail_event_ts is not None:
            return ExitDecision(
                exit_ts=trail_event_ts, exit_reason=EXIT_TRAILING_STOP,
                target_level=float("nan"), stop_level=round(peak_bid * (1 + config.trail_pct), 4),
                invalidation_level=None,
                mae=round(mae, 4), mfe=round(mfe, 4), time_to_target_seconds=time_to_target,
                time_underwater_seconds=round((trail_event_ts - entry_ts).total_seconds(), 1),
                ambiguous=False, rule_flags=rule_flags,
            )

        elapsed_minutes = (minute_end - entry_ts).total_seconds() / 60.0
        no_progress = last_bid <= entry_premium
        if elapsed_minutes >= config.time_stop_minutes and no_progress:
            return ExitDecision(
                exit_ts=minute_end, exit_reason=EXIT_TIME_STOP,
                target_level=float("nan"), stop_level=round(peak_bid * (1 + config.trail_pct), 4),
                invalidation_level=None,
                mae=round(mae, 4), mfe=round(mfe, 4), time_to_target_seconds=time_to_target,
                time_underwater_seconds=round((minute_end - entry_ts).total_seconds(), 1),
                ambiguous=False, rule_flags=rule_flags,
            )

        minute = minute_end

    if n_ticks == 0:
        rule_flags.append(FLAG_DATA_GAP)
        return ExitDecision(
            exit_ts=forced_close_ts, exit_reason=EXIT_DATA_BLOCKED,
            target_level=float("nan"), stop_level=round(peak_bid * (1 + config.trail_pct), 4), invalidation_level=None,
            mae=round(mae, 4), mfe=round(mfe, 4), time_to_target_seconds=time_to_target,
            time_underwater_seconds=round((forced_close_ts - entry_ts).total_seconds(), 1),
            ambiguous=False, rule_flags=rule_flags,
        )

    return ExitDecision(
        exit_ts=forced_close_ts, exit_reason=EXIT_FORCED_CLOSE,
        target_level=float("nan"), stop_level=round(peak_bid * (1 + config.trail_pct), 4), invalidation_level=None,
        mae=round(mae, 4), mfe=round(mfe, 4), time_to_target_seconds=time_to_target,
        time_underwater_seconds=round((forced_close_ts - entry_ts).total_seconds(), 1),
        ambiguous=False, rule_flags=rule_flags,
    )
