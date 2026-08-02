"""ONE canonical admission rule shared by the live executor and the backtest.

Why this module exists. On 2026-07-31 the live strategy took three entries on the
SAME contract inside 30 minutes (QQQ260731C00690000), which the OCC-keyed JSON
control plane silently collapsed into one tracked record, orphaning a live
position. The live fix forbids that outright (smc.reconcile.assert_occ_free_for_entry
plus UNIQUE(signal_key) in the SMC state store).

But the BACKTEST that justified trading this strategy has no such rule: it walks
1,779 signals and simulates each one independently, so it happily models several
simultaneous positions on the same contract, and unlimited concurrency. That means
the backtest measures a strategy the live system is not allowed to run -- and the
direction of the bias is not knowable a priori, so it isn't safely ignorable.

`AdmissionPolicy` is the single definition of "may this signal open a position?"
Both sides call it, so a change to the rule cannot drift between them, and
`smc/tests/test_pnl_and_parity.py` asserts they agree signal-for-signal.

This module holds NO strategy parameters (no thresholds, bands, or deltas) -- only
the execution-admission rules that already exist in the live risk gates.
"""

from __future__ import annotations

import dataclasses
from typing import Optional

REJECT_SAME_CONTRACT = "SAME_CONTRACT_ALREADY_OPEN"
REJECT_MAX_CONCURRENT = "MAX_CONCURRENT_POSITIONS"
REJECT_MAX_CORRELATED = "MAX_CORRELATED_CONTRACTS"
ADMIT = "ADMIT"


@dataclasses.dataclass(frozen=True)
class AdmissionPolicy:
    """Mirrors the live SmcConfig gates that affect WHETHER a position may open.
    Defaults intentionally match smc.config.SmcConfig's own defaults."""
    max_concurrent_positions: int = 2
    max_correlated_contracts: int = 2
    block_same_contract: bool = True


@dataclasses.dataclass
class OpenBook:
    """Minimal model of currently-open exposure, shared shape for both sides."""
    occs: dict = dataclasses.field(default_factory=dict)  # occ -> contracts open

    def open_count(self) -> int:
        return len([o for o, q in self.occs.items() if q > 0])

    def total_contracts(self) -> int:
        return sum(q for q in self.occs.values() if q > 0)

    def holds(self, occ: str) -> bool:
        return self.occs.get(occ, 0) > 0

    def add(self, occ: str, qty: int) -> None:
        self.occs[occ] = self.occs.get(occ, 0) + qty

    def remove(self, occ: str, qty: Optional[int] = None) -> None:
        if occ not in self.occs:
            return
        if qty is None:
            self.occs.pop(occ, None)
        else:
            self.occs[occ] = max(self.occs[occ] - qty, 0)
            if self.occs[occ] == 0:
                self.occs.pop(occ, None)


def admit(book: OpenBook, occ: str, qty: int,
          policy: AdmissionPolicy = AdmissionPolicy()) -> tuple:
    """(decision, reason). `decision` is ADMIT or a REJECT_* constant.

    Evaluation order matches the live risk gate's order of severity: an exact-OCC
    conflict is the hard structural one (it is what actually corrupted state), then
    concurrency, then aggregate correlated size."""
    if policy.block_same_contract and book.holds(occ):
        return REJECT_SAME_CONTRACT, f"already holding {occ}"
    if book.open_count() >= policy.max_concurrent_positions:
        return (REJECT_MAX_CONCURRENT,
                f"{book.open_count()} open >= max {policy.max_concurrent_positions}")
    if book.total_contracts() + qty > policy.max_correlated_contracts:
        return (REJECT_MAX_CORRELATED,
                f"{book.total_contracts()}+{qty} > max {policy.max_correlated_contracts}")
    return ADMIT, "ok"
