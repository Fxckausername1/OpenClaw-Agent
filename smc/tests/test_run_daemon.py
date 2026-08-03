"""Tests for smc/run_daemon.py and smc/dashboard.py.

unittest, no network. The mode gate is the safety property under test: no
mode except paper-forward may ever be entry-capable, and that must hold even
if every readiness gate is (wrongly) green.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from smc import dashboard
from smc.run_daemon import (
    ENTRY_CAPABLE_MODES, EXIT_ALREADY_RUNNING, EXIT_PAPER_VIOLATION, MODE_CONNECTIVITY,
    MODE_FORWARD, MODE_VALIDATE, MODES, entries_allowed, parse_args,
)
from smc.readiness import GATES, Readiness


def green() -> Readiness:
    r = Readiness()
    for g in GATES:
        r.set_bool(g, True)
    return r


class ModeGateTests(unittest.TestCase):
    def test_only_paper_forward_is_entry_capable(self):
        self.assertEqual(ENTRY_CAPABLE_MODES, {MODE_FORWARD})

    def test_validate_never_trades_even_with_all_gates_green(self):
        """Two independent conditions: a readiness bug alone cannot enable
        trading."""
        self.assertFalse(entries_allowed(MODE_VALIDATE, green()))

    def test_connectivity_never_trades_even_with_all_gates_green(self):
        self.assertFalse(entries_allowed(MODE_CONNECTIVITY, green()))

    def test_forward_trades_only_when_all_gates_green(self):
        self.assertTrue(entries_allowed(MODE_FORWARD, green()))
        self.assertFalse(entries_allowed(MODE_FORWARD, Readiness()))

    def test_forward_blocked_by_a_single_red_gate(self):
        r = green()
        r.set_bool("theta_stream_connected", False, "down")
        self.assertFalse(entries_allowed(MODE_FORWARD, r))

    def test_unknown_mode_is_not_entry_capable(self):
        self.assertFalse(entries_allowed("some-new-mode", green()))


class ArgTests(unittest.TestCase):
    def test_mode_required(self):
        with self.assertRaises(SystemExit):
            parse_args([])

    def test_invalid_mode_rejected(self):
        with self.assertRaises(SystemExit):
            parse_args(["--mode", "live"])

    def test_all_three_modes_parse(self):
        for m in MODES:
            with self.subTest(m=m):
                self.assertEqual(parse_args(["--mode", m]).mode, m)

    def test_exit_codes_are_distinct_and_nonzero(self):
        codes = {EXIT_PAPER_VIOLATION, EXIT_ALREADY_RUNNING}
        self.assertEqual(len(codes), 2)
        self.assertNotIn(0, codes)


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "panel.json"

    def tearDown(self):
        self._tmp.cleanup()

    def _payload(self, **kw):
        health = {"readiness": Readiness().as_dict(), "bus": {"depth": 0}}
        health.update(kw.pop("health", {}))
        return dashboard.build_snapshot(runner_health=health, **kw)

    def test_identity_fields_always_present_and_explicit(self):
        p = self._payload()
        self.assertEqual(p["mode"], "ALPACA PAPER")
        self.assertFalse(p["is_live_money"])
        self.assertEqual(p["candidate"], "VARIANT_B_NO_SWEEP")
        self.assertEqual(p["signal_feed"], "alpaca_iex")
        self.assertIn("NOT consolidated SIP", p["feed_caveat"])

    def test_required_sections_present(self):
        p = self._payload()
        for section in ("gates", "detector", "event_loop", "thetadata",
                        "alpaca_stream", "exits", "exit_liveness",
                        "notifications", "positions", "entry_attempts"):
            self.assertIn(section, p)

    def test_degraded_banner_when_gates_red(self):
        p = self._payload()
        self.assertTrue(p["degraded"])
        self.assertIn("DEGRADED", p["alert_banner"])

    def test_unmanaged_risk_dominates_the_banner(self):
        class Live:
            def health(self):
                return {"unmanaged_risk": 2}
        p = self._payload(exit_liveness=Live())
        self.assertTrue(p["unmanaged_risk"])
        self.assertEqual(p["unmanaged_risk_count"], 2)
        self.assertIn("UNMANAGED RISK", p["alert_banner"])
        self.assertIn("NOT confirmed closed", p["alert_banner"])

    def test_undelivered_critical_surfaces(self):
        class N:
            def health(self):
                # The banner now alarms on how LONG an obligation has been
                # outstanding, not merely that one exists: every publish is
                # briefly undelivered, so a count-based banner stayed lit
                # through any active session.
                return {"undelivered_critical": 3, "state": "open",
                        "worker_alive": True, "outbox_readable": True,
                        "oldest_undelivered_critical_seconds": 600.0}
        r = Readiness()
        for g in GATES:
            r.set_bool(g, True)
        p = dashboard.build_snapshot(
            runner_health={"readiness": r.as_dict(), "bus": {}}, notifier=N())
        self.assertIn("undelivered", p["alert_banner"])

    def test_no_banner_when_healthy(self):
        r = Readiness()
        for g in GATES:
            r.set_bool(g, True)
        p = dashboard.build_snapshot(runner_health={"readiness": r.as_dict(),
                                                    "bus": {}})
        self.assertIsNone(p["alert_banner"])

    def test_write_is_atomic_and_leaves_no_tmp(self):
        res = dashboard.write_snapshot(self.path, self._payload())
        self.assertTrue(res["ok"])
        self.assertTrue(self.path.exists())
        self.assertEqual(list(self.path.parent.glob("*.tmp*")), [])
        json.loads(self.path.read_text())

    def test_write_never_raises_on_bad_path(self):
        res = dashboard.write_snapshot("/proc/definitely/not/writable/x.json",
                                       self._payload())
        self.assertFalse(res["ok"])
        self.assertIsNotNone(res["error"])

    def test_build_snapshot_is_pure_no_io(self):
        import inspect
        src = inspect.getsource(dashboard.build_snapshot)
        for banned in ("open(", "write_text", "requests", "Path("):
            self.assertNotIn(banned, src)


if __name__ == "__main__":
    unittest.main()
