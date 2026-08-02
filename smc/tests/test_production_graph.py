"""Object-graph test: proves run_daemon builds the REAL production graph.

The gap this closes: the integration test drove smc.assembly while
run_daemon had its own shorter wiring that never constructed a detector
worker, exit monitor or liveness tracker. Validation was testing a different
system from production.

These tests inspect the graph the PRODUCTION ENTRY POINT actually creates.
A fake detector or a disconnected placeholder cannot satisfy them: each
component is asserted to be the real class, and the shared instances are
asserted to be the SAME objects (identity, not equality), so a second
runner, bus or store would fail.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from smc import run_daemon as rd


class ProductionGraphTests(unittest.TestCase):
    """Runs offline-validate, which opens no socket but builds the full
    graph -- the point being that the graph is mode-independent."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        args = rd.parse_args([
            "--mode", rd.MODE_OFFLINE_VALIDATE, "--once",
            "--lock", str(Path(cls._tmp.name) / "graph.lock"),
            "--dashboard", str(Path(cls._tmp.name) / "panel.json")])
        cls.daemon = rd.Daemon(args)
        cls.runner = cls.daemon.start()

    @classmethod
    def tearDownClass(cls):
        cls.daemon.shutdown()
        cls._tmp.cleanup()

    # ------------------------------------------------ components present
    def test_detector_is_the_real_persistent_detector(self):
        from smc.detector import PersistentDetector
        det = self.daemon.components.get("detector")
        self.assertIsInstance(det, PersistentDetector)
        self.assertTrue(det.synchronized, "detector must be warmed, not a stub")

    def test_detector_worker_constructed_and_running(self):
        """The gap that made the daemon not-assembled."""
        from smc.detector_worker import DetectorWorker
        w = self.daemon.components.get("detector_worker")
        self.assertIsInstance(w, DetectorWorker)
        self.assertIsNotNone(w._thread, "worker thread not started")

    def test_exit_monitor_is_real(self):
        from smc.exit_monitor import StreamingExitMonitor
        self.assertIsInstance(self.daemon.components.get("exit_monitor"),
                              StreamingExitMonitor)

    def test_exit_liveness_is_real(self):
        from smc.exit_liveness import ExitLivenessTracker
        self.assertIsInstance(self.daemon.components.get("exit_liveness"),
                              ExitLivenessTracker)

    def test_durable_state_and_outbox_are_real(self):
        from smc.notify_outbox import NotifyOutbox
        from smc.state import SmcStateStore
        self.assertIsInstance(self.daemon.components.get("store"), SmcStateStore)
        self.assertIsInstance(self.daemon.components.get("outbox"), NotifyOutbox)

    def test_notifier_is_the_durable_queue(self):
        from smc.notify_queue import NotifyQueue
        self.assertIsInstance(self.daemon.components.get("notifier"), NotifyQueue)

    def test_pipeline_built_by_the_shared_factory(self):
        from smc.assembly import Pipeline
        self.assertIsInstance(self.daemon.pipeline, Pipeline)

    # ------------------------------------------- shared instance identity
    def test_pipeline_and_daemon_share_one_runner(self):
        self.assertIs(self.daemon.pipeline.runner, self.runner)

    def test_pipeline_shares_the_real_component_instances(self):
        c = self.daemon.components
        self.assertIs(self.daemon.pipeline.detector_worker, c["detector_worker"])
        self.assertIs(self.daemon.pipeline.exit_monitor, c["exit_monitor"])
        self.assertIs(self.daemon.pipeline.exit_liveness, c["exit_liveness"])
        self.assertIs(self.daemon.pipeline.notifier, c["notifier"])

    def test_outbox_shares_the_state_store_connection(self):
        """One SQLite connection for events and their delivery obligations --
        two would break the atomic commit_event guarantee."""
        self.assertIs(self.daemon.components["outbox"].conn,
                      self.daemon.components["store"].conn)

    def test_single_event_bus(self):
        self.assertIs(self.daemon.pipeline.runner.bus, self.runner.bus)

    # ------------------------------------------------ callbacks registered
    def test_detector_worker_result_callback_registered(self):
        self.assertIsNotNone(self.daemon.components["detector_worker"].on_result)

    def test_signal_handler_replaced_by_the_pipeline(self):
        from smc import events as ev
        handler = self.runner._handlers[ev.EV_SIGNAL]
        self.assertEqual(handler.__name__, "handle_signal")

    def test_quote_handler_replaced_by_the_pipeline(self):
        from smc import events as ev
        self.assertEqual(self.runner._handlers[ev.EV_QUOTE].__name__, "handle_quote")

    def test_order_handlers_replaced_by_the_pipeline(self):
        from smc import events as ev
        for etype in (ev.EV_FILL, ev.EV_PARTIAL_FILL, ev.EV_CANCEL,
                      ev.EV_REJECT, ev.EV_ORDER_ACK):
            with self.subTest(etype=etype):
                self.assertEqual(self.runner._handlers[etype].__name__, "handle_order")

    def test_exit_monitor_reads_pipeline_positions(self):
        """build_pipeline rebinds open_positions so the monitor sees the
        live position map rather than the empty placeholder."""
        self.daemon.pipeline.positions["p1"] = {"position_id": "p1", "occ": "X"}
        try:
            self.assertEqual(
                len(self.daemon.components["exit_monitor"].open_positions()), 1)
        finally:
            self.daemon.pipeline.positions.clear()

    # ------------------------------------------------------ no placeholders
    def test_no_component_is_none(self):
        for name in ("store", "outbox", "notifier", "detector",
                     "detector_worker", "exit_monitor", "exit_liveness"):
            with self.subTest(name=name):
                self.assertIsNotNone(self.daemon.components.get(name),
                                     f"{name} missing from the production graph")

    def test_same_factory_used_for_every_mode(self):
        """validate and paper-forward must not build different systems."""
        import inspect
        src = inspect.getsource(rd.Daemon._build_production_pipeline)
        self.assertIn("build_pipeline", src)
        start_src = inspect.getsource(rd.Daemon.start)
        self.assertEqual(start_src.count("_build_production_pipeline"), 1)
        self.assertNotIn("if self.mode == MODE_FORWARD", start_src)


if __name__ == "__main__":
    unittest.main()
