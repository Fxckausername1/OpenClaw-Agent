"""Phase 4: collapse the detector -> selector -> executor pipeline into ONE
event-driven pass, and supervise open positions at a real cadence.

The measured problem. On 2026-07-31 the three stages were three separate cron
entries, each on `*/3`:

    */3  live_heff_smc_detector_wrapper.sh    (writes triggers.jsonl)
    */3  live_heff_smc_selector_wrapper.sh    (reads it, writes decisions.jsonl)
    */3  live_heff_smc_executor_wrapper.sh    (reads that, submits)

A signal therefore waited for up to three independent cron boundaries before an
order was sent. Real measured signal-to-submit latency that day: 227.5s to
407.3s, median 352.85s. The backtest that justified trading this strategy assumes
a 3.0s reaction latency (bt2_fills.FillConfig.reaction_latency_seconds). The live
system was thus executing a materially different strategy from the validated one
-- ~117x the modelled delay -- which is not a tuning discrepancy but a different
experiment.

This module runs detection, selection, gating and submission in a single process
with no waiting in between, records every timestamp in the latency chain, and
refuses any signal that is still too old by the time it reaches submission.

Exit supervision runs at `supervisor_poll_seconds` (1s default) and urgent exits
poll sub-second, rather than discovering a breached stop on the next 2-minute
cron tick.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import logging
import time
from pathlib import Path
from typing import Callable, Optional
from zoneinfo import ZoneInfo

from . import calendar as smc_calendar
from . import notify
from .broker import Broker, BrokerTimeout
from .lifecycle import (
    Clock, EXIT_TARGET, ExitOutcome, decide_exit, execute_exit, signal_age_seconds, submit_entry,
)
from .reconcile import reconcile
from .risk import check_entry_allowed, flatten_watchdog, record_execution_failure
from .state import (
    CLOSED, OPEN, PARTIAL, RECON_MISMATCH, SmcStateError, SmcStateStore,
)

ET = ZoneInfo("America/New_York")
logger = logging.getLogger("smc.pipeline")

def _quote_is_fresh(quote, now: dt.datetime, max_age_seconds: float) -> bool:
    if quote is None or not quote.ts:
        return False
    try:
        parsed = dt.datetime.fromisoformat(str(quote.ts).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
    except (TypeError, ValueError):
        return False
    age = (now - parsed).total_seconds()
    return -2.0 <= age <= max_age_seconds

def _fresh_entry_quote(broker, occ: str, now: dt.datetime, config):
    """(quote, reject_reason). Fails closed on anything unusable.

    Provenance rule (2026-08-01): OPRA/NBBO is required for an entry UNLESS
    config.entry_quote_policy() permits indicative -- which is paper-only and
    exists because this account has no OPRA entitlement (403 "OPRA agreement is
    not signed"), a condition that otherwise blocks 100% of entries. Freshness
    and two-sidedness are enforced regardless of feed: provenance and staleness
    are separate risks and the allowance relaxes only the former.

    Returns the quote actually used, so the caller can record WHICH feed priced
    the entry rather than leaving it implicit."""
    max_age_seconds = config.max_quote_age_seconds
    try:
        quote = broker.get_quote(occ)
    except Exception as exc:  # noqa: BLE001 -- entry must fail closed
        return None, f"entry quote read failed: {exc}"
    if quote is None:
        return None, "no entry quote"
    if not quote.is_nbbo:
        allowed, why = config.entry_quote_policy()
        if not allowed:
            return None, f"entry quote is {quote.label}; fresh OPRA is required ({why})"
    if not quote.two_sided:
        return None, f"entry quote ({quote.label}) is not valid and two-sided"
    if not _quote_is_fresh(quote, now, max_age_seconds):
        return None, (f"entry quote ({quote.label}) is missing a fresh timestamp "
                      f"(max {max_age_seconds:.1f}s)")
    return quote, None



@dataclasses.dataclass
class CycleReport:
    reconciled: bool = False
    halted: bool = False
    halt_reason: str = ""
    supervision_unavailable: bool = False
    signals_seen: int = 0
    signals_stale: int = 0
    signals_no_contract: int = 0
    signals_gated: int = 0
    entries_submitted: int = 0
    entries_filled: int = 0
    exits_executed: int = 0
    latencies: list = dataclasses.field(default_factory=list)
    notes: list = dataclasses.field(default_factory=list)
    # Per-signal latency legs, in the vocabulary the re-arm gate asks for.
    # One dict per signal that reached the quote stage:
    #   signal_to_quote_s      -- bar close -> entry quote in hand
    #   quote_to_decision_s    -- quote in hand -> submit/gate decision made
    #   decision_to_ack_s      -- submit issued -> broker acknowledgement
    #   signal_to_ack_s        -- end to end
    # Kept as raw samples, not just an aggregate, so the shadow session yields a
    # real distribution to compare against the backtest's frozen 3.0s assumption.
    latency_legs: list = dataclasses.field(default_factory=list)
    entry_quote_feeds: dict = dataclasses.field(default_factory=dict)

    @staticmethod
    def _dist(samples: list) -> Optional[dict]:
        if not samples:
            return None
        ordered = sorted(samples)
        n = len(ordered)
        return {
            "n": n, "min": ordered[0], "median": ordered[n // 2],
            "p90": ordered[min(int(n * 0.9), n - 1)], "max": ordered[-1],
            "mean": round(sum(ordered) / n, 3),
        }

    def as_dict(self) -> dict:
        d = dataclasses.asdict(self)
        if self.latencies:
            ordered = sorted(self.latencies)
            d["latency_median_seconds"] = ordered[len(ordered) // 2]
            d["latency_max_seconds"] = ordered[-1]
        for leg in ("signal_to_quote_s", "quote_to_decision_s",
                     "decision_to_ack_s", "signal_to_ack_s"):
            dist = self._dist([x[leg] for x in self.latency_legs if x.get(leg) is not None])
            if dist:
                d[f"latency_{leg}"] = dist
        return d


class SmcPipeline:
    """`detect_fn()` -> list of trigger dicts (each with at least signal_key,
    session, bar_index, side, ticker, time, trigger, score, detected_at).
    `select_fn(trigger)` -> contract dict or None.

    Both are injected so tests exercise the whole lifecycle with zero network and
    zero dependency on the replay engine."""

    def __init__(self, store: SmcStateStore, broker: Broker, config,
                 detect_fn: Callable, select_fn: Callable,
                 exit_config, dashboard_sync_fn: Optional[Callable] = None,
                 dashboard_db: Optional[Path] = None, clock: Optional[Clock] = None):
        self.store = store
        self.broker = broker
        self.config = config
        # Monotonic timestamp of the last completed reconciliation, for the
        # supervision throttle. None = never reconciled, so the first pass always
        # reconciles (a restart must never begin by trusting local state).
        self._last_reconcile_at: Optional[float] = None
        self.detect_fn = detect_fn
        self.select_fn = select_fn
        self.exit_config = exit_config
        self.dashboard_sync_fn = dashboard_sync_fn
        self.dashboard_db = dashboard_db
        self.clock = clock or Clock()

    # ------------------------------------------------------------- startup
    def _reconcile_due(self) -> bool:
        """Throttle for supervision-driven reconciliation.

        Reconciliation is what catches orphans, so this throttles rather than
        removes it, and the FIRST pass always reconciles. Measured 2026-08-01:
        reconciling on every supervision pass costs 3 broker calls/pass, which at
        the 1Hz supervisor cadence with 2 open positions is ~360 req/min against
        Alpaca's ~200/min ceiling -- the protective path would begin erroring
        under exactly the load it exists to handle. At the 15s default this still
        reconciles 8x more often than the 2-minute cron it replaced.

        Note this only gates the SUPERVISION path. run_signal_cycle() calls
        startup() unconditionally, so no entry is ever submitted against
        unreconciled state."""
        if self._last_reconcile_at is None:
            return True
        return (time.monotonic() - self._last_reconcile_at) >= self.config.reconcile_interval_seconds

    def startup(self) -> CycleReport:
        """Reconcile against the broker BEFORE doing anything else. A restart must
        never begin by assuming local state is complete -- that assumption is what
        allows a broker position to exist with no supervision."""
        report = CycleReport()
        try:
            result = reconcile(self.store, self.broker, self.config, self.dashboard_db)
        except Exception as e:  # noqa: BLE001 -- startup ambiguity must fail closed
            reason = f"startup reconciliation failed: {e}"
            logger.exception("STARTUP RECONCILIATION FAILED -- entries halted: %s", e)
            report.halted = True
            report.halt_reason = reason
            try:
                self.store.set_halt(reason)
            except Exception:  # noqa: BLE001 -- original failure remains primary
                logger.exception("could not durably record startup halt")
            return report
        report.reconciled = True
        self._last_reconcile_at = time.monotonic()
        if result.adopted:
            report.notes.append(f"adopted {len(result.adopted)} unrecorded broker fill(s)")
        if not result.clean:
            report.halted = True
            report.halt_reason = "; ".join(result.halt_reasons[:3])
            logger.error("STARTUP RECONCILIATION NOT CLEAN -- entries halted: %s",
                         report.halt_reason)
        return report

    # -------------------------------------------------- signal -> submission
    def run_signal_cycle(self, *, arm: bool = False) -> CycleReport:
        """Detection -> selection -> gate -> submission, with no cron hop between
        stages. Every stage timestamp is preserved on the position row."""
        report = self.startup()
        if report.halted:
            return report


        try:
            triggers = self.detect_fn() or []
        except Exception as e:  # noqa: BLE001 -- detection failure must not kill supervision
            logger.exception("detection failed: %s", e)
            report.notes.append(f"detection failed: {e}")
            return report

        report.signals_seen = len(triggers)
        # Time comes from the injected clock, never wall-clock: the risk gate and the
        # flatten window are both time-dependent, so a direct datetime.now() here
        # would make gating untestable AND inconsistent with the supervisor (which
        # does use the clock). Caught by
        # test_dashboard_failure_is_recorded_but_not_fatal_on_entry.
        now_et = self.clock.now().astimezone(ET)
        schedule = smc_calendar.session_schedule(now_et.date())

        for trig in triggers:
            signal_key = trig["signal_key"]
            selected_ts = None

            # --- reject stale BEFORE spending an API call on chain selection.
            age = signal_age_seconds(trig["signal_ts"], self.clock.now())
            if age > self.config.max_signal_age_seconds:
                report.signals_stale += 1
                self.store.log_event("SIGNAL_REJECTED_STALE",
                                      {"signal_key": signal_key, "age_seconds": round(age, 2)})
                logger.warning("signal %s rejected as stale (%.1fs)", signal_key, age)
                continue

            try:
                contract = self.select_fn(trig)
            except Exception as e:  # noqa: BLE001
                logger.exception("selection failed for %s: %s", signal_key, e)
                report.notes.append(f"selection failed for {signal_key}: {e}")
                continue
            selected_ts = self.clock.now().isoformat()

            if not contract:
                report.signals_no_contract += 1
                self.store.log_event("NO_CONTRACT", {"signal_key": signal_key})
                continue

            occ = contract["occ"]
            qty = int(contract.get("qty", 1))

            entry_quote, quote_reject = _fresh_entry_quote(
                self.broker, occ, self.clock.now(), self.config)
            quote_at = self.clock.now()
            leg = {
                "signal_key": signal_key, "occ": occ,
                "signal_to_quote_s": round(signal_age_seconds(trig["signal_ts"], quote_at), 3),
                "quote_to_decision_s": None, "decision_to_ack_s": None,
                "signal_to_ack_s": None,
                "quote_feed": entry_quote.feed if entry_quote else None,
            }
            if quote_reject:
                report.signals_gated += 1
                report.latency_legs.append(leg)
                self.store.log_event("ENTRY_REJECTED_QUOTE",
                                     {"signal_key": signal_key, "occ": occ, "reason": quote_reject})
                report.notes.append(f"{signal_key} gated: {quote_reject}")
                continue
            # Record which feed actually priced entries -- with OPRA unentitled this
            # should read 'indicative', and a silent switch either way is material.
            report.entry_quote_feeds[entry_quote.feed] = (
                report.entry_quote_feeds.get(entry_quote.feed, 0) + 1)
            entry_premium = float(entry_quote.ask)

            gate = check_entry_allowed(
                self.store, self.broker, self.config, occ=occ, entry_premium=entry_premium,
                intended_qty=qty, dashboard_db=self.dashboard_db, now_et=now_et, schedule=schedule,
            )
            leg["quote_to_decision_s"] = round(
                (self.clock.now() - quote_at).total_seconds(), 3)
            if not gate.allowed:
                report.signals_gated += 1
                report.latency_legs.append(leg)
                report.notes.append(f"{signal_key} gated: {gate.reasons[0]}")
                continue

            decision_at = self.clock.now()

            outcome = submit_entry(
                self.store, self.broker, self.config, signal_key=signal_key, occ=occ,
                underlying=trig.get("ticker", "QQQ"), contract_right=contract["right"],
                signal_side=trig["side"], intended_qty=qty, limit_price=entry_premium,
                signal_ts=trig["signal_ts"], detected_ts=trig.get("detected_at"),
                selected_ts=selected_ts, trigger_kind=trig.get("trigger"),
                trigger_score=trig.get("score"), arm=arm, clock=self.clock,
            )
            report.entries_submitted += 1
            acked_at = self.clock.now()
            submit_latency = signal_age_seconds(trig["signal_ts"], acked_at)
            report.latencies.append(round(submit_latency, 2))
            # decision->ack covers intent commit + POST + broker acknowledgement,
            # i.e. everything submit_entry does before it knows the order landed.
            leg["decision_to_ack_s"] = round((acked_at - decision_at).total_seconds(), 3)
            leg["signal_to_ack_s"] = round(submit_latency, 3)
            leg["outcome_state"] = outcome.state
            report.latency_legs.append(leg)
            self.store.log_event("ENTRY_LATENCY", leg,
                                 position_id=outcome.position_id,
                                 client_order_id=outcome.client_order_id)

            if outcome.is_open:
                report.entries_filled += 1
                # State is already committed by submit_entry. Reporting comes AFTER,
                # and neither of these can block or unwind risk management.
                self._sync_dashboard(outcome.position_id, outcome, occ)
                notify.notify_after_commit(True, (
                    f"SMC ENTRY (paper): {occ}\n"
                    f"{trig['side'].upper()} {trig.get('trigger')} score={trig.get('score')}\n"
                    f"qty={outcome.filled_qty} @ ${outcome.fill_price:.2f}\n"
                    f"signal->submit {submit_latency:.1f}s"
                ), self.config)
            else:
                report.notes.append(f"{signal_key}: {outcome.state} -- {outcome.reason}")

        return report

    def _sync_dashboard(self, position_id, outcome, occ) -> None:
        """Dashboard writes are REPORTING ONLY. Wrapped so a failure here can never
        propagate into the trading path -- the 2026-07-30 code did its dashboard
        insert inline with position bookkeeping, so a ledger error could interleave
        with control-plane truth."""
        if self.dashboard_sync_fn is None:
            return
        try:
            self.dashboard_sync_fn(position_id=position_id, outcome=outcome, occ=occ)
            self.store.mark_dashboard_synced(position_id, True)
        except Exception as e:  # noqa: BLE001
            self.store.mark_dashboard_synced(position_id, False, str(e))
            logger.error("dashboard sync failed for %s (REPORTING ONLY, trading unaffected): %s",
                         position_id, e)

    # ------------------------------------------------------------ supervision
    def supervise_once(self, *, arm: bool = False) -> CycleReport:
        """One protective pass over every open position. Deliberately does NOT
        depend on the entry path or the risk gate -- a halted strategy must still
        manage the positions it already has."""
        report = self.startup() if self._reconcile_due() else CycleReport()
        now = self.clock.now()
        now_et = now.astimezone(ET)
        schedule = smc_calendar.session_schedule(now_et.date())

        # Independent EOD/early-close watchdog, calendar-driven.
        should_flatten, flatten_reason, _ = flatten_watchdog(
            self.store, self.config, now_et=now_et, schedule=schedule)

        try:
            positions = self.store.open_positions()
        except SmcStateError as e:
            # Never interpret a broken read as "nothing to supervise".
            logger.critical("CANNOT READ OPEN POSITIONS (%s) -- halting entries and alerting. "
                            "Any real position is currently unsupervised.", e)
            self.store.set_halt(f"state unreadable during supervision: {e}")
            report.halted = True
            report.halt_reason = str(e)
            report.supervision_unavailable = True
            return report

        for pos in positions:
            occ = pos["occ"]
            entry_price = pos["entry_fill_price"]
            if entry_price is None:
                continue  # not filled yet; the entry lifecycle owns it
            if self.store.has_unresolved_entry_order(pos["position_id"]):
                # Reconciliation has requested cancel but cannot yet prove the
                # entry terminal. An exit could race a late buy fill and oversell.
                self.store.log_event("SUPERVISION_WAITING_FOR_ENTRY_CANCEL",
                                     {"occ": occ}, position_id=pos["position_id"])
                report.notes.append(
                    f"{occ}: entry cancel unresolved; no exit replacement submitted")
                continue
            if self.store.has_unresolved_exit_order(pos["position_id"]):
                # A pre-restart exit may still fill. Reconciliation must first
                # cancel/confirm it; a replacement here could oversell.
                self.store.log_event("SUPERVISION_WAITING_FOR_EXIT_CANCEL",
                                     {"occ": occ}, position_id=pos["position_id"])
                report.notes.append(
                    f"{occ}: prior exit unresolved; no replacement submitted")
                continue

            quote = None
            try:
                quote = self.broker.get_quote(occ)
            except Exception as e:  # noqa: BLE001
                logger.warning("supervision quote failed for %s: %s", occ, e)

            reason = None
            if should_flatten:
                reason = "FORCED_CLOSE"
                report.notes.append(f"{occ}: watchdog flatten -- {flatten_reason}")
            elif (quote is None or not quote.two_sided or
                  not _quote_is_fresh(quote, now, self.config.max_quote_age_seconds)):
                # A missing/one-sided/zero-bid quote is itself informative: we cannot
                # evaluate target/stop, so we say so rather than silently skipping.
                self.store.log_event("SUPERVISION_NO_USABLE_QUOTE",
                                      {"occ": occ, "quote": str(quote)},
                                      position_id=pos["position_id"])
                report.notes.append(f"{occ}: no usable/fresh quote this pass")
                continue
            else:
                opened_at = dt.datetime.fromisoformat(
                    str(pos["entry_filled_ts"] or pos["intent_ts"]).replace("Z", "+00:00"))
                if opened_at.tzinfo is None:
                    opened_at = opened_at.replace(tzinfo=dt.timezone.utc)
                reason = decide_exit(float(entry_price), quote, opened_at, now,
                                      self.exit_config, schedule, self.config)

            if reason is None:
                continue

            outcome = execute_exit(self.store, self.broker, self.config, pos, reason,
                                    arm=arm, clock=self.clock)
            report.exits_executed += 1
            if outcome.state == CLOSED and outcome.realized_pnl is not None:
                pct = ((outcome.exit_fill_price / float(entry_price) - 1) * 100
                       if entry_price else 0.0)
                notify.notify_after_commit(True, (
                    f"SMC EXIT (paper): {occ}\n"
                    f"{reason} via {outcome.used_policy} — entry ${float(entry_price):.2f} -> "
                    f"exit ${outcome.exit_fill_price:.2f} ({pct:+.1f}%)\n"
                    f"Realized: ${outcome.realized_pnl:+.2f} on {outcome.closed_qty} contract(s)"
                ), self.config)
            elif outcome.state == RECON_MISMATCH:
                notify.notify_after_commit(True, (
                    f"SMC EXIT PROBLEM: {occ}\n{outcome.reason}\n"
                    "Entries halted. Position may still be open -- check the broker."
                ), self.config)

        return report

    def supervise_for(self, seconds: float, *, arm: bool = False) -> CycleReport:
        """Continuous supervision for a bounded window, at the configured cadence.
        Bounded rather than infinite so it stays cron-compatible on this box (no
        daemons -- the established pattern) while still being sub-cron responsive."""
        deadline = self.clock.now() + dt.timedelta(seconds=seconds)
        merged = CycleReport()
        while self.clock.now() < deadline:
            r = self.supervise_once(arm=arm)
            merged.exits_executed += r.exits_executed
            merged.notes.extend(r.notes)
            merged.reconciled = merged.reconciled or r.reconciled
            merged.supervision_unavailable = r.supervision_unavailable
            if r.halted:
                merged.halted, merged.halt_reason = True, r.halt_reason
            if r.supervision_unavailable:
                break
            self.clock.sleep(self.config.supervisor_poll_seconds)
        return merged
