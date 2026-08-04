from __future__ import annotations

import tempfile
import unittest

from smc.theta_terminal import ThetaTerminalManager


class NoDuplicateTerminalTests(unittest.TestCase):
    """systemd starts theta-terminal.service and smc-paper-daemon.service at
    the same instant. The daemon won that race, saw the port not yet listening
    and started its OWN Terminal -- two JVM pairs, the duplicate 712MB and
    invisible to systemd, on a 1.9GB box. That is what forced the machine into
    swap and killed the market-data feed."""

    def _manager(self, ready_after_calls, **kw):
        calls = {"n": 0}

        def port_check():
            calls["n"] += 1
            return calls["n"] > ready_after_calls

        spawned = []
        m = ThetaTerminalManager(
            port_check=port_check,
            popen=lambda *a, **k: spawned.append(a) or self.fail("spawned!"),
            clock=lambda: calls["n"] * 0.25,
            sleep=lambda s: None, startup_timeout=30.0, **kw)
        return m, spawned

    def test_waits_for_an_external_terminal_instead_of_spawning(self):
        m, spawned = self._manager(ready_after_calls=5, allow_spawn=False)
        self.assertTrue(m.ensure_running(wait=True))
        self.assertEqual(spawned, [])
        self.assertEqual(m.health()["state"], "ready_external")

    def test_refuses_to_spawn_even_if_the_external_one_never_appears(self):
        """Fail loudly rather than quietly duplicating. A missing feed blocks
        entries; a duplicate Terminal takes the whole box down."""
        m = ThetaTerminalManager(
            port_check=lambda: False, allow_spawn=False,
            popen=lambda *a, **k: self.fail("spawned a duplicate Terminal"),
            clock=lambda: 1e9, sleep=lambda s: None, startup_timeout=0.0)
        self.assertFalse(m.ensure_running(wait=True))
        self.assertIn("refusing to spawn", m.health()["last_error"])

    def test_watchdog_never_respawns_when_externally_managed(self):
        m = ThetaTerminalManager(
            port_check=lambda: False, allow_spawn=False,
            popen=lambda *a, **k: self.fail("watchdog spawned a duplicate"),
            clock=lambda: 1e9, sleep=lambda s: None, startup_timeout=0.0)
        m._stop.set()
        m._watch()          # returns immediately; must not have spawned

    def test_standalone_mode_still_spawns(self):
        """Without the systemd unit installed the daemon must still work."""
        spawned = []

        class P:
            def poll(self):
                return None
            pid = 4242

        m = ThetaTerminalManager(
            port_check=lambda: False, allow_spawn=True,
            popen=lambda *a, **k: (spawned.append(a), P())[1],
            run=lambda *a, **k: type("R", (), {
                "returncode": 0, "stdout": 'version "21.0.1"', "stderr": ""})(),
            clock=lambda: 1e9, sleep=lambda s: None, startup_timeout=0.0)
        m.java_path = __import__("pathlib").Path(__file__)   # exists
        m.jar_path = __import__("pathlib").Path(__file__)
        m.key_file = __import__("pathlib").Path(__file__)
        m.ensure_running(wait=False)
        self.assertEqual(len(spawned), 1)
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
