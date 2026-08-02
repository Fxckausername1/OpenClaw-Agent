"""Own the local Theta Terminal lifecycle for the SMC daemon.

The API key is injected through the child environment, never the argv or log.
An already-running terminal is reused and never stopped by this manager.
"""
from __future__ import annotations

import datetime as dt
import logging
import os
import re
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger("smc.theta_terminal")

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_JAVA = Path("/home/heff/.local/opt/temurin21/bin/java")
DEFAULT_JAR = Path("/home/heff/.openclaw/thetadata-terminal/ThetaTerminalv3.jar")
DEFAULT_KEY_FILE = ROOT / "credentials" / "thetadata_key.txt"
DEFAULT_LOG = ROOT / "logs" / "theta_terminal.log"
DEFAULT_PID_FILE = Path("/tmp/thetadata_terminal_heff.pid")


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


class ThetaTerminalManager:
    """Start, monitor, and stop the Theta Terminal used by this daemon."""

    def __init__(
        self,
        *,
        java_path: Path | str = DEFAULT_JAVA,
        jar_path: Path | str = DEFAULT_JAR,
        key_file: Path | str = DEFAULT_KEY_FILE,
        log_path: Path | str = DEFAULT_LOG,
        pid_file: Path | str = DEFAULT_PID_FILE,
        host: str = "127.0.0.1",
        port: int = 25520,
        startup_timeout: float = 90.0,
        watchdog_interval: float = 2.0,
        popen: Callable = subprocess.Popen,
        run: Callable = subprocess.run,
        port_check: Optional[Callable[[], bool]] = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.java_path = Path(os.environ.get("THETADATA_JAVA", str(java_path)))
        self.jar_path = Path(os.environ.get("THETADATA_TERMINAL_JAR", str(jar_path)))
        self.key_file = Path(os.environ.get("THETADATA_API_KEY_FILE", str(key_file)))
        self.log_path = Path(os.environ.get("THETADATA_TERMINAL_LOG", str(log_path)))
        self.pid_file = Path(pid_file)
        self.host = host
        self.port = int(port)
        self.startup_timeout = float(startup_timeout)
        self.watchdog_interval = float(watchdog_interval)
        self._popen = popen
        self._run = run
        self._port_check = port_check or self._tcp_ready
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._watchdog: Optional[threading.Thread] = None
        self._process = None
        self._owned = False
        self._state = "stopped"
        self._last_error: Optional[str] = None
        self._last_start_ts: Optional[str] = None
        self._restart_count = 0

    def _tcp_ready(self) -> bool:
        try:
            with socket.create_connection((self.host, self.port), timeout=0.25):
                return True
        except OSError:
            return False

    def _validate_java(self) -> None:
        if not self.java_path.is_file():
            raise RuntimeError(f"Java runtime missing: {self.java_path}")
        result = self._run(
            [str(self.java_path), "-version"], capture_output=True, text=True,
            timeout=10, check=False)
        output = f"{result.stdout}\n{result.stderr}"
        match = re.search(r'version\s+"(\d+)', output)
        if result.returncode != 0 or not match or int(match.group(1)) < 21:
            raise RuntimeError("Theta Terminal requires Java 21 or newer")

    def _read_key(self) -> str:
        try:
            key = self.key_file.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise RuntimeError(f"ThetaData key file unavailable: {self.key_file}") from exc
        if not key:
            raise RuntimeError(f"ThetaData key file is empty: {self.key_file}")
        return key

    def _owned_alive(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def is_ready(self) -> bool:
        ready = bool(self._port_check())
        with self._lock:
            if ready:
                self._state = "ready_owned" if self._owned else "ready_external"
                self._last_error = None
            elif self._owned_alive():
                self._state = "starting"
            elif self._state not in {"failed", "stopped"}:
                self._state = "down"
        return ready

    def _spawn(self) -> None:
        self._validate_java()
        if not self.jar_path.is_file():
            raise RuntimeError(f"Theta Terminal JAR missing: {self.jar_path}")
        key = self._read_key()
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        env = os.environ.copy()
        env["THETADATA_API_KEY"] = key
        # The bootstrap JAR launches its downloaded runtime with bare `java`.
        # Keep the private Java 21 runtime discoverable by that child as well.
        env["PATH"] = f"{self.java_path.parent}{os.pathsep}{env.get('PATH', '')}"
        with self.log_path.open("ab", buffering=0) as log_handle:
            process = self._popen(
                [str(self.java_path), "-jar", str(self.jar_path)],
                cwd=str(self.jar_path.parent), env=env,
                stdin=subprocess.DEVNULL, stdout=log_handle,
                stderr=subprocess.STDOUT, start_new_session=True)
        self._process = process
        self._owned = True
        self._state = "starting"
        self._last_start_ts = _utc_now()
        self.pid_file.write_text(f"{process.pid}\n", encoding="ascii")
        try:
            self.pid_file.chmod(0o600)
        except OSError:
            pass
        logger.info("Theta Terminal launched pid=%s", process.pid)

    def ensure_running(self, *, wait: bool = True) -> bool:
        if self.is_ready():
            return True
        with self._lock:
            if not self._owned_alive():
                if self._process is not None:
                    self._restart_count += 1
                try:
                    self._spawn()
                    self._last_error = None
                except Exception as exc:  # noqa: BLE001
                    self._state = "failed"
                    self._last_error = str(exc)
                    logger.error("Theta Terminal launch failed: %s", exc)
                    return False
        if not wait:
            return self.is_ready()
        deadline = self._clock() + self.startup_timeout
        while self._clock() < deadline and not self._stop.is_set():
            if self.is_ready():
                logger.info("Theta Terminal WebSocket ready on %s:%s", self.host, self.port)
                return True
            if not self._owned_alive():
                break
            self._sleep(0.25)
        with self._lock:
            code = self._process.poll() if self._process is not None else None
            self._state = "failed"
            self._last_error = (
                f"WebSocket port {self.host}:{self.port} not ready; process_exit={code}")
        logger.error("Theta Terminal startup failed: %s", self._last_error)
        return False

    def _watch(self) -> None:
        while not self._stop.wait(self.watchdog_interval):
            if self.is_ready():
                continue
            if not self._owned_alive():
                self.ensure_running(wait=False)

    def start(self) -> bool:
        self._stop.clear()
        ready = self.ensure_running(wait=True)
        with self._lock:
            if self._watchdog is None or not self._watchdog.is_alive():
                self._watchdog = threading.Thread(
                    target=self._watch, name="theta-terminal-watchdog", daemon=True)
                self._watchdog.start()
        return ready

    def stop(self) -> None:
        self._stop.set()
        watchdog = self._watchdog
        if watchdog is not None and watchdog is not threading.current_thread():
            watchdog.join(timeout=max(1.0, self.watchdog_interval + 0.5))
        with self._lock:
            process = self._process
            if self._owned and process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            if self._owned:
                try:
                    self.pid_file.unlink(missing_ok=True)
                except OSError:
                    pass
            self._state = "stopped"
            self._process = None
            self._owned = False
        logger.info("Theta Terminal manager stopped")

    def health(self) -> dict:
        ready = self.is_ready()
        with self._lock:
            return {
                "ready": ready,
                "state": self._state,
                "pid": getattr(self._process, "pid", None),
                "owned_by_daemon": self._owned,
                "restart_count": self._restart_count,
                "last_start_ts": self._last_start_ts,
                "last_error": self._last_error,
                "host": self.host,
                "websocket_port": self.port,
                "java": str(self.java_path),
                "jar": str(self.jar_path),
                "log": str(self.log_path),
            }
