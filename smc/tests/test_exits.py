"""Urgent-exit scenarios (required tests 10, 11-close, 12, 13, 16).

The 2026-07-31 failure these guard against: a STOP exit priced as a limit at a
LAGGING indicative bid rested unfilled while the market fell, retried 6 times over
10 minutes, and realised roughly 3x the intended -20% stop. The tests below prove
the replacement path actually liquidates -- including when the venue refuses a
market order, when the quote is unusable, and when the fill lands during a cancel.
"""

from __future__ import annotations

import datetime as dt
import shutil
import unittest

from smc.broker import FEED_INDICATIVE, FEED_OPRA
from smc.config import URGENT_LIMIT_LADDER, URGENT_MARKET, MODE_LIVE_APPROVED
from smc.lifecycle import (
    EXIT_FORCED_CLOSE, EXIT_STOP, EXIT_TARGET, EXIT_TIME_STOP, decide_exit, execute_exit,
)
from smc.reconcile import reconcile
from smc.state import CLOSED, OPEN, PARTIAL, RECON_MISMATCH
from smc.tests.harness import (
    FILL_IMMEDIATE, FILL_NEVER, FILL_ON_CANCEL, FILL_PARTIAL, OCC, OTHER_OCC,
    FakeBroker, FakeClock, make_config, make_dashboard, make_store, make_tmpdir,
)
from thetadata_pipeline.bt2_exits import ExitConfig


class ExitTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = make_tmpdir()
        self.config = make_config(self.tmp)
        self.store = make_store(self.config)
        self.broker = FakeBroker()
        self.clock = FakeClock()
        self.exit_config = ExitConfig()
        self.dashboard = make_dashboard(self.tmp)

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _open_position(self, occ=OCC, qty=1, entry_price=0.80, signal_key="s:1:long"):
        """Puts a real OPEN position in the store and at the broker.

        The broker_order_id must be unique per position -- smc_orders enforces
        UNIQUE(broker_order_id), and an earlier version of this helper reused a
        literal id, which the constraint correctly rejected."""
        intent = self.store.create_entry_intent(
            signal_key=signal_key, occ=occ, underlying="QQQ", contract_right="C",
            signal_side="long", intended_qty=qty, limit_price=entry_price,
            order_type="limit", signal_ts="2026-07-31T14:00:00+00:00")
        self.store.mark_order_submitted(intent["client_order_id"])
        self.store.record_broker_ack(intent["client_order_id"],
                                      f"bkr-entry-{intent['position_id']}")
        self.store.record_order_fill(intent["client_order_id"], qty, entry_price, OPEN)
        self.store.record_entry_filled(intent["position_id"], qty, entry_price, True)
        self.broker.set_position(occ, qty)
        return self.store.get_position(intent["position_id"])


class UrgentExitTest(ExitTestBase):
    # ----------------------------------------------------------- scenario 13
    def test_urgent_stop_uses_market_order_in_paper(self):
        """Paper default: a real market order, which is marketable by construction --
        unlike a limit derived from a possibly-stale indicative quote."""
        pos = self._open_position()
        self.broker.set_quote(OCC, bid=0.60, ask=0.65)
        self.broker.fill_policy = FILL_IMMEDIATE
        self.broker.fill_price = 0.60

        outcome = execute_exit(self.store, self.broker, self.config, pos, EXIT_STOP,
                               arm=True, clock=self.clock)

        self.assertEqual(outcome.state, CLOSED)
        self.assertEqual(self.broker.sell_orders()[0]["type"], "market")
        self.assertEqual(self.broker.positions[OCC], 0)

    def test_market_rejection_falls_back_to_limit_ladder_and_still_liquidates(self):
        """The venue refuses market orders for this contract. The exit must NOT give
        up -- it escalates through marketable limits until it is flat."""
        pos = self._open_position()
        self.broker.set_quote(OCC, bid=0.60, ask=0.65)
        self.broker.reject_order_types = {"market"}
        self.broker.fill_policy = FILL_IMMEDIATE
        self.broker.fill_price = 0.57

        outcome = execute_exit(self.store, self.broker, self.config, pos, EXIT_STOP,
                               arm=True, clock=self.clock)

        self.assertEqual(outcome.state, CLOSED, "must liquidate via the ladder after rejection")
        types = [o["type"] for o in self.broker.sell_orders()]
        self.assertIn("market", types, "should have attempted a market order first")
        self.assertIn("limit", types, "should have fallen back to a limit")
        self.assertEqual(self.broker.positions[OCC], 0)

    def test_live_money_market_orders_are_hard_blocked(self):
        """The two-switch gate. mode=LIVE_APPROVED alone must NOT unlock market
        orders -- allow_live_market_orders must also be set, and this repair ships
        with it off."""
        live_cfg = make_config(self.tmp, mode=MODE_LIVE_APPROVED,
                               allow_live_market_orders=False)
        permitted, reason = live_cfg.market_orders_permitted()
        self.assertFalse(permitted)
        self.assertIn("BLOCKED", reason)

        pos = self._open_position()
        self.broker.set_quote(OCC, bid=0.60, ask=0.65)
        self.broker.fill_policy = FILL_IMMEDIATE
        outcome = execute_exit(self.store, self.broker, live_cfg, pos, EXIT_STOP,
                               arm=True, clock=self.clock)
        self.assertNotIn("market", [o["type"] for o in self.broker.sell_orders()],
                         "a live-money market order must never be sent")
        self.assertEqual(outcome.state, CLOSED)

    def test_ladder_offsets_escalate_and_never_replace_a_live_order(self):
        """Each rung crosses deeper, and a new rung is only sent after the prior
        order reached a terminal state -- so two exits can never race."""
        pos = self._open_position()
        self.broker.set_quote(OCC, bid=1.00, ask=1.05)
        self.broker.reject_order_types = {"market"}
        self.broker.fill_policy = FILL_NEVER

        execute_exit(self.store, self.broker, self.config, pos, EXIT_STOP,
                     arm=True, clock=self.clock)

        limits = [float(o["limit_price"]) for o in self.broker.sell_orders()
                  if o["type"] == "limit"]
        self.assertGreater(len(limits), 1, "ladder should have escalated")
        self.assertTrue(all(b <= a for a, b in zip(limits, limits[1:])),
                        f"ladder must get MORE aggressive, got {limits}")
        # Every attempt was cancelled/terminal before the next was sent.
        self.assertEqual(len(self.broker.canceled), len(limits) - 0 - 1 + 1,
                         "each non-terminal attempt must be cancelled before escalating")

    # ----------------------------------------------------------- scenario 10
    def test_exit_fill_during_cancel_is_captured(self):
        """The exit-side race: our ladder attempt times out, we cancel, and the fill
        lands anyway. That fill must be recorded, not lost."""
        pos = self._open_position()
        self.broker.set_quote(OCC, bid=0.60, ask=0.65)
        self.broker.reject_order_types = {"market"}
        self.broker.fill_policy = FILL_ON_CANCEL
        self.broker.fill_price = 0.58

        outcome = execute_exit(self.store, self.broker, self.config, pos, EXIT_STOP,
                               arm=True, clock=self.clock)

        self.assertEqual(outcome.state, CLOSED,
                         "a fill landing during the exit cancel must be captured")
        self.assertEqual(self.broker.positions[OCC], 0)
        self.assertEqual(outcome.closed_qty, 1)

    # ----------------------------------------------------------- scenario 11
    def test_partial_close_continues_until_flat(self):
        """A partial close leaves real residual exposure. The exit loop must keep
        going, sized off the RE-READ broker quantity each pass."""
        pos = self._open_position(qty=3, entry_price=0.80)
        self.broker.set_quote(OCC, bid=0.60, ask=0.65)
        self.broker.reject_order_types = {"market"}
        self.broker.fill_policy = FILL_PARTIAL
        self.broker.partial_qty = 1
        self.broker.fill_price = 0.60

        outcome = execute_exit(self.store, self.broker, self.config, pos, EXIT_STOP,
                               arm=True, clock=self.clock)

        self.assertEqual(self.broker.positions[OCC], 0, "must keep working until flat")
        self.assertEqual(outcome.state, CLOSED)
        self.assertEqual(outcome.closed_qty, 3)
        sizes = [int(o["qty"]) for o in self.broker.sell_orders() if o["type"] == "limit"]
        self.assertEqual(sizes, [3, 2, 1],
                         f"each attempt must size off the re-read remaining qty, got {sizes}")

    def test_exit_never_uses_a_hardcoded_quantity_of_one(self):
        """Regression guard for the old `qty=1` assumption: a 4-lot position must
        submit a 4-lot exit."""
        pos = self._open_position(qty=4)
        self.broker.set_quote(OCC, bid=0.60, ask=0.65)
        self.broker.fill_policy = FILL_IMMEDIATE
        execute_exit(self.store, self.broker, self.config, pos, EXIT_STOP,
                     arm=True, clock=self.clock)
        self.assertEqual(int(self.broker.sell_orders()[0]["qty"]), 4)

    # ----------------------------------------------------------- scenario 12
    def test_zero_bid_quote_still_liquidates_via_market_order(self):
        """A zero/one-sided bid makes a limit unpriceable. Rather than stalling (the
        old behaviour), the exit escalates to a market order."""
        pos = self._open_position()
        self.broker.set_quote(OCC, bid=0.0, ask=0.05)
        self.broker.fill_policy = FILL_IMMEDIATE
        self.broker.fill_price = 0.01

        outcome = execute_exit(self.store, self.broker, self.config, pos, EXIT_STOP,
                               arm=True, clock=self.clock)
        self.assertEqual(outcome.state, CLOSED)
        self.assertEqual(self.broker.sell_orders()[0]["type"], "market")

    def test_missing_quote_does_not_block_an_urgent_exit(self):
        pos = self._open_position()
        self.broker.clear_quote(OCC)
        self.broker.fill_policy = FILL_IMMEDIATE
        outcome = execute_exit(self.store, self.broker, self.config, pos, EXIT_STOP,
                               arm=True, clock=self.clock)
        self.assertEqual(outcome.state, CLOSED,
                         "a stop must not be abandoned just because a quote is missing")

    def test_decide_exit_returns_none_on_unusable_quote(self):
        """We must not fabricate a target/stop decision from a missing quote."""
        self.assertIsNone(decide_exit(0.80, None, self.clock.now(), self.clock.now(),
                                       self.exit_config, _regular_schedule(), self.config))

    def test_indicative_quote_is_labelled_and_not_called_nbbo(self):
        from smc.broker import Quote
        ind = Quote(OCC, 0.5, 0.52, 10, 10, "t", FEED_INDICATIVE)
        opra = Quote(OCC, 0.5, 0.52, 10, 10, "t", FEED_OPRA)
        self.assertFalse(ind.is_nbbo)
        self.assertIn("not NBBO", ind.label)
        self.assertTrue(opra.is_nbbo)
        self.assertIn("NBBO", opra.label)

    # ----------------------------------------------------------- scenario 16
    def test_multiple_simultaneous_stops_all_liquidate(self):
        """Two positions breach at once. Both must be closed, each sized to its own
        real broker quantity -- no cross-contamination."""
        pos_a = self._open_position(occ=OCC, qty=1, signal_key="a:1:long")
        pos_b = self._open_position(occ=OTHER_OCC, qty=2, signal_key="b:1:short")
        self.broker.set_quote(OCC, bid=0.60, ask=0.65)
        self.broker.set_quote(OTHER_OCC, bid=0.60, ask=0.65)
        self.broker.fill_policy = FILL_IMMEDIATE
        self.broker.fill_price = 0.60

        out_a = execute_exit(self.store, self.broker, self.config, pos_a, EXIT_STOP,
                             arm=True, clock=self.clock)
        out_b = execute_exit(self.store, self.broker, self.config, pos_b, EXIT_STOP,
                             arm=True, clock=self.clock)

        self.assertEqual(out_a.state, CLOSED)
        self.assertEqual(out_b.state, CLOSED)
        self.assertEqual(self.broker.positions[OCC], 0)
        self.assertEqual(self.broker.positions[OTHER_OCC], 0)
        by_symbol = {o["symbol"]: int(o["qty"]) for o in self.broker.sell_orders()}
        self.assertEqual(by_symbol[OCC], 1)
        self.assertEqual(by_symbol[OTHER_OCC], 2)

    def test_restart_recovers_exit_fill_and_closes_from_durable_order_evidence(self):
        pos = self._open_position(entry_price=0.80)
        intent = self.store.create_exit_intent(
            position_id=pos["position_id"], occ=OCC, intended_qty=1,
            order_type="market", limit_price=None, exit_reason=EXIT_STOP,
        )
        self.store.mark_order_submitted(intent["client_order_id"])
        self.broker.fill_policy = FILL_IMMEDIATE
        self.broker.fill_price = 0.60
        self.broker.submit_order({
            "symbol": OCC, "qty": "1", "side": "sell", "type": "market",
            "time_in_force": "day", "client_order_id": intent["client_order_id"]})
        reconcile(self.store, self.broker, self.config, self.dashboard)
        refreshed = self.store.get_position(pos["position_id"])
        self.assertEqual(refreshed["state"], CLOSED)
        self.assertAlmostEqual(refreshed["realized_pnl"], -20.0, places=2)
        self.assertFalse(self.store.unresolved_exit_orders())


    # ------------------------------------------------------------- targets
    def test_target_exit_uses_a_limit_not_a_market_order(self):
        """TARGET is not urgent; every one on 2026-07-31 filled at the quote. Paying
        the spread on a winner for no reason would be a real cost."""
        pos = self._open_position()
        self.broker.set_quote(OCC, bid=1.05, ask=1.10)
        self.broker.fill_policy = FILL_IMMEDIATE
        self.broker.fill_price = 1.05
        outcome = execute_exit(self.store, self.broker, self.config, pos, EXIT_TARGET,
                               arm=True, clock=self.clock)
        self.assertEqual(outcome.state, CLOSED)
        self.assertEqual(self.broker.sell_orders()[0]["type"], "limit")

    def test_unfilled_target_cancel_returns_to_open_without_false_mismatch(self):
        pos = self._open_position()
        self.broker.set_quote(OCC, bid=1.05, ask=1.10)
        self.broker.fill_policy = FILL_NEVER
        outcome = execute_exit(self.store, self.broker, self.config, pos, EXIT_TARGET,
                               arm=True, clock=self.clock)
        self.assertEqual(outcome.state, OPEN)
        self.assertEqual(self.store.get_position(pos["position_id"])["state"], OPEN)
        self.assertEqual(self.store.positions_in_mismatch(), [])
        self.assertEqual(len(self.broker.sell_orders()), 1)

    def test_unconfirmed_cancel_never_submits_a_replacement_exit(self):
        pos = self._open_position()
        self.broker.set_quote(OCC, bid=0.60, ask=0.65)
        self.broker.reject_order_types = {"market"}
        self.broker.fill_policy = FILL_NEVER
        self.broker.cancel_timeout = True
        outcome = execute_exit(self.store, self.broker, self.config, pos, EXIT_STOP,
                               arm=True, clock=self.clock)
        limits = [order for order in self.broker.sell_orders()
                  if order["type"] == "limit"]
        self.assertEqual(outcome.state, RECON_MISMATCH)
        self.assertEqual(len(limits), 1, "must not race an unconfirmed prior exit")
        last = self.store.orders_for_position(pos["position_id"], role="EXIT")[-1]
        self.assertIsNone(last["terminal_ts"])

    def test_exit_flags_mismatch_when_broker_qty_unreadable(self):
        """We must never guess a size. If broker quantity can't be established the
        position is flagged, entries halt, and supervision continues."""
        pos = self._open_position()
        self.broker.position_read_timeout = True
        outcome = execute_exit(self.store, self.broker, self.config, pos, EXIT_STOP,
                               arm=True, clock=self.clock)
        self.assertEqual(outcome.state, RECON_MISMATCH)
        self.assertFalse(self.broker.submitted, "must not submit an exit of unknown size")

    def test_exit_on_already_flat_broker_flags_mismatch(self):
        """Local thinks open, broker is flat: that is a real disagreement and must
        be surfaced, not silently closed as if all were well."""
        pos = self._open_position(qty=1)
        self.broker.set_position(OCC, 0)
        outcome = execute_exit(self.store, self.broker, self.config, pos, EXIT_STOP,
                               arm=True, clock=self.clock)
        self.assertEqual(outcome.state, RECON_MISMATCH)

    def test_dry_run_exit_submits_nothing(self):
        pos = self._open_position()
        self.broker.set_quote(OCC, bid=0.60, ask=0.65)
        execute_exit(self.store, self.broker, self.config, pos, EXIT_STOP,
                     arm=False, clock=self.clock)
        self.assertFalse(self.broker.submitted)


class DecideExitRuleTest(ExitTestBase):
    """The exit RULE itself is unchanged from the validated backtest -- these lock
    the thresholds so a future edit can't silently retune them."""

    def test_target_and_stop_match_validated_backtest_defaults(self):
        self.assertEqual(self.exit_config.target_return, 0.25)
        self.assertEqual(self.exit_config.premium_stop_pct, -0.20)
        self.assertEqual(self.exit_config.time_stop_minutes, 30)

    def test_target_fires_at_plus_25_percent(self):
        q = _quote(bid=1.00)
        self.assertEqual(decide_exit(0.80, q, self.clock.now(), self.clock.now(),
                                      self.exit_config, _regular_schedule(), self.config),
                         EXIT_TARGET)

    def test_stop_fires_at_minus_20_percent(self):
        q = _quote(bid=0.64)
        self.assertEqual(decide_exit(0.80, q, self.clock.now(), self.clock.now(),
                                      self.exit_config, _regular_schedule(), self.config),
                         EXIT_STOP)

    def test_time_stop_only_when_no_progress(self):
        opened = self.clock.now()
        later = opened + dt.timedelta(minutes=31)
        self.assertEqual(decide_exit(0.80, _quote(bid=0.79), opened, later,
                                      self.exit_config, _regular_schedule(), self.config),
                         EXIT_TIME_STOP)
        self.assertIsNone(decide_exit(0.80, _quote(bid=0.90), opened, later,
                                       self.exit_config, _regular_schedule(), self.config))

    def test_no_exit_inside_bands(self):
        self.assertIsNone(decide_exit(0.80, _quote(bid=0.85), self.clock.now(),
                                       self.clock.now(), self.exit_config,
                                       _regular_schedule(), self.config))


def _quote(bid, ask=None):
    from smc.broker import Quote
    return Quote(OCC, bid, ask if ask is not None else bid + 0.02, 50, 50,
                 "2026-07-31T14:00:00Z", FEED_OPRA)


def _regular_schedule():
    from smc.calendar import SessionSchedule
    from zoneinfo import ZoneInfo
    et = ZoneInfo("America/New_York")
    d = dt.date(2026, 7, 31)
    return SessionSchedule(
        session_date=d, is_trading_day=True,
        open_et=dt.datetime.combine(d, dt.time(9, 30), tzinfo=et),
        close_et=dt.datetime.combine(d, dt.time(16, 0), tzinfo=et),
        is_early_close=False, degraded=False, source="test",
    )


if __name__ == "__main__":
    unittest.main()
