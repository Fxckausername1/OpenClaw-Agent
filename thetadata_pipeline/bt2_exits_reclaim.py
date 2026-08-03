"""BT-3 reclaim-exit variant (heff's 2026-07-30 ask): replace the fixed
+25% premium target with "a new same-direction sweep-and-reclaim event
fires after entry" -- everything else in the exit family (premium stop,
30-min no-progress time stop, forced 15:30 ET close) stays byte-for-byte
identical to bt2_exits.ExitConfig's defaults, per heff's explicit "replace,
not layered on top... just replace 25% with reclaim icon."

Deliberately a NEW, isolated module reusing bt2_exits.py's tested helpers
(_bars_by_minute, the ExitDecision shape, the exit-reason constants) rather
than editing that file -- it's actively imported by the currently-running
indicator holdout sweep, and this is a genuinely different exit family, not
a bug fix to the existing one.

THE ONE SUBTLE CORRECTNESS POINT, worth being explicit about: reclaim data
comes from 1-min underlying bars (heff_smc_engine's per-bar diagnostic
output), same imprecision problem bt2_exits.py's own invalidation leg
already solved for -- a bar-level event can't be timestamped more
precisely than "sometime in this minute," so when it lands in the same
minute as a tick-precise premium event, this module cannot honestly know
which happened first. bt2_exits.py's own rule for this exact class of
ambiguity is "assume the worse outcome for the trade happened first."
Applied here: reclaim is GOOD news (take profit), stop is BAD news -- so
if both are true in the same minute, STOP wins the tie, not reclaim. This
is the mirror image of bt2_exits.py's own invalidation-vs-premium logic
(there, invalidation was already the bad-news leg, so it always won ties
by construction; here, the bad-news leg is whichever premium event fired,
so the tie-break has to be applied explicitly rather than falling out of
which branch is bad news).

SECOND correctness point: a reclaim event on the ENTRY bar itself doesn't
count (heff's framing was "reclaim AFTER the triangle" -- a NEW, later
confirmation, not the same bar that already triggered entry, since the
entry trigger can itself BE a sweep-reclaim event). The reclaim check is
only evaluated starting the bar strictly after entry; stop/time-stop/
forced-close all still start evaluating from the entry bar itself,
unchanged from bt2_exits.py's own walk.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
from typing import Optional

import pandas as pd

from thetadata_pipeline.bt2_exits import (
    ET, EXIT_FORCED_CLOSE, EXIT_STOP, EXIT_TIME_STOP, EXIT_DATA_BLOCKED,
    FLAG_AMBIGUOUS_INTRABAR, FLAG_DATA_GAP, FLAG_PARTIAL_DATA_GAP, PARTIAL_GAP_MINUTES,
    ExitDecision, _bars_by_minute,
)

EXIT_RECLAIM = "RECLAIM"


@dataclasses.dataclass(frozen=True)
class ReclaimExitConfig:
    """Same defaults as bt2_exits.ExitConfig for every leg that's unchanged.
    No target_return field -- reclaim replaces it entirely, not a parallel
    leg."""
    premium_stop_pct: float = -0.20
    time_stop_minutes: int = 30
    forced_close_time: dt.time = dt.time(15, 30)


def _reclaim_hit(bar, right: str) -> bool:
    """right='C' (long call): look for sweep_reclaim_long. right='P'
    (long put): look for sweep_reclaim_short. Same direction as the trade
    itself -- a reclaim in the OPPOSITE direction is not a profit-taking
    confirmation, it's a warning sign, and deliberately not treated as an
    exit trigger of either kind here (out of scope for this experiment;
    heff asked to test the same-direction reclaim specifically)."""
    if bar is None:
        return False
    col = "sweep_reclaim_long" if right == "C" else "sweep_reclaim_short"
    val = bar.get(col)
    return bool(val) if val is not None and not pd.isna(val) else False


def resolve_exit_reclaim(
    option_ticks: pd.DataFrame, reclaim_bars: pd.DataFrame,
    entry_ts, entry_premium: float, right: str, session_date: dt.date,
    config: ReclaimExitConfig = ReclaimExitConfig(),
) -> ExitDecision:
    """Mirrors bt2_exits.resolve_exit's walk exactly, minus the target
    leg, plus the reclaim leg. reclaim_bars: 1-min rows with a 't' column
    and 'sweep_reclaim_long'/'sweep_reclaim_short' booleans (the engine's
    own diagnostic frame, filtered to this session)."""
    stop_level = round(entry_premium * (1 + config.premium_stop_pct), 4)
    forced_close_ts = pd.Timestamp(dt.datetime.combine(session_date, config.forced_close_time), tz=ET)
    entry_ts = pd.Timestamp(entry_ts)
    entry_minute = entry_ts.floor("min")

    ticks = (
        option_ticks[option_ticks["trade_timestamp"] > entry_ts].sort_values("trade_timestamp").reset_index(drop=True)
        if option_ticks is not None and not option_ticks.empty else pd.DataFrame()
    )
    bars_by_minute = _bars_by_minute(reclaim_bars)

    mae = 0.0
    mfe = 0.0
    time_to_target: Optional[float] = None  # kept in the ledger shape; "target" here means reclaim exit, first bid recorded at that point
    rule_flags: list = []
    last_bid = entry_premium
    consecutive_empty_minutes = 0

    n_ticks = len(ticks)
    tick_idx = 0
    minute = entry_minute

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
        # entry bar's own reclaim (if any) doesn't count -- must be strictly after entry
        reclaim_hit = (minute > entry_minute) and _reclaim_hit(bar, right)

        stop_event_ts = None
        for tick in minute_ticks:
            bid = tick.get("bid")
            if bid is None or pd.isna(bid):
                continue
            bid = float(bid)
            last_bid = bid
            mae = max(mae, entry_premium - bid)
            mfe = max(mfe, bid - entry_premium)
            if stop_event_ts is None and bid <= stop_level:
                stop_event_ts = tick["trade_timestamp"]

        if reclaim_hit and stop_event_ts is not None:
            # ambiguous same-minute: bad news (stop) assumed first, per
            # bt2_exits.py's own "assume the worse outcome" honesty rule
            rule_flags.append(FLAG_AMBIGUOUS_INTRABAR)
            return ExitDecision(
                exit_ts=minute, exit_reason=EXIT_STOP,
                target_level=float("nan"), stop_level=stop_level, invalidation_level=None,
                mae=round(mae, 4), mfe=round(mfe, 4), time_to_target_seconds=time_to_target,
                time_underwater_seconds=round((minute - entry_ts).total_seconds(), 1),
                ambiguous=True, rule_flags=rule_flags,
            )
        if stop_event_ts is not None:
            return ExitDecision(
                exit_ts=stop_event_ts, exit_reason=EXIT_STOP,
                target_level=float("nan"), stop_level=stop_level, invalidation_level=None,
                mae=round(mae, 4), mfe=round(mfe, 4), time_to_target_seconds=time_to_target,
                time_underwater_seconds=round((stop_event_ts - entry_ts).total_seconds(), 1),
                ambiguous=False, rule_flags=rule_flags,
            )
        if reclaim_hit:
            if time_to_target is None:
                time_to_target = round((minute - entry_ts).total_seconds(), 1)
            return ExitDecision(
                exit_ts=minute, exit_reason=EXIT_RECLAIM,
                target_level=float("nan"), stop_level=stop_level, invalidation_level=None,
                mae=round(mae, 4), mfe=round(mfe, 4), time_to_target_seconds=time_to_target,
                time_underwater_seconds=round((minute - entry_ts).total_seconds(), 1),
                ambiguous=False, rule_flags=rule_flags,
            )

        elapsed_minutes = (minute_end - entry_ts).total_seconds() / 60.0
        no_progress = last_bid <= entry_premium
        if elapsed_minutes >= config.time_stop_minutes and no_progress:
            return ExitDecision(
                exit_ts=minute_end, exit_reason=EXIT_TIME_STOP,
                target_level=float("nan"), stop_level=stop_level, invalidation_level=None,
                mae=round(mae, 4), mfe=round(mfe, 4), time_to_target_seconds=time_to_target,
                time_underwater_seconds=round((minute_end - entry_ts).total_seconds(), 1),
                ambiguous=False, rule_flags=rule_flags,
            )

        minute = minute_end

    if n_ticks == 0:
        rule_flags.append(FLAG_DATA_GAP)
        return ExitDecision(
            exit_ts=forced_close_ts, exit_reason=EXIT_DATA_BLOCKED,
            target_level=float("nan"), stop_level=stop_level, invalidation_level=None,
            mae=round(mae, 4), mfe=round(mfe, 4), time_to_target_seconds=time_to_target,
            time_underwater_seconds=round((forced_close_ts - entry_ts).total_seconds(), 1),
            ambiguous=False, rule_flags=rule_flags,
        )

    return ExitDecision(
        exit_ts=forced_close_ts, exit_reason=EXIT_FORCED_CLOSE,
        target_level=float("nan"), stop_level=stop_level, invalidation_level=None,
        mae=round(mae, 4), mfe=round(mfe, 4), time_to_target_seconds=time_to_target,
        time_underwater_seconds=round((forced_close_ts - entry_ts).total_seconds(), 1),
        ambiguous=False, rule_flags=rule_flags,
    )
