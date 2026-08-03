"""SMC execution package -- the durable control plane, order lifecycle, risk
gates, and reconciliation for the live (paper) HEFF-SMC triangle strategy.

Built 2026-07-31 as a P0 repair after a real paper-trading session lost $101
across 10 trades with two structural defects: an OCC-keyed JSON control plane
that silently orphaned a live position, and urgent stop exits priced off a
lagging indicative quote that rested unfilled while the market ran away.

The strategy itself -- indicator, score threshold, premium band, delta floor,
target, stop -- is deliberately NOT touched by this package. This is execution
safety only.

Nothing here arms itself. `arm=False` is the default on every order-placing path.
"""

from .config import SmcConfig, load_config  # noqa: F401
from .state import SmcStateStore, SmcStateError, DuplicateSignal  # noqa: F401

__all__ = [
    "SmcConfig", "load_config", "SmcStateStore", "SmcStateError", "DuplicateSignal",
]
