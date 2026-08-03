"""Tri-state readiness model.

Replaces a boolean gate set that produced a real false-green: connected
validate reported every gate true and entries_permitted=True while NO live
ThetaData option quote had ever arrived and the live quote parser was
unverified. A connected socket was being read as quote readiness. They are
different claims and now have different gates.

STATUSES. Only PASS permits entries. The others exist so a red gate can say
WHY without being silently equivalent to a pass:

    PASS          verified now, in this process, from real data
    RED           genuinely failed or not satisfied
    MARKET_CLOSED healthy but not exercisable right now (weekend/after hours)
    NOT_TESTABLE  cannot be established in this mode (offline validate)
    SKIPPED       deliberately not attempted

MARKET_CLOSED and NOT_TESTABLE are NOT passes. A weekend validate therefore
reports honest amber on the live-quote gates and entries_permitted stays
false, which is exactly what the previous model got wrong.

THE THETADATA SPLIT, and why each gate is separate:

    theta_terminal_authenticated  MDDS+FPSS logged in -- says nothing about
                                  the WebSocket
    theta_stream_connected        socket up AND subscriptions acked -- says
                                  nothing about any message arriving
    theta_quote_parser_verified   a REAL message passed schema/unit/contract/
                                  timestamp checks. Synthetic tests can never
                                  set this; only a live message can.
    theta_live_quotes_fresh       a required candidate got a quote during the
                                  CURRENT stream generation, passing
                                  provenance and staleness. A REST snapshot
                                  does not satisfy it, and a pre-reconnect
                                  quote does not either.
    universe_greeks_warm          positive rows, required fields, Greek ages
                                  inside ceiling, valid expirations only
    candidate_universe_ready      enough contracts have BOTH a fresh stream
                                  quote and a fresh Greek

NOTIFICATIONS ARE A TRADING PRECONDITION, not a convenience. An entry whose
fill, stop and exit cannot be announced is an entry nobody can supervise, so
`notifications_operational` blocks NEW entries whenever the notify worker is
dead, the durable outbox is unreadable, the Telegram breaker is open, or a
critical obligation has been undelivered past its ceiling. It does NOT stop
the daemon and does NOT stop an existing position from being managed: the
gate exists so the system waits loudly instead of trading silently.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
from typing import Optional

PASS = "PASS"
RED = "RED"
MARKET_CLOSED = "MARKET_CLOSED"
NOT_TESTABLE = "NOT_TESTABLE"
SKIPPED = "SKIPPED"

# Only this permits entries. Everything else blocks.
PASSING = frozenset({PASS})
NON_BLOCKING_EXPLANATIONS = frozenset({MARKET_CLOSED, NOT_TESTABLE, SKIPPED})

GATES = (
    "singleton",
    "state_recovered",
    "notifications_operational",
    "theta_terminal_authenticated",
    "theta_stream_connected",
    "theta_quote_parser_verified",
    "theta_live_quotes_fresh",
    "universe_greeks_warm",
    "candidate_universe_ready",
    "broker_prewarmed",
    "trade_updates",
    "reconciled",
    "detector_synced",
)

# Gates that can only be satisfied by live market data. During a
# market-closed run these are expected to be MARKET_CLOSED, never PASS.
LIVE_DATA_GATES = frozenset({
    "theta_quote_parser_verified",
    "theta_live_quotes_fresh",
    "candidate_universe_ready",
})


@dataclasses.dataclass
class Gate:
    name: str
    status: str = RED
    reason: str = "not evaluated"
    evidence: Optional[dict] = None
    updated_ts: Optional[str] = None

    @property
    def passing(self) -> bool:
        return self.status in PASSING

    def as_dict(self) -> dict:
        return dataclasses.asdict(self)


class Readiness:
    def __init__(self):
        self.gates = {g: Gate(name=g) for g in GATES}

    def set(self, name: str, status: str, reason: str = "",
            evidence: Optional[dict] = None) -> None:
        if name not in self.gates:
            raise KeyError(f"unknown readiness gate {name!r}")
        if status not in (PASS, RED, MARKET_CLOSED, NOT_TESTABLE, SKIPPED):
            raise ValueError(f"invalid gate status {status!r}")
        self.gates[name] = Gate(
            name=name, status=status,
            reason=reason or ("" if status == PASS else status.lower()),
            evidence=evidence,
            updated_ts=dt.datetime.now(dt.timezone.utc).isoformat())

    def set_bool(self, name: str, ok: bool, reason: str = "",
                 evidence: Optional[dict] = None) -> None:
        """Convenience for gates that really are binary."""
        self.set(name, PASS if ok else RED, reason, evidence)

    # --------------------------------------------------------- decisions
    @property
    def entries_permitted(self) -> bool:
        """EVERY gate must be PASS. MARKET_CLOSED and NOT_TESTABLE are
        explanations, not permissions -- treating them as passes is the
        defect this model exists to prevent."""
        return all(g.passing for g in self.gates.values())

    @property
    def degraded(self) -> bool:
        return not self.entries_permitted

    def blocking(self) -> list:
        return [n for n, g in self.gates.items() if not g.passing]

    def by_status(self, status: str) -> list:
        return [n for n, g in self.gates.items() if g.status == status]

    @property
    def live_data_unverified(self) -> list:
        """Live gates that are not PASS -- the ones a market-closed run can
        never satisfy. Surfaced separately so a report can say 'amber because
        the market is closed' rather than implying a fault."""
        return [n for n in LIVE_DATA_GATES if not self.gates[n].passing]

    def as_dict(self) -> dict:
        return {
            "entries_permitted": self.entries_permitted,
            "degraded": self.degraded,
            "gates": {n: g.as_dict() for n, g in self.gates.items()},
            "statuses": {n: g.status for n, g in self.gates.items()},
            "blocking": self.blocking(),
            "reasons": {n: g.reason for n, g in self.gates.items()
                        if not g.passing},
            "market_closed_gates": self.by_status(MARKET_CLOSED),
            "not_testable_gates": self.by_status(NOT_TESTABLE),
            "live_data_unverified": self.live_data_unverified,
        }

    def summary_line(self) -> str:
        counts = {}
        for g in self.gates.values():
            counts[g.status] = counts.get(g.status, 0) + 1
        parts = ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
        return (f"entries_permitted={self.entries_permitted} ({parts}); "
                f"blocking={self.blocking()}")
