"""Entry-lifecycle failure scenarios (required tests 1, 2, 5, 8, 9, 11, 14).

Every test asserts the property that actually protects money: after the scenario,
no broker position exists without a recoverable SMC state record, and no state
record claims exposure the broker doesn't have.
"""

from __future__ import annotations

import datetime as dt
import shutil
import unittest

from smc.lifecycle import submit_entry
from smc.reconcile import reconcile
from smc.state import CANCELED, OPEN, PARTIAL, SUBMITTED, SmcStateStore
from smc.tests.harness import (
    FILL_AFTER_POLLS, FILL_IMMEDIATE, FILL_NEVER, FILL_ON_CANCEL, FILL_PARTIAL, OCC,
    FakeBroker, FakeClock, assert_no_unaccounted_exposure, make_config, make_dashboard,
    make_store, make_tmpdir,
)


class EntryLifecycleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = make_tmpdir()
        self.config = make_config(self.tmp)
        self.store = make_store(self.config)
        self.broker = FakeBroker()
        self.broker.set_quote(OCC, bid=0.50, ask=0.52)
        self.clock = FakeClock()
        self.dashboard = make_dashboard(self.tmp)

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _submit(self, qty=1, signal_key="2026-07-31:1950:long",
                signal_ts="2026-07-31T14:00:00+00:00", store=None, arm=True):
        return submit_entry(
            store or self.store, self.broker, self.config, signal_key=signal_key, occ=OCC,
            underlying="QQQ", contract_right="C", signal_side="long", intended_qty=qty,
            limit_price=0.52, signal_ts=signal_ts, arm=arm, clock=self.clock,
        )

    # ------------------------------------------------------------ scenario 1
    def test_broker_accepted_but_client_timed_out(self):
        """Broker recorded the order; the client never got the response. The old
        code returned {'submitted': False} here, which is how a real filled
        position ends up with no local record."""
        self.broker.submit_mode = "accepted_but_timeout"
        self.broker.fill_policy = FILL_IMMEDIATE

        outcome = self._submit()

        self.assertEqual(outcome.state, OPEN,
                         f"expected the real fill to be adopted, got {outcome.state}")
        self.assertEqual(outcome.filled_qty, 1)
        pos = self.store.get_position(outcome.position_id)
        self.assertTrue(pos["entry_broker_order_id"],
                        "broker order id must be persisted after timeout resolution")
        assert_no_unaccounted_exposure(self, self.store, self.broker)

    def test_submit_timeout_that_never_landed_is_not_treated_as_open(self):
        """The mirror case: the order genuinely never reached the venue. CANCELED is
        only concluded because the broker AFFIRMATIVELY 404s the lookup."""
        self.broker.submit_mode = "lost"
        outcome = self._submit()
        self.assertEqual(outcome.state, CANCELED)
        self.assertIn("never landed", outcome.reason)
        self.assertEqual(self.broker.positions.get(OCC, 0), 0)

    def test_submit_timeout_with_unresolvable_lookup_stays_submitted(self):
        """If even the lookup is ambiguous we must NOT finalise. Leaving the row
        SUBMITTED is what lets reconciliation recover it later."""
        self.broker.submit_mode = "accepted_but_timeout"
        self.broker.lookup_timeout = True
        outcome = self._submit()
        self.assertEqual(outcome.state, SUBMITTED)
        self.assertIn("UNKNOWN", outcome.reason)
        self.assertEqual(self.store.get_position(outcome.position_id)["state"], SUBMITTED)

    # ------------------------------------------------------------ scenario 2
    def test_crash_immediately_after_broker_acceptance_is_recovered(self):
        """Process dies between broker ack and any local fill record: intent is
        committed, the order is live and filled at the broker, local row still says
        SUBMITTED. A fresh process must adopt it."""
        intent = self.store.create_entry_intent(
            signal_key="crash:1:long", occ=OCC, underlying="QQQ", contract_right="C",
            signal_side="long", intended_qty=1, limit_price=0.52, order_type="limit",
            signal_ts="2026-07-31T14:00:00+00:00")
        self.store.mark_order_submitted(intent["client_order_id"])
        self.broker.fill_policy = FILL_IMMEDIATE
        self.broker.submit_order({
            "symbol": OCC, "qty": "1", "side": "buy", "type": "limit",
            "limit_price": "0.52", "client_order_id": intent["client_order_id"]})

        # ---- process "restarts": new store handle over the same database file
        self.store.close()
        fresh = SmcStateStore(self.config.db_path)
        try:
            result = reconcile(fresh, self.broker, self.config, self.dashboard)
            self.assertTrue(result.adopted,
                            "a filled broker order with no local fill record must be adopted")
            pos = fresh.get_position(intent["position_id"])
            self.assertEqual(pos["state"], OPEN)
            self.assertEqual(pos["filled_qty"], 1)
            assert_no_unaccounted_exposure(self, fresh, self.broker)
        finally:
            fresh.close()
            self.store = SmcStateStore(self.config.db_path)  # for tearDown

    def test_restart_cancels_a_still_working_entry_instead_of_resetting_ttl(self):
        intent = self.store.create_entry_intent(
            signal_key="restart:working", occ=OCC, underlying="QQQ", contract_right="C",
            signal_side="long", intended_qty=1, limit_price=0.52, order_type="limit",
            signal_ts="2026-07-31T14:00:00+00:00")
        self.store.mark_order_submitted(intent["client_order_id"])
        self.broker.fill_policy = FILL_NEVER
        self.broker.submit_order({
            "symbol": OCC, "qty": "1", "side": "buy", "type": "limit",
            "limit_price": "0.52", "client_order_id": intent["client_order_id"]})
        result = reconcile(self.store, self.broker, self.config, self.dashboard)
        self.assertEqual(self.store.get_position(intent["position_id"])["state"], CANCELED)
        self.assertTrue(self.broker.canceled)
        self.assertFalse(self.store.unresolved_intents())


    # ------------------------------------------------------------ scenario 5
    def test_duplicate_signal_submits_only_once(self):
        """The dedup failure at the signal level. UNIQUE(signal_key) must reject the
        second attempt -- not application bookkeeping."""
        self.broker.fill_policy = FILL_IMMEDIATE
        first = self._submit()
        second = self._submit()  # identical signal_key

        self.assertEqual(first.state, OPEN)
        self.assertEqual(second.state, CANCELED)
        self.assertIn("duplicate signal suppressed", second.reason)
        self.assertEqual(len(self.broker.buy_orders()), 1,
                         "a duplicate signal must never reach the broker twice")

    def test_duplicate_signal_suppressed_even_after_restart(self):
        """Dedup must survive a process restart, which the old JSON bookkeeping
        could not guarantee."""
        self.broker.fill_policy = FILL_IMMEDIATE
        self._submit()
        self.store.close()
        fresh = SmcStateStore(self.config.db_path)
        try:
            again = self._submit(store=fresh)
            self.assertEqual(again.state, CANCELED)
            self.assertEqual(len(self.broker.buy_orders()), 1)
        finally:
            fresh.close()
            self.store = SmcStateStore(self.config.db_path)

    # ------------------------------------------------------------ scenario 8
    def test_entry_unfilled_past_ttl_is_canceled_and_flat(self):
        """No TTL existed before: a stale limit could fill minutes after its
        1-minute signal bar. Now it must be cancelled and confirmed flat."""
        self.broker.fill_policy = FILL_NEVER
        outcome = self._submit()

        self.assertEqual(outcome.state, CANCELED)
        self.assertTrue(self.broker.canceled, "TTL expiry must actually request a cancel")
        self.assertEqual(self.broker.positions.get(OCC, 0), 0)
        self.assertEqual(self.store.get_position(outcome.position_id)["state"], CANCELED)
        assert_no_unaccounted_exposure(self, self.store, self.broker)

    def test_entry_fills_within_ttl_after_a_few_polls(self):
        self.broker.fill_policy = FILL_AFTER_POLLS
        self.broker.fill_after_polls = 2
        outcome = self._submit()
        self.assertEqual(outcome.state, OPEN)
        self.assertFalse(self.broker.canceled,
                         "a fill inside the TTL must not trigger a cancel")

    # ------------------------------------------------------------ scenario 9
    def test_entry_fill_during_cancel_is_registered_and_protected(self):
        """The race that silently creates unmanaged exposure: TTL expires, we
        cancel, and the fill lands anyway. It must be adopted, not dropped."""
        self.broker.fill_policy = FILL_ON_CANCEL
        outcome = self._submit()

        self.assertEqual(outcome.state, OPEN,
                         f"fill-during-cancel must be adopted, got {outcome.state}")
        self.assertEqual(outcome.filled_qty, 1)
        self.assertEqual(self.broker.positions[OCC], 1)
        pos = self.store.get_position(outcome.position_id)
        self.assertEqual(pos["state"], OPEN)
        self.assertEqual(pos["filled_qty"], 1)
        assert_no_unaccounted_exposure(self, self.store, self.broker)

    # ----------------------------------------------------------- scenario 11
    def test_partial_entry_fill_is_kept_at_real_quantity(self):
        """A partial fill is real exposure, tracked at the ACTUAL filled quantity --
        the old code hard-coded qty=1 and would have exited the wrong size."""
        self.broker.fill_policy = FILL_PARTIAL
        self.broker.partial_qty = 1
        outcome = self._submit(qty=3)

        self.assertEqual(outcome.state, PARTIAL)
        self.assertEqual(outcome.filled_qty, 1,
                         "must record the real partial quantity, not the intent")
        pos = self.store.get_position(outcome.position_id)
        self.assertEqual(pos["intended_qty"], 3)
        self.assertEqual(pos["filled_qty"], 1)
        assert_no_unaccounted_exposure(self, self.store, self.broker)

    # ----------------------------------------------------------- scenario 14
    def test_restart_with_unattributable_broker_position_halts(self):
        """A broker position SMC cannot attribute to any state record must halt
        entries and be reported as an orphan -- never silently ignored, and never
        assumed to be SMC's to close."""
        self.broker.set_position(OCC, 2)

        result = reconcile(self.store, self.broker, self.config, self.dashboard)

        self.assertTrue(result.orphans, "unattributed SMC-underlying exposure must be flagged")
        self.assertEqual(result.orphans[0]["occ"], OCC)
        self.assertFalse(result.clean)
        self.assertIsNotNone(self.store.active_halt(), "an orphan must halt new entries")

    # ------------------------------------------------------------- backstops
    def test_stale_signal_is_rejected_before_any_order(self):
        """Backstop for the measured 227-407s pipeline latency: even if the
        pipeline regresses, an old signal must not be traded."""
        old_ts = (self.clock.now() - dt.timedelta(seconds=300)).isoformat()
        outcome = self._submit(signal_ts=old_ts)
        self.assertEqual(outcome.state, CANCELED)
        self.assertIn("rejected", outcome.reason)
        self.assertFalse(self.broker.submitted, "a stale signal must never reach the broker")

    def test_dry_run_leaves_no_phantom_state_and_no_orders(self):
        """The disarmed wrapper runs this path every cron tick; it must not
        accumulate INTENT rows that later look like real positions."""
        outcome = self._submit(signal_key="dry:1:long", arm=False)
        self.assertEqual(outcome.state, CANCELED)
        self.assertFalse(self.broker.submitted, "dry run must not submit anything")
        self.assertEqual(self.store.open_positions(), [])

    def test_no_order_is_ever_submitted_without_a_client_order_id(self):
        """Deterministic ids are the whole basis of timeout recovery; a submission
        without one would be unrecoverable by construction."""
        self.broker.fill_policy = FILL_IMMEDIATE
        self._submit()
        for payload in self.broker.submitted:
            self.assertIn("client_order_id", payload)
            self.assertTrue(payload["client_order_id"].startswith("smc-"))


if __name__ == "__main__":
    unittest.main()
