"""Stream-driven exits: ThetaData quote -> decide_exit -> immediate submit.

Replaces the 2-minute exit-manager cron. That cadence is what turned a -20%
stop rule into realised -26%, -31%, -46%, -53% and -66% outcomes on
2026-07-31: the rule was never wrong, the observation interval was. Here a
quote arriving on the stream is evaluated the moment it lands.

THE HOT PATH, and what is deliberately NOT on it:

    quote event -> decide_exit -> persist intent -> submit

Nothing else. No cron tick, no REST quote poll, no notification call, no
dashboard write sits between detecting a threshold crossing and the broker
POST. Notification and dashboard work happen AFTER submission, through the
durable outbox, precisely because on 07-31 a synchronous Telegram call
blocked the executor for 150 seconds at a time.

The frozen thresholds are untouched: target, stop, time-stop and
forced-close all come from bt2_exits.ExitConfig via lifecycle.decide_exit,
which this module calls rather than reimplements. It contains no threshold
of its own -- there is nothing here to accidentally tune.

Two rules inherited from the postmortem:

  * NEVER re-price an unfilled exit on a schedule. The 07-31 manager
    cancelled exits that missed a 2-second fill window and resubmitted them
    ~2 minutes later at the new, lower bid, walking two stops to -66% and
    -42%. An outstanding exit is MANAGED CONTINUOUSLY here (via
    trade_updates), not re-laddered by a timer.
  * A stale quote must not trigger an exit. max_quote_age_seconds is
    enforced against the stream's own monotonic receipt clock, not a wall
    clock that can jump.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import logging
import time
from typing import Callable, Optional

from smc.broker import FEED_THETADATA, Quote
from smc.lifecycle import EXIT_STOP, EXIT_TARGET, URGENT_REASONS, decide_exit

logger = logging.getLogger("smc.exit_monitor")


def theta_to_quote(sq) -> Optional[Quote]:
    """Adapts a ThetaData StreamQuote to the Quote shape lifecycle expects.
    Labelled FEED_THETADATA (a real NBBO feed, see broker.NBBO_FEEDS) so the
    record always shows WHICH provider priced the decision."""
    if sq is None:
        return None
    return Quote(occ=sq.occ, bid=sq.bid, ask=sq.ask,
                 bid_size=sq.bid_size, ask_size=sq.ask_size,
                 ts=sq.exchange_ts.isoformat() if sq.exchange_ts else None,
                 feed=FEED_THETADATA)


@dataclasses.dataclass
class ExitDecisionRecord:
    """Full provenance for one exit decision, including the latency chain the
    Monday gate needs to measure."""
    position_id: str
    occ: str
    reason: str
    entry_fill_price: float
    quote_bid: Optional[float]
    quote_ask: Optional[float]
    quote_age_seconds: Optional[float]
    quote_exchange_ts: Optional[str]
    quote_generation: Optional[int]
    decided_ts: str
    decide_latency_ms: float          # quote receipt -> decision
    submit_latency_ms: Optional[float] = None   # decision -> broker ack
    client_order_id: Optional[str] = None
    submitted: bool = False
    submit_error: Optional[str] = None

    @property
    def total_latency_ms(self) -> Optional[float]:
        if self.submit_latency_ms is None:
            return None
        return round(self.decide_latency_ms + self.submit_latency_ms, 3)


class StreamingExitMonitor:
    """Evaluates open positions against every incoming ThetaData quote.

    `submit_exit(position, reason, quote)` is injected: it must persist the
    exit intent and submit, in that order, and return a client_order_id. The
    monitor never talks to the broker directly, so the ordering guarantee
    lives in one place."""

    def __init__(self, *, open_positions: Callable, submit_exit: Callable,
                 exit_config, schedule_for: Callable, config,
                 clock: Callable = time.monotonic):
        self.open_positions = open_positions
        self.submit_exit = submit_exit
        self.exit_config = exit_config
        self.schedule_for = schedule_for
        self.config = config
        self.clock = clock
        self.decisions: list = []
        self.suppressed_stale = 0
        self.suppressed_inflight = 0
        self._inflight: set = set()      # position_ids with an exit already out

    # ------------------------------------------------------------ hot path
    def on_quote(self, stream_quote) -> Optional[ExitDecisionRecord]:
        """THE hot path. Called directly from the quote event handler."""
        if stream_quote is None:
            return None
        t0 = self.clock()
        occ = getattr(stream_quote, "occ", None)
        if occ is None:
            return None

        for position in self.open_positions():
            if position.get("occ") != occ:
                continue
            pid = position.get("position_id")
            if pid in self._inflight:
                # An exit is already working. Do NOT submit another, and do
                # NOT re-price it on a timer -- that is the 07-31 ladder.
                self.suppressed_inflight += 1
                continue

            age = stream_quote.age_seconds()
            max_age = float(getattr(self.config, "max_quote_age_seconds", 10.0))
            if age is None or age > max_age:
                self.suppressed_stale += 1
                logger.warning("exit check skipped for %s: quote age %.2fs > %.2fs",
                               occ, age if age is not None else -1, max_age)
                continue

            quote = theta_to_quote(stream_quote)
            now = dt.datetime.now(dt.timezone.utc)
            reason = decide_exit(
                entry_fill_price=float(position["entry_fill_price"]),
                quote=quote, opened_at=position["opened_at"], now=now,
                exit_config=self.exit_config,
                schedule=self.schedule_for(now), config=self.config)
            if not reason:
                continue

            decide_ms = (self.clock() - t0) * 1000.0
            rec = ExitDecisionRecord(
                position_id=pid, occ=occ, reason=reason,
                entry_fill_price=float(position["entry_fill_price"]),
                quote_bid=quote.bid, quote_ask=quote.ask,
                quote_age_seconds=round(age, 4),
                quote_exchange_ts=quote.ts,
                quote_generation=getattr(stream_quote, "generation", None),
                decided_ts=now.isoformat(), decide_latency_ms=round(decide_ms, 3))

            # SUBMIT IMMEDIATELY. Nothing between the decision and this call.
            t1 = self.clock()
            try:
                coid = self.submit_exit(position, reason, quote)
                rec.client_order_id = coid
                rec.submitted = coid is not None
            except Exception as e:  # noqa: BLE001 -- record and keep managing
                rec.submit_error = repr(e)
                logger.exception("exit submit failed for %s: %s", pid, e)
            rec.submit_latency_ms = round((self.clock() - t1) * 1000.0, 3)
            if rec.submitted:
                self._inflight.add(pid)
            self.decisions.append(rec)
            return rec
        return None

    # ------------------------------------------------------- order updates
    def on_exit_terminal(self, position_id: str) -> None:
        """Called when an exit order reaches a terminal state. Clearing
        in-flight here (rather than on a timer) is what lets a genuinely
        cancelled or rejected exit be retried on the NEXT quote, without ever
        creating a re-pricing ladder."""
        self._inflight.discard(position_id)

    def urgent_reasons(self) -> tuple:
        return URGENT_REASONS

    def health(self) -> dict:
        submitted = [d for d in self.decisions if d.submitted]
        return {
            "decisions": len(self.decisions),
            "submitted": len(submitted),
            "inflight": len(self._inflight),
            "suppressed_stale_quote": self.suppressed_stale,
            "suppressed_inflight": self.suppressed_inflight,
            "max_decide_latency_ms": max((d.decide_latency_ms for d in self.decisions),
                                         default=0.0),
            "max_submit_latency_ms": max((d.submit_latency_ms or 0.0
                                          for d in self.decisions), default=0.0),
            "by_reason": {r: sum(1 for d in self.decisions if d.reason == r)
                          for r in (EXIT_TARGET, EXIT_STOP, "TIME_STOP", "FORCED_CLOSE")},
        }
