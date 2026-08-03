"""SMC risk guardrails -- the gate every entry must pass, plus the EOD/early-close
flatten watchdog.

Context for why these numbers exist at all: on 2026-07-31 the strategy took TEN
entries in a single session with no cap of any kind -- no daily loss limit, no
concurrency limit, no rate limit, no consecutive-loss breaker. Three of those
entries were on the SAME contract inside 30 minutes. Net -$101, and one position
went unsupervised because nothing was watching the aggregate picture. Every gate
below maps to a specific way that session could have been stopped earlier.

Design rules:
* Gates are WITHHOLD-ONLY. They block NEW entries. They never block, delay, or
  weaken a protective exit -- an exit is how risk gets smaller, so gating it
  would invert the purpose of a risk system.
* Every gate returns a human-readable reason. A silent block is an operational
  trap; the whole point is that a human can see why the strategy stopped.
* Defaults are conservative for paper. They are configurable so a canary run can
  tighten them further (see SmcConfig / SMC_* env overrides).
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import logging
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from . import calendar as smc_calendar
from .reconcile import assert_occ_free_for_entry
from .state import CLOSED, SmcStateError, SmcStateStore

ET = ZoneInfo("America/New_York")
logger = logging.getLogger("smc.risk")


@dataclasses.dataclass
class GateResult:
    allowed: bool
    reasons: list = dataclasses.field(default_factory=list)

    def block(self, reason: str) -> None:
        self.allowed = False
        self.reasons.append(reason)


def _midnight_et_iso(now_et: Optional[dt.datetime] = None) -> str:
    now_et = now_et or dt.datetime.now(ET)
    midnight = now_et.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight.astimezone(dt.timezone.utc).isoformat()


def daily_realized_pnl(store: SmcStateStore, now_et: Optional[dt.datetime] = None) -> float:
    return store.realized_pnl_since(_midnight_et_iso(now_et))


def consecutive_losses(store: SmcStateStore, limit_scan: int = 20) -> int:
    """Counts back from the most recent close. A break-even/winning trade resets
    the streak -- this measures a *run* of losses, not a loss rate."""
    streak = 0
    for row in store.recent_closed_ordered(limit=limit_scan):
        pnl = row["realized_pnl"]
        if pnl is not None and pnl < 0:
            streak += 1
        else:
            break
    return streak


def net_qqq_directional_contracts(store: SmcStateStore) -> int:
    """Correlated-exposure measure: all live SMC QQQ option contracts count
    toward one number regardless of strike/expiry, because every one of them is
    a leveraged bet on the same underlying in the same session. Calls and puts
    do NOT net against each other here -- holding both is two ways to lose to
    theta, not a hedge."""
    total = 0
    for pos in store.open_positions():
        qty = int(pos["filled_qty"] or 0) or int(pos["intended_qty"] or 0)
        total += abs(qty)
    return total


def check_entry_allowed(
    store: SmcStateStore, broker, config, *, occ: str, entry_premium: float,
    intended_qty: int, dashboard_db: Optional[Path] = None,
    now_et: Optional[dt.datetime] = None, schedule=None,
) -> GateResult:
    """Every pre-entry gate, evaluated together so the operator sees ALL reasons
    the strategy declined, not just the first."""
    result = GateResult(allowed=True)
    now_et = now_et or dt.datetime.now(ET)
    dashboard_db = Path(dashboard_db) if dashboard_db else (config.db_path.parent.parent / "options_eval.db")

    # --- 0. An active halt (set by reconciliation or a breaker) outranks everything.
    try:
        halt = store.active_halt()
    except SmcStateError as e:
        result.block(f"state store unreadable while checking halts ({e}) -- failing closed")
        return result
    if halt:
        result.block(f"ACTIVE HALT: {halt['reason']}")

    # --- 1. Reconciliation mismatch anywhere = no new exposure.
    mismatches = store.positions_in_mismatch()
    if mismatches:
        result.block(
            f"{len(mismatches)} position(s) in RECON_MISMATCH "
            f"(e.g. {mismatches[0]['position_id']}: {mismatches[0]['recon_note']})")

    # --- 2. Daily realized loss.
    realized = daily_realized_pnl(store, now_et)
    if realized <= -abs(config.max_daily_realized_loss):
        result.block(f"daily realized loss ${realized:.2f} hit limit "
                     f"-${abs(config.max_daily_realized_loss):.2f}")

    # --- 3. Open premium at risk (this candidate included).
    candidate_premium = entry_premium * intended_qty * 100
    open_premium = store.open_premium_at_risk()
    if open_premium + candidate_premium > config.max_open_premium_at_risk:
        result.block(
            f"open premium ${open_premium:.2f} + candidate ${candidate_premium:.2f} exceeds "
            f"${config.max_open_premium_at_risk:.2f} cap")

    # --- 4. Concurrency.
    live = store.open_positions()
    if len(live) >= config.max_concurrent_positions:
        result.block(f"{len(live)} concurrent SMC positions already open "
                     f"(max {config.max_concurrent_positions})")

    # --- 5. Correlated QQQ directional exposure.
    net_contracts = net_qqq_directional_contracts(store)
    if net_contracts + intended_qty > config.max_correlated_qqq_contracts:
        result.block(
            f"correlated QQQ exposure {net_contracts} + {intended_qty} exceeds "
            f"{config.max_correlated_qqq_contracts} contracts")

    # --- 6. Entry rate limit over a rolling window.
    window_start = (now_et - dt.timedelta(minutes=config.entry_window_minutes)) \
        .astimezone(dt.timezone.utc).isoformat()
    recent_entries = store.entries_since(window_start)
    if len(recent_entries) >= config.max_entries_per_window:
        result.block(
            f"{len(recent_entries)} entries in the last {config.entry_window_minutes}min "
            f"(max {config.max_entries_per_window}) -- rate limited")

    # --- 7. Consecutive-loss breaker.
    streak = consecutive_losses(store)
    if streak >= config.max_consecutive_losses:
        result.block(f"{streak} consecutive losing trades (max {config.max_consecutive_losses}) "
                     "-- circuit breaker open")

    # --- 8. Execution-failure breaker (broker submit/cancel failures today).
    failures = store.execution_failure_count_since(_midnight_et_iso(now_et))
    if failures >= config.max_execution_failures:
        result.block(f"{failures} execution failures today (max {config.max_execution_failures}) "
                     "-- refusing to keep firing into a broken execution path")

    # --- 9. Exact-OCC ownership across every strategy on this shared account.
    ok, reason = assert_occ_free_for_entry(store, broker, occ, dashboard_db)
    if not ok:
        result.block(f"OCC ownership: {reason}")

    # --- 10. Session schedule: don't open into a close we can't manage out of.
    schedule = schedule or smc_calendar.session_schedule(now_et.date())
    should_flatten, flatten_reason = smc_calendar.must_flatten(now_et, schedule, config)
    if should_flatten:
        result.block(f"inside the flatten window, no new entries: {flatten_reason}")

    if not result.allowed:
        store.log_event("ENTRY_BLOCKED", {"occ": occ, "reasons": result.reasons})
        logger.warning("ENTRY BLOCKED for %s: %s", occ, " | ".join(result.reasons))
    return result


def record_execution_failure(store: SmcStateStore, detail: str,
                             position_id: Optional[str] = None) -> None:
    """Feeds gate 8. Kept as its own call so a broker failure is a first-class,
    counted event rather than just a log line someone might read later."""
    store.log_event("EXECUTION_FAILURE", {"detail": detail}, position_id=position_id)
    logger.error("EXECUTION FAILURE recorded: %s", detail)


def flatten_watchdog(store: SmcStateStore, config, now_et: Optional[dt.datetime] = None,
                     schedule=None) -> tuple:
    """Independent of any per-position exit rule: (should_flatten, reason, positions).

    Deliberately separate from the target/stop/time-stop logic. Those are strategy
    rules and could in principle be misconfigured; this watchdog exists so that a
    session boundary is enforced by the CALENDAR alone, which is what the old
    hard-coded 15:30 could not do on an early-close day."""
    now_et = now_et or dt.datetime.now(ET)
    schedule = schedule or smc_calendar.session_schedule(now_et.date())
    should, reason = smc_calendar.must_flatten(now_et, schedule, config)
    positions = store.open_positions() if should else []
    if should and positions:
        logger.error("FLATTEN WATCHDOG firing on %d position(s): %s", len(positions), reason)
    return should, reason, positions
