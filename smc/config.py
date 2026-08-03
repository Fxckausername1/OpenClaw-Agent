"""SMC execution configuration -- every operational limit and policy switch
for the live (paper) HEFF-SMC triangle strategy, in ONE place so a reviewer can
audit the whole risk surface without reading the execution code.

Defaults are deliberately CONSERVATIVE for paper mode. Nothing here tunes the
STRATEGY (indicator params, score threshold, premium band, delta floor, target,
stop are all untouched by this module and live in their own already-validated
homes) -- this file only governs EXECUTION safety and risk containment.

Live-money gate: `mode` must be PAPER for the urgent market-order liquidation
path to be usable at all. Enabling it for real money requires BOTH
mode=LIVE_APPROVED and allow_live_market_orders=True, which is deliberately two
independent switches rather than one, because a market order on an illiquid
option chain is a genuinely different risk in real money than in paper.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import os
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
ROOT = Path(__file__).resolve().parent.parent  # smc/ lives under the workspace root

MODE_PAPER = "PAPER"
MODE_LIVE_APPROVED = "LIVE_APPROVED"

STRATEGY_ID = "SMC_TRIANGLE"

# Urgent-exit execution policies (Phase 3).
URGENT_MARKET = "MARKET"              # broker market order (paper default)
URGENT_LIMIT_LADDER = "LIMIT_LADDER"  # progressively more aggressive marketable limits


@dataclasses.dataclass(frozen=True)
class SmcConfig:
    # ---------------------------------------------------------------- mode
    mode: str = MODE_PAPER
    # Hard block: paper market orders are fine, real-money market orders are not
    # enabled by this repair. Both this AND mode=LIVE_APPROVED are required.
    allow_live_market_orders: bool = False

    # ------------------------------------------------------ state / storage
    db_path: Path = ROOT / "data" / "live_heff_smc" / "smc_state.db"

    # -------------------------------------------------- entry lifecycle (P2)
    # How long an entry limit order may rest before we cancel it. The old code
    # used time_in_force=day with no TTL, so an entry could fill many minutes
    # after its 1-minute signal bar had gone stale.
    entry_ttl_seconds: float = 20.0
    # Poll cadence while waiting for the entry to fill or reach TTL.
    entry_poll_seconds: float = 0.5
    # A signal older than this at submission time is REJECTED outright rather
    # than traded late. Real measurement 2026-07-31: signal->submit latency was
    # 227.5-407.3s (median 352.85s) because of three chained */3 cron hops,
    # against a backtest that assumes 3s. Phase 4 collapses the pipeline; this
    # is the backstop that refuses to trade a stale edge even if it regresses.
    max_signal_age_seconds: float = 45.0

    # --------------------------------------------------- urgent exits (P3)
    urgent_exit_policy: str = URGENT_MARKET
    # Ladder fallback, used when a market order is rejected/unsupported. Each
    # rung crosses further through the bid. Applied to the CURRENT quote each
    # attempt, never to a stale one.
    urgent_ladder_offsets: tuple = (0.0, 0.05, 0.12, 0.25, 0.50)
    urgent_poll_seconds: float = 0.4        # sub-second supervision, not the 2-min cron
    urgent_attempt_timeout_seconds: float = 3.0  # wait for terminal state per attempt
    urgent_max_attempts: int = 6
    # TARGET exits stay a plain limit at the bid -- they are not urgent and
    # every one on 2026-07-31 filled immediately at/near the quote.
    target_exit_uses_limit: bool = True

    # ------------------------------------------------------- supervision
    supervisor_poll_seconds: float = 1.0
    # Flatten-everything watchdog margin before the session close (uses the REAL
    # calendar close, including early closes -- not a hard-coded 15:30).
    forced_close_buffer_minutes: int = 30
    early_close_flatten_buffer_minutes: int = 15

    # ------------------------------------------------------ risk gates (P6)
    max_daily_realized_loss: float = 150.0     # dollars, absolute value
    max_open_premium_at_risk: float = 300.0    # dollars of premium simultaneously open
    max_concurrent_positions: int = 2
    max_correlated_qqq_contracts: int = 2      # net directional QQQ option contracts
    max_entries_per_window: int = 3
    entry_window_minutes: int = 30
    max_consecutive_losses: int = 3
    max_execution_failures: int = 3            # broker submit/cancel failures before halt

    # ------------------------------------------------------- notifications
    telegram_target: str = "7590346809"
    notifications_enabled: bool = True
    telegram_timeout_seconds: float = 5.0      # short: never delays risk management

    # ---------------------------------------------------------- data source
    # Alpaca's `indicative` options feed is NOT real NBBO/OPRA. Anything derived
    # from it must be labelled as indicative, never described as NBBO.
    preferred_quote_feed: str = "opra"
    # Never make target/stop decisions from a quote older than this.
    max_quote_age_seconds: float = 10.0
    fallback_quote_feed: str = "indicative"

    # ------------------------------------------------- subscription breadth
    # How much of the chain to actually STREAM. Subscribing to the whole
    # discovered chain (1,170 contracts on 2026-08-03) saturated this
    # single-core box during RTH: keepalive pings timed out, the socket
    # reconnected 212 times, 0/1170 subscriptions were ever acknowledged, and
    # the detector ran 12-18s against a 900ms ceiling. Nothing could trade.
    #
    # Narrowing costs no real candidates. Variant B buys the tightest spread
    # under a $100 debit cap -- roughly <=$1.00 premium -- so deep ITM (too
    # expensive) and far OTM (too wide) contracts are never selectable. These
    # bounds keep everything that could be bought and drop what could not.
    #
    # Both bounds apply; whichever is tighter wins. Set strikes_per_side to 0
    # to disable narrowing entirely and stream the full discovered chain.
    subscription_strikes_per_side: int = 25   # nearest N strikes each side of spot
    subscription_band_pct: float = 0.04       # ...and never beyond +/-4% of spot

    # PAPER-ONLY: permit an indicative quote to price an ENTRY when OPRA is
    # unavailable. Measured 2026-08-01: this account returns
    #   HTTP 403 {"message":"OPRA agreement is not signed"}
    # so requiring OPRA made entry structurally impossible -- 4/4 liquid
    # near-money QQQ contracts were blocked at the gate. Fail-closed, but it
    # blocked everything, including the shadow session meant to validate the
    # rest of the pipeline.
    #
    # Why this is a defensible loosening rather than reopening the old wound:
    # the 2026-07-31 loss came from EXIT limits resting unfilled against a
    # lagging indicative bid. An ENTRY limit priced off a stale indicative ask
    # simply does not fill, and the 20s TTL cancels it -- the cost is a missed
    # signal, not an unmanaged position. Urgent exits now use market orders and
    # need no quote at all. So the strict rule was protecting the one case that
    # no longer needs it.
    #
    # Deliberately NOT applicable to live money: market_orders_permitted()-style
    # two-condition gating via entry_quote_policy() below. Freshness is still
    # enforced on every quote regardless of feed -- staleness is a separate risk
    # from provenance, and this flag does not relax it.
    allow_indicative_entry_quotes_in_paper: bool = True

    # How often supervision re-reconciles against the broker. Reconciliation is
    # what catches orphans, so this is a throttle, not a removal: it used to run
    # on EVERY supervision pass, measured at 3 broker calls/pass, which at 1Hz
    # with 2 open positions is ~360 req/min against Alpaca's ~200/min ceiling --
    # i.e. the protective path would start erroring exactly when it matters.
    # 15s still reconciles 8x more often than the 2-minute cron it replaced.
    reconcile_interval_seconds: float = 15.0

    # How long a negative OPRA probe is trusted before re-probing. Caching the
    # negative result stops every quote paying a doomed 403 round-trip in the
    # sub-second urgent-exit loop; the TTL means a newly-signed OPRA agreement
    # is picked up automatically without a restart.
    opra_reprobe_seconds: float = 600.0

    def __post_init__(self) -> None:
        if self.mode not in (MODE_PAPER, MODE_LIVE_APPROVED):
            raise ValueError(f"invalid SMC mode: {self.mode!r}")
        if self.urgent_exit_policy not in (URGENT_MARKET, URGENT_LIMIT_LADDER):
            raise ValueError(f"invalid urgent_exit_policy: {self.urgent_exit_policy!r}")
        positive = (
            "entry_ttl_seconds", "entry_poll_seconds", "urgent_poll_seconds",
            "urgent_attempt_timeout_seconds", "urgent_max_attempts",
            "supervisor_poll_seconds", "max_quote_age_seconds",
            "reconcile_interval_seconds", "opra_reprobe_seconds",
        )
        for name in positive:
            if float(getattr(self, name)) <= 0:
                raise ValueError(f"{name} must be > 0")
        nonnegative = (
            "max_signal_age_seconds", "max_daily_realized_loss",
            "max_open_premium_at_risk", "max_concurrent_positions",
            "max_correlated_qqq_contracts", "max_entries_per_window",
        )
        for name in nonnegative:
            if float(getattr(self, name)) < 0:
                raise ValueError(f"{name} must be >= 0")
        if (not self.urgent_ladder_offsets or
                any(not 0 <= float(x) < 1 for x in self.urgent_ladder_offsets)):
            raise ValueError("urgent_ladder_offsets must be non-empty values in [0, 1)")

    @property
    def is_paper(self) -> bool:
        return self.mode == MODE_PAPER

    def entry_quote_policy(self) -> tuple:
        """(indicative_allowed, reason). Mirrors market_orders_permitted()'s
        two-condition shape: a loosening must be BOTH explicitly enabled AND in
        paper mode. Live money always requires real OPRA/NBBO for an entry."""
        if not self.is_paper:
            return False, (
                f"mode={self.mode}: live entries require real OPRA/NBBO; the indicative "
                "allowance is paper-only and cannot be enabled for real money here")
        if not self.allow_indicative_entry_quotes_in_paper:
            return False, "paper mode but allow_indicative_entry_quotes_in_paper=False"
        return True, ("paper mode with indicative entry pricing explicitly allowed "
                      "(OPRA not entitled on this account)")

    def market_orders_permitted(self) -> tuple:
        """(permitted, reason). Paper may use market orders. Real money needs
        two explicit switches; this repair ships with them off."""
        if self.is_paper:
            return True, "paper mode"
        if self.mode == MODE_LIVE_APPROVED and self.allow_live_market_orders:
            return True, "live mode with explicit market-order approval"
        return False, (
            f"market orders BLOCKED: mode={self.mode} "
            f"allow_live_market_orders={self.allow_live_market_orders} -- real-money "
            "market orders require both mode=LIVE_APPROVED and "
            "allow_live_market_orders=True (deliberate two-switch gate)"
        )


def load_config() -> SmcConfig:
    """Env-overridable so a canary run can tighten limits without a code edit.
    Only whitelisted scalar fields are overridable; the live-money switches are
    deliberately NOT env-overridable -- flipping those must be a reviewed code
    change, not an environment variable someone exports by accident."""
    overrides = {}
    numeric_fields = {
        "max_quote_age_seconds": float, "reconcile_interval_seconds": float,
        "opra_reprobe_seconds": float,
        "entry_ttl_seconds": float, "max_signal_age_seconds": float,
        "max_daily_realized_loss": float, "max_open_premium_at_risk": float,
        "max_concurrent_positions": int, "max_correlated_qqq_contracts": int,
        "max_entries_per_window": int, "entry_window_minutes": int,
        "max_consecutive_losses": int, "max_execution_failures": int,
        "urgent_poll_seconds": float, "supervisor_poll_seconds": float,
        "subscription_strikes_per_side": int, "subscription_band_pct": float,
    }
    for field, caster in numeric_fields.items():
        raw = os.environ.get(f"SMC_{field.upper()}")
        if raw:
            try:
                overrides[field] = caster(raw)
            except ValueError as e:
                raise ValueError(
                    f"invalid SMC_{field.upper()}={raw!r}; refusing unsafe fallback") from e
    return SmcConfig(**overrides)
