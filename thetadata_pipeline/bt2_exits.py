"""BT-2 exit engine -- roadmap Section 8: hybrid exit family, intrabar
ordering, path-dependence tracking (MAE/MFE/time-to-target/time-underwater).

Default hybrid family, first-to-trigger wins: premium stop OR underlying
invalidation OR time stop (no progress after N minutes) OR forced
end-of-day close (15:30 ET).

Intrabar ordering: BT-1's option data is real trade-level ticks
(trade_timestamp/quote_timestamp per row), so premium-side target/stop
hits are walked and ordered EXACTLY as they occurred -- no ambiguity there
as long as ticks exist; walking strictly in timestamp order is itself the
resolution, not a special case. The underlying side is only ever 1-min
OHLC bars (BT-1's fetch_underlying_bars, Alpaca IEX feed) -- this codebase
has never ingested underlying tick data, live or historical -- so an
underlying invalidation level that falls within a single bar's [low, high]
range cannot be timestamped more precisely than "sometime in this minute."
When that imprecise underlying event lands in the SAME minute as a real,
precisely-ordered premium tick event, this module cannot honestly claim to
know which happened first -- Section 16's own rule applies: assume the
WORSE outcome (invalidation, since it overrides a target/stop that hadn't
been realized yet) came first, i.e. adverse ordering, and flag the trade
ambiguous in rule_flags, rather than picking the flattering order.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from typing import Optional
from zoneinfo import ZoneInfo

import pandas as pd

ET = ZoneInfo("America/New_York")

EXIT_TARGET = "TARGET"
EXIT_STOP = "STOP"
EXIT_INVALIDATION = "INVALIDATION"
EXIT_TIME_STOP = "TIME_STOP"
EXIT_FORCED_CLOSE = "FORCED_CLOSE"
EXIT_DATA_BLOCKED = "DATA_BLOCKED"

FLAG_AMBIGUOUS_INTRABAR = "AMBIGUOUS_INTRABAR_ORDERING_ADVERSE_APPLIED"
FLAG_DATA_GAP = "DATA_GAP_NO_INTERPOLATION"
FLAG_PARTIAL_DATA_GAP = "PARTIAL_DATA_GAP_DETECTED"

PARTIAL_GAP_MINUTES = 15  # consecutive tick-less core-session minutes before
                           # flagging a partial gap for honesty -- reporting
                           # only, does not by itself force an exit (the next
                           # real tick still correctly resolves target/stop).


@dataclasses.dataclass(frozen=True)
class ExitConfig:
    target_return: float = 0.25          # matches contract_quote.GROSS_TARGET_RETURN's research default
    premium_stop_pct: float = -0.20
    time_stop_minutes: int = 30
    forced_close_time: dt.time = dt.time(15, 30)


@dataclasses.dataclass
class ExitDecision:
    exit_ts: Optional[pd.Timestamp]
    exit_reason: str
    target_level: float
    stop_level: float
    invalidation_level: Optional[float]
    mae: float
    mfe: float
    time_to_target_seconds: Optional[float]
    time_underwater_seconds: float
    ambiguous: bool
    rule_flags: list


def _bars_by_minute(bars: Optional[pd.DataFrame]) -> dict:
    if bars is None or bars.empty:
        return {}
    out = {}
    for _, row in bars.iterrows():
        minute = pd.Timestamp(row["t"]).floor("min")
        out[minute] = row
    return out


def _invalidation_crossed(bar, level: Optional[float], right: str) -> bool:
    """right='C' (long call, bullish): invalidation is a downside level --
    crossed if the bar traded down to/through it. right='P' (long put,
    bearish): invalidation is an upside level -- crossed if the bar traded
    up to/through it."""
    if level is None or bar is None:
        return False
    if right == "C":
        return float(bar["l"]) <= level
    return float(bar["h"]) >= level


def resolve_exit(
    option_ticks: pd.DataFrame, underlying_bars: pd.DataFrame,
    entry_ts, entry_premium: float, right: str,
    invalidation_level: Optional[float], session_date: dt.date,
    config: ExitConfig = ExitConfig(),
) -> ExitDecision:
    """option_ticks: this contract's full trade_quote history for the
    session (trade_timestamp, bid columns), already point-in-time safe by
    construction (real historical data, nothing from the future was ever
    withheld -- the exit walk itself only advances forward from entry_ts).
    underlying_bars: 1-min OHLC for the session (t, o, h, l, c columns).
    invalidation_level: supplied by the caller (e.g. the HEFF-SMC
    indicator's own trigger/invalidation level at signal time) -- this
    module has no opinion on how that level is derived, only on how it
    interacts with the other exit legs once given. None disables that leg."""
    target_level = round(entry_premium * (1 + config.target_return), 4)
    stop_level = round(entry_premium * (1 + config.premium_stop_pct), 4)
    forced_close_ts = pd.Timestamp(dt.datetime.combine(session_date, config.forced_close_time), tz=ET)
    entry_ts = pd.Timestamp(entry_ts)

    ticks = (
        option_ticks[option_ticks["trade_timestamp"] > entry_ts].sort_values("trade_timestamp").reset_index(drop=True)
        if option_ticks is not None and not option_ticks.empty else pd.DataFrame()
    )
    bars_by_minute = _bars_by_minute(underlying_bars)

    mae = 0.0   # most-adverse-excursion in premium terms (positive = worse than entry)
    mfe = 0.0   # most-favorable-excursion in premium terms
    time_to_target: Optional[float] = None
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

        bar = bars_by_minute.get(minute)
        invalidation_hit = _invalidation_crossed(bar, invalidation_level, right)

        premium_event = None  # (reason, ts)
        for tick in minute_ticks:
            bid = tick.get("bid")
            if bid is None or pd.isna(bid):
                continue
            bid = float(bid)
            last_bid = bid
            mae = max(mae, entry_premium - bid)
            mfe = max(mfe, bid - entry_premium)
            if time_to_target is None and bid >= target_level:
                time_to_target = round((tick["trade_timestamp"] - entry_ts).total_seconds(), 1)
            if premium_event is None:
                if bid <= stop_level:
                    premium_event = (EXIT_STOP, tick["trade_timestamp"])
                elif bid >= target_level:
                    premium_event = (EXIT_TARGET, tick["trade_timestamp"])

        if invalidation_hit and premium_event is not None:
            rule_flags.append(FLAG_AMBIGUOUS_INTRABAR)
            return ExitDecision(
                exit_ts=minute, exit_reason=EXIT_INVALIDATION,
                target_level=target_level, stop_level=stop_level, invalidation_level=invalidation_level,
                mae=round(mae, 4), mfe=round(mfe, 4), time_to_target_seconds=time_to_target,
                time_underwater_seconds=round((minute - entry_ts).total_seconds(), 1),
                ambiguous=True, rule_flags=rule_flags,
            )
        if invalidation_hit:
            return ExitDecision(
                exit_ts=minute, exit_reason=EXIT_INVALIDATION,
                target_level=target_level, stop_level=stop_level, invalidation_level=invalidation_level,
                mae=round(mae, 4), mfe=round(mfe, 4), time_to_target_seconds=time_to_target,
                time_underwater_seconds=round((minute - entry_ts).total_seconds(), 1),
                ambiguous=False, rule_flags=rule_flags,
            )
        if premium_event is not None:
            reason, ts = premium_event
            return ExitDecision(
                exit_ts=ts, exit_reason=reason,
                target_level=target_level, stop_level=stop_level, invalidation_level=invalidation_level,
                mae=round(mae, 4), mfe=round(mfe, 4), time_to_target_seconds=time_to_target,
                time_underwater_seconds=round((ts - entry_ts).total_seconds(), 1),
                ambiguous=False, rule_flags=rule_flags,
            )

        elapsed_minutes = (minute_end - entry_ts).total_seconds() / 60.0
        no_progress = last_bid <= entry_premium
        if elapsed_minutes >= config.time_stop_minutes and no_progress:
            return ExitDecision(
                exit_ts=minute_end, exit_reason=EXIT_TIME_STOP,
                target_level=target_level, stop_level=stop_level, invalidation_level=invalidation_level,
                mae=round(mae, 4), mfe=round(mfe, 4), time_to_target_seconds=time_to_target,
                time_underwater_seconds=round((minute_end - entry_ts).total_seconds(), 1),
                ambiguous=False, rule_flags=rule_flags,
            )

        minute = minute_end

    if n_ticks == 0:
        rule_flags.append(FLAG_DATA_GAP)
        return ExitDecision(
            exit_ts=forced_close_ts, exit_reason=EXIT_DATA_BLOCKED,
            target_level=target_level, stop_level=stop_level, invalidation_level=invalidation_level,
            mae=round(mae, 4), mfe=round(mfe, 4), time_to_target_seconds=time_to_target,
            time_underwater_seconds=round((forced_close_ts - entry_ts).total_seconds(), 1),
            ambiguous=False, rule_flags=rule_flags,
        )

    return ExitDecision(
        exit_ts=forced_close_ts, exit_reason=EXIT_FORCED_CLOSE,
        target_level=target_level, stop_level=stop_level, invalidation_level=invalidation_level,
        mae=round(mae, 4), mfe=round(mfe, 4), time_to_target_seconds=time_to_target,
        time_underwater_seconds=round((forced_close_ts - entry_ts).total_seconds(), 1),
        ambiguous=False, rule_flags=rule_flags,
    )
