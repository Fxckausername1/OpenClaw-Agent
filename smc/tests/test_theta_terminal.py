from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from smc.theta_terminal import ThetaTerminalManager


class FakeProcess:
    def __init__(self, pid=4321):
        self.pid = pid
        self.code = None
        self.terminated = False

    def poll(self):
        return self.code

    def terminate(self):
        self.terminated = True
        self.code = 0

    def wait(self, timeout=None):
        return self.code

    def kill(self):
        self.code = -9


class ThetaTerminalManagerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.java = root / "java"
        self.jar = root / "ThetaTerminalv3.jar"
        self.key = root / "key.txt"
        self.java.write_text("fake")
        self.jar.write_text("fake")
        self.key.write_text("SECRET-NEVER-ARGV")
        self.log = root / "terminal.log"
        self.pid = root / "terminal.pid"

    def tearDown(self):
        self.tmp.cleanup()

    def manager(self, **kw):
        defaults = dict(
            java_path=self.java, jar_path=self.jar, key_file=self.key,
            log_path=self.log, pid_file=self.pid,
            run=lambda *a, **k: SimpleNamespace(
                returncode=0, stdout="", stderr='openjdk version "21.0.12"'),
            startup_timeout=0.01, watchdog_interval=60)
        defaults.update(kw)
        return ThetaTerminalManager(**defaults)

    def test_reuses_ready_external_terminal_without_spawning_or_owning(self):
        calls = []
        m = self.manager(port_check=lambda: True,
                         popen=lambda *a, **k: calls.append((a, k)))
        self.assertTrue(m.start())
        self.assertEqual(calls, [])
        self.assertFalse(m.health()["owned_by_daemon"])
        m.stop()

    def test_key_is_environment_only_and_not_in_argv(self):
        captured = {}
        process = FakeProcess()

        def popen(argv, **kwargs):
            captured["argv"] = argv
            captured["env"] = kwargs["env"]
            return process

        m = self.manager(port_check=lambda: False, popen=popen)
        self.assertFalse(m.ensure_running(wait=False))
        self.assertNotIn("SECRET-NEVER-ARGV", " ".join(captured["argv"]))
        self.assertEqual(captured["env"]["THETADATA_API_KEY"],
                         "SECRET-NEVER-ARGV")
        self.assertEqual(self.pid.read_text().strip(), "4321")
        m.stop()
        self.assertTrue(process.terminated)

    def test_missing_key_fails_closed_without_spawning(self):
        self.key.unlink()
        calls = []
        m = self.manager(port_check=lambda: False,
                         popen=lambda *a, **k: calls.append((a, k)))
        self.assertFalse(m.ensure_running(wait=False))
        self.assertEqual(calls, [])
        self.assertIn("key file unavailable", m.health()["last_error"])

    def test_watchdog_restarts_owned_process_after_crash(self):
        processes = [FakeProcess(1), FakeProcess(2)]
        calls = []

        def popen(*args, **kwargs):
            process = processes[len(calls)]
            calls.append(process)
            return process

        m = self.manager(port_check=lambda: False, popen=popen)
        m.ensure_running(wait=False)
        calls[0].code = 1
        m.ensure_running(wait=False)
        self.assertEqual([p.pid for p in calls], [1, 2])
        self.assertEqual(m.health()["restart_count"], 1)
        m.stop()


if __name__ == "__main__":
    unittest.main()
