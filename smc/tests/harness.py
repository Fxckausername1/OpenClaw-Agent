"""Test harness for SMC execution: a controllable fake broker and a fake clock.

`unittest`-based to match this codebase's existing convention
(thetadata_pipeline/tests/* are all unittest; pytest is not installed and this
box is on a no-new-paid/no-new-deps footing).

No test in this suite touches the network or places a real paper order. Every
broker behaviour that mattered in the 2026-07-31 incident -- a POST that is
accepted but times out on the client, a fill that lands during a cancel, a
market order the venue refuses, a quote that goes one-sided -- is reproducible
here on demand, because those are exactly the paths that cannot be validated by
reading the code.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import sqlite3
import tempfile
from pathlib import Path

from smc.broker import BrokerOrder, BrokerRejected, BrokerTimeout, FEED_INDICATIVE, FEED_OPRA, Quote
from smc.config import SmcConfig
from smc.state import SmcStateStore

OCC = "QQQ260731C00690000"
OTHER_OCC = "QQQ260731P00675000"

FILL_IMMEDIATE = "immediate"
FILL_NEVER = "never"
FILL_ON_CANCEL = "on_cancel"      # the fill-during-cancel race
FILL_AFTER_POLLS = "after_polls"
FILL_PARTIAL = "partial"


class FakeClock:
    """Deterministic time. `sleep()` advances the clock instead of blocking, so TTL
    and ladder loops terminate immediately and predictably."""

    def __init__(self, start: dt.datetime = None):
        self._now = start or dt.datetime(2026, 7, 31, 14, 0, 0, tzinfo=dt.timezone.utc)
        self.slept = 0.0

    def now(self) -> dt.datetime:
        return self._now

    def sleep(self, seconds: float) -> None:
        self.slept += seconds
        self._now = self._now + dt.timedelta(seconds=seconds)

    def advance(self, seconds: float) -> None:
        self._now = self._now + dt.timedelta(seconds=seconds)


@dataclasses.dataclass
class _FakeOrder:
    client_order_id: str
    broker_order_id: str
    occ: str
    side: str
    order_type: str
    qty: int
    status: str = "new"
    filled_qty: int = 0
    avg_fill_price: float = None
    polls: int = 0


class FakeBroker:
    """Implements the `Broker` protocol with switchable failure behaviour."""

    def __init__(self):
        self.orders: dict = {}
        self.positions: dict = {}
        self.quotes: dict = {}
        self._seq = 0

        # --- behaviour switches
        self.fill_policy = FILL_IMMEDIATE
        self.fill_price = 0.50
        self.partial_qty = 1
        self.fill_after_polls = 2
        self.reject_order_types: set = set()
        # 'accepted_but_timeout' = broker records the order, client sees a timeout.
        # 'lost' = nothing recorded, broker will 404 the lookup.
        self.submit_mode = "ok"
        self.lookup_timeout = False
        self.position_read_timeout = False
        self.cancel_timeout = False
        self.calendar_rows: list = []

        # --- call log for assertions
        self.submitted: list = []
        self.canceled: list = []

    # ------------------------------------------------------------- helpers
    def set_quote(self, occ, bid, ask, bid_size=50, ask_size=50, feed=FEED_OPRA):
        self.quotes[occ] = Quote(occ=occ, bid=bid, ask=ask, bid_size=bid_size,
                                  ask_size=ask_size, ts="2026-07-31T14:00:00Z", feed=feed)

    def clear_quote(self, occ):
        self.quotes.pop(occ, None)

    def set_position(self, occ, qty):
        self.positions[occ] = qty

    def buy_orders(self) -> list:
        return [p for p in self.submitted if p["side"] == "buy"]

    def sell_orders(self) -> list:
        return [p for p in self.submitted if p["side"] == "sell"]

    # --------------------------------------------------------- order flow
    def submit_order(self, payload: dict) -> BrokerOrder:
        coid = payload["client_order_id"]
        otype = payload["type"]
        self.submitted.append(dict(payload))

        if otype in self.reject_order_types:
            raise BrokerRejected(f"{otype} orders not supported for this contract")

        if self.submit_mode == "lost":
            raise BrokerTimeout("connection reset before the order reached the venue")

        self._seq += 1
        order = _FakeOrder(
            client_order_id=coid, broker_order_id=f"bkr-{self._seq}", occ=payload["symbol"],
            side=payload["side"], order_type=otype, qty=int(payload["qty"]),
        )
        self.orders[coid] = order
        self._maybe_fill(order, initial=True)

        if self.submit_mode == "accepted_but_timeout":
            # The order IS live at the broker; the client just never heard back.
            raise BrokerTimeout("read timeout after POST -- outcome unknown to the client")

        return self._to_broker_order(order)

    def _maybe_fill(self, order: _FakeOrder, initial: bool = False):
        if self.fill_policy == FILL_IMMEDIATE and initial:
            self._apply_fill(order, order.qty)
        elif self.fill_policy == FILL_PARTIAL and initial:
            self._apply_fill(order, min(self.partial_qty, order.qty), partial=True)

    def _apply_fill(self, order: _FakeOrder, qty: int, partial: bool = False):
        order.filled_qty = qty
        order.avg_fill_price = self.fill_price
        order.status = "partially_filled" if (partial and qty < order.qty) else "filled"
        delta = qty if order.side == "buy" else -qty
        self.positions[order.occ] = max(self.positions.get(order.occ, 0) + delta, 0)

    def get_order_by_client_id(self, client_order_id: str):
        if self.lookup_timeout:
            raise BrokerTimeout("order lookup unavailable")
        order = self.orders.get(client_order_id)
        if order is None:
            return None  # broker affirmatively never saw it
        order.polls += 1
        if (self.fill_policy == FILL_AFTER_POLLS and order.filled_qty == 0
                and order.polls >= self.fill_after_polls):
            self._apply_fill(order, order.qty)
        return self._to_broker_order(order)

    def cancel_order(self, broker_order_id: str) -> None:
        self.canceled.append(broker_order_id)
        if self.cancel_timeout:
            raise BrokerTimeout("cancel request timed out")
        for order in self.orders.values():
            if order.broker_order_id != broker_order_id:
                continue
            if order.status in ("filled", "canceled", "rejected", "expired"):
                return
            if self.fill_policy == FILL_ON_CANCEL:
                # THE RACE: the fill lands while we are cancelling.
                self._apply_fill(order, order.qty)
            else:
                order.status = "canceled"

    def _to_broker_order(self, order: _FakeOrder) -> BrokerOrder:
        return BrokerOrder(
            broker_order_id=order.broker_order_id, client_order_id=order.client_order_id,
            occ=order.occ, status=order.status, filled_qty=order.filled_qty,
            intended_qty=order.qty, avg_fill_price=order.avg_fill_price, raw={},
        )

    # --------------------------------------------------------- positions
    def list_option_positions(self) -> list:
        if self.position_read_timeout:
            raise BrokerTimeout("position read unavailable")
        return [{"symbol": occ, "qty": str(q), "asset_class": "us_option"}
                for occ, q in self.positions.items() if q != 0]

    def get_position_qty(self, occ: str) -> int:
        if self.position_read_timeout:
            raise BrokerTimeout("position read unavailable")
        return int(self.positions.get(occ, 0))

    def list_open_orders(self) -> list:
        return [self._to_broker_order(o) for o in self.orders.values()
                if o.status in ("new", "accepted", "partially_filled")]

    def get_quote(self, occ: str):
        return self.quotes.get(occ)

    def get_calendar(self, start, end) -> list:
        return self.calendar_rows


# ------------------------------------------------------------------ builders

def make_tmpdir() -> Path:
    return Path(tempfile.mkdtemp(prefix="smc_test_"))


def make_config(tmpdir: Path, **overrides) -> SmcConfig:
    """Lifecycle-focused defaults: risk limits deliberately loose so lifecycle
    behaviour is what's under test. The risk tests set their own tight values."""
    base = dict(
        db_path=tmpdir / "smc_state.db",
        entry_ttl_seconds=5.0,
        entry_poll_seconds=1.0,
        max_signal_age_seconds=45.0,
        urgent_poll_seconds=0.5,
        urgent_attempt_timeout_seconds=2.0,
        urgent_max_attempts=4,
        notifications_enabled=False,
        max_daily_realized_loss=10_000.0,
        max_open_premium_at_risk=10_000.0,
        max_concurrent_positions=10,
        max_correlated_qqq_contracts=50,
        max_entries_per_window=50,
        max_consecutive_losses=99,
        max_execution_failures=99,
    )
    base.update(overrides)
    return SmcConfig(**base)


def make_store(config: SmcConfig) -> SmcStateStore:
    return SmcStateStore(config.db_path)


def make_dashboard(tmpdir: Path) -> Path:
    """A minimal shared dashboard ledger, so cross-strategy ownership checks have
    something real (and read-only) to consult."""
    db = tmpdir / "options_eval.db"
    conn = sqlite3.connect(db)
    conn.execute("""CREATE TABLE trades_ledger (
        trade_id TEXT PRIMARY KEY, strategy_id TEXT NOT NULL, entry_time INTEGER,
        exit_time INTEGER, legs_metadata TEXT NOT NULL, regime_tag TEXT,
        initial_risk REAL, realized_pnl REAL, r_multiple REAL, status TEXT NOT NULL)""")
    conn.commit()
    conn.close()
    return db


def add_foreign_position(dashboard_db: Path, strategy_id: str, occ: str,
                         status: str = "OPEN") -> None:
    conn = sqlite3.connect(dashboard_db)
    conn.execute(
        "INSERT INTO trades_ledger(trade_id, strategy_id, entry_time, legs_metadata, "
        "regime_tag, initial_risk, status) VALUES(?,?,?,?,?,?,?)",
        (f"{strategy_id}-{occ}", strategy_id, 0,
         json.dumps({"legs": [{"occ": occ, "side": "BUY"}]}), "UNKNOWN", 100.0, status),
    )
    conn.commit()
    conn.close()


def make_trigger(signal_key="2026-07-31:1950:long", signal_ts=None, side="long"):
    return {
        "signal_key": signal_key,
        "signal_ts": signal_ts or "2026-07-31T14:00:00+00:00",
        "detected_at": "2026-07-31T14:00:01+00:00",
        "ticker": "QQQ", "side": side, "trigger": "PULLBACK", "score": 5.5,
        "session": "2026-07-31", "bar_index": 1950,
    }


def make_contract(occ=OCC, ask=0.52, right="C", qty=1):
    return {"occ": occ, "ask": ask, "right": right, "qty": qty,
            "strike": 690.0, "expiration": "2026-07-31"}


def write_calendar_cache(tmpdir_workspace_data: Path, sessions: dict) -> None:
    """Seeds smc.calendar's on-disk cache so calendar tests need no network."""
    path = tmpdir_workspace_data / "market_calendar_cache.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"sessions": sessions}))


def assert_no_unaccounted_exposure(testcase, store, broker) -> None:
    """The single invariant this whole repair exists to guarantee: no broker
    position may exist without a recoverable, non-terminal SMC state record."""
    for occ, qty in broker.positions.items():
        if qty == 0:
            continue
        claimed = store.occ_owned_by_smc(occ)
        testcase.assertTrue(claimed, (
            f"INVARIANT VIOLATED: broker holds {qty} of {occ} with no non-terminal SMC "
            f"state record -- the orphaned-position bug has reappeared"))
