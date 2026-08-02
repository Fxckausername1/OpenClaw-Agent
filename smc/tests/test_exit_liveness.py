"""Tests for smc/exit_liveness.py.

Covers every failure heff listed: lost terminal event, duplicate event,
out-of-order event, partial fill, cancel/fill race, stream disconnect and
restart. The clock is injected so deadlines are deterministic.
"""
from __future__ import annotations

import unittest

from smc.exit_liveness import (
    EXIT_CANCELED, EXIT_FILLED, EXIT_PARTIAL, EXIT_PENDING, EXIT_REJECTED,
    EXIT_UNKNOWN, ExitLivenessTracker,
)


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


def tracker(lookup=None, **kw):
    incidents = []
    t = ExitLivenessTracker(
        rest_lookup=lookup or (lambda coid: None),
        raise_incident=lambda kind, detail: incidents.append((kind, detail)),
        clock=kw.pop("clock", Clock()), **kw)
    t._incidents = incidents
    return t


def reg(t, qty=1.0):
    return t.register(position_id="pos-1", occ="QQQ260803C00580000",
                      client_order_id="coid-1", intended_qty=qty)


class BasicTests(unittest.TestCase):
    def test_registration_arms_a_deadline(self):
        t = tracker()
        a = reg(t)
        self.assertEqual(a.state, EXIT_PENDING)
        self.assertGreater(a.reconcile_deadline, 0)
        self.assertEqual(a.remaining_qty, 1.0)

    def test_full_fill_is_terminal(self):
        t = tracker()
        reg(t)
        a = t.on_update("coid-1", "fill", filled_qty=1.0)
        self.assertEqual(a.state, EXIT_FILLED)
        self.assertTrue(a.terminal)
        self.assertEqual(a.remaining_qty, 0.0)

    def test_unknown_coid_ignored(self):
        self.assertIsNone(tracker().on_update("nope", "fill"))


class PartialFillTests(unittest.TestCase):
    def test_partial_keeps_remaining_managed(self):
        t = tracker()
        reg(t, qty=3.0)
        a = t.on_update("coid-1", "partial_fill", filled_qty=1.0)
        self.assertEqual(a.state, EXIT_PARTIAL)
        self.assertEqual(a.remaining_qty, 2.0)
        self.assertFalse(a.terminal)

    def test_partial_then_cancel_needs_followup_for_remainder_only(self):
        t = tracker()
        reg(t, qty=3.0)
        t.on_update("coid-1", "partial_fill", filled_qty=1.0)
        a = t.on_update("coid-1", "canceled")
        self.assertEqual(a.state, EXIT_CANCELED)
        self.assertTrue(a.needs_followup_exit)
        self.assertEqual(a.remaining_qty, 2.0)

    def test_partial_completing_to_full_is_filled(self):
        t = tracker()
        reg(t, qty=2.0)
        t.on_update("coid-1", "partial_fill", filled_qty=1.0)
        a = t.on_update("coid-1", "partial_fill", filled_qty=2.0)
        self.assertEqual(a.state, EXIT_FILLED)

    def test_partial_deadline_rearmed(self):
        clk = Clock()
        t = tracker(clock=clk)
        reg(t, qty=3.0)
        clk.advance(4.0)
        a = t.on_update("coid-1", "partial_fill", filled_qty=1.0)
        self.assertGreater(a.reconcile_deadline, clk.t)


class EventOrderingTests(unittest.TestCase):
    def test_duplicate_fill_is_idempotent(self):
        t = tracker()
        reg(t)
        t.on_update("coid-1", "fill", filled_qty=1.0)
        a = t.on_update("coid-1", "fill", filled_qty=1.0)
        self.assertEqual(a.filled_qty, 1.0)
        self.assertEqual(a.state, EXIT_FILLED)

    def test_out_of_order_smaller_fill_does_not_reduce_quantity(self):
        """filled_qty only moves forward."""
        t = tracker()
        reg(t, qty=3.0)
        t.on_update("coid-1", "partial_fill", filled_qty=2.0)
        a = t.on_update("coid-1", "partial_fill", filled_qty=1.0)
        self.assertEqual(a.filled_qty, 2.0)

    def test_late_cancel_after_full_fill_does_not_resurrect_exposure(self):
        """The cancel/fill race in the dangerous direction."""
        t = tracker()
        reg(t)
        t.on_update("coid-1", "fill", filled_qty=1.0)
        a = t.on_update("coid-1", "canceled")
        self.assertEqual(a.state, EXIT_FILLED)
        self.assertFalse(a.needs_followup_exit)

    def test_rejected_with_remaining_needs_followup(self):
        t = tracker()
        reg(t)
        a = t.on_update("coid-1", "rejected", reason="no position")
        self.assertEqual(a.state, EXIT_REJECTED)
        self.assertTrue(a.needs_followup_exit)


class LostEventTests(unittest.TestCase):
    def test_no_update_by_deadline_triggers_reconcile(self):
        clk = Clock()
        t = tracker(clock=clk)
        reg(t)
        self.assertEqual(t.due_for_reconcile(), [])
        clk.advance(6.0)
        self.assertEqual(len(t.due_for_reconcile()), 1)

    def test_reconcile_resolves_from_rest(self):
        clk = Clock()
        t = tracker(lookup=lambda c: {"status": "filled", "filled_qty": "1",
                                      "id": "brk-9"}, clock=clk)
        a = reg(t)
        clk.advance(6.0)
        t.reconcile(a)
        self.assertEqual(a.state, EXIT_FILLED)
        self.assertEqual(a.broker_order_id, "brk-9")

    def test_reconcile_never_resubmits(self):
        """The tracker has no submit path at all -- resolving fate must
        never create a second exit."""
        import inspect

        import smc.exit_liveness as el
        src = inspect.getsource(el)
        for banned in ("submit_order", "submit_exit", "place_order"):
            self.assertNotIn(banned, src)

    def test_partial_status_from_rest_keeps_managing(self):
        clk = Clock()
        t = tracker(lookup=lambda c: {"status": "partially_filled",
                                      "filled_qty": "1"}, clock=clk)
        a = reg(t, qty=3.0)
        clk.advance(6.0)
        t.reconcile(a)
        self.assertEqual(a.state, EXIT_PARTIAL)
        self.assertEqual(a.remaining_qty, 2.0)

    def test_unknown_fate_becomes_unmanaged_risk_incident(self):
        clk = Clock()
        t = tracker(lookup=lambda c: None, clock=clk, max_attempts=2)
        a = reg(t)
        for _ in range(2):
            clk.advance(6.0)
            t.reconcile(a)
        self.assertEqual(a.state, EXIT_UNKNOWN)
        self.assertTrue(a.unmanaged_risk)
        kinds = [k for k, _ in t._incidents]
        self.assertIn("reconciliation_mismatch", kinds)
        self.assertEqual(t.health()["unmanaged_risk"], 1)

    def test_unknown_fate_is_not_treated_as_closed(self):
        clk = Clock()
        t = tracker(lookup=lambda c: None, clock=clk, max_attempts=1)
        a = reg(t)
        clk.advance(6.0)
        t.reconcile(a)
        self.assertNotEqual(a.state, EXIT_FILLED)
        self.assertFalse(a.terminal)

    def test_lookup_raising_is_captured(self):
        clk = Clock()

        def boom(c):
            raise RuntimeError("rest down")
        t = tracker(lookup=boom, clock=clk, max_attempts=5)
        a = reg(t)
        clk.advance(6.0)
        t.reconcile(a)
        self.assertIn("rest down", a.last_error)


class DisconnectTests(unittest.TestCase):
    def test_disconnect_blocks_entries_and_reconciles(self):
        calls = []
        t = tracker(lookup=lambda c: calls.append(c) or {"status": "new"})
        reg(t)
        active = t.on_stream_disconnect()
        self.assertTrue(t.entries_blocked)
        self.assertEqual(len(active), 1)
        self.assertEqual(calls, ["coid-1"])

    def test_disconnect_keeps_protecting_existing_position(self):
        """Degraded is not stopped: the attempt stays tracked."""
        t = tracker(lookup=lambda c: {"status": "new"})
        reg(t)
        t.on_stream_disconnect()
        self.assertEqual(t.health()["in_flight"], 1)

    def test_reconnect_unblocks_entries(self):
        t = tracker(lookup=lambda c: {"status": "new"})
        reg(t)
        t.on_stream_disconnect()
        t.on_stream_reconnect()
        self.assertFalse(t.entries_blocked)


class RestartTests(unittest.TestCase):
    def test_restore_reconciles_before_anything_else(self):
        seen = []
        t = tracker(lookup=lambda c: seen.append(c) or {"status": "filled",
                                                        "filled_qty": "1"})
        restored = t.restore([{"position_id": "pos-1", "occ": "QQQ260803C00580000",
                               "client_order_id": "coid-1", "intended_qty": 1,
                               "filled_qty": 0, "state": EXIT_PENDING}])
        self.assertEqual(len(restored), 1)
        self.assertEqual(seen, ["coid-1"])
        self.assertEqual(restored[0].state, EXIT_FILLED)

    def test_restore_preserves_partial_quantity(self):
        t = tracker(lookup=lambda c: {"status": "partially_filled", "filled_qty": "1"})
        restored = t.restore([{"position_id": "p", "occ": "O",
                               "client_order_id": "c", "intended_qty": 3,
                               "filled_qty": 1, "state": EXIT_PARTIAL}])
        self.assertEqual(restored[0].remaining_qty, 2.0)

    def test_terminal_rows_not_reconciled(self):
        seen = []
        t = tracker(lookup=lambda c: seen.append(c) or None)
        t.restore([{"position_id": "p", "occ": "O", "client_order_id": "c",
                    "intended_qty": 1, "filled_qty": 1, "state": EXIT_FILLED}])
        self.assertEqual(seen, [])

    def test_persist_failure_is_non_fatal(self):
        def bad(attempt):
            raise RuntimeError("disk full")
        t = ExitLivenessTracker(rest_lookup=lambda c: None,
                                raise_incident=lambda k, d: None,
                                persist=bad, clock=Clock())
        a = t.register(position_id="p", occ="O", client_order_id="c",
                       intended_qty=1)
        self.assertIsNotNone(a)


class HealthTests(unittest.TestCase):
    def test_health_shape(self):
        h = tracker().health()
        for k in ("tracked", "in_flight", "partial", "unmanaged_risk",
                  "lost_event_reconciles", "entries_blocked",
                  "oldest_deadline_age_s"):
            self.assertIn(k, h)


if __name__ == "__main__":
    unittest.main()
