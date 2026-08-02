"""OS-level single-instance guard for the paper runner.

Database idempotency (deterministic client_order_id, UNIQUE signal_key)
already prevents a duplicate ORDER. It does not prevent two runners from
both holding ThetaData subscriptions, both consuming trade_updates, both
writing dashboard state and both racing the same exit decision. Those are
real harms that no amount of row-level uniqueness addresses, which is why
heff asked for an OS-level mechanism IN ADDITION to the DB guarantees.

Uses flock(LOCK_EX|LOCK_NB) on a lock file, which is the right primitive
here for one specific reason: the kernel releases it automatically when the
holding process dies, however it dies -- SIGKILL, OOM, power loss. A PID
file would survive a hard kill and lock out the restart; a stale flock
cannot exist.

Same primitive the box already uses for cron overlap protection (see
scripts/*_wrapper.sh `exec 9>...; flock -n 9`), so the operational idiom is
unchanged, only moved into the process.

The PID and start time are written into the file purely as a human-readable
breadcrumb for `cat`; they are never used to decide whether the lock is
held. The flock is the truth.
"""
from __future__ import annotations

import datetime as dt
import errno
import fcntl
import logging
import os
from pathlib import Path
from typing import Optional

logger = logging.getLogger("smc.singleton")

DEFAULT_LOCK_PATH = Path("/tmp/smc_paper_runner.lock")


class AlreadyRunning(RuntimeError):
    """Another runner holds the lock. Fail closed: do not start a second one."""


class SingleInstance:
    """Context manager. Acquire BEFORE opening any stream, socket or DB."""

    def __init__(self, lock_path: Path = DEFAULT_LOCK_PATH):
        self.lock_path = Path(lock_path)
        self._fd: Optional[int] = None

    def acquire(self) -> "SingleInstance":
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        # Kept open for the process lifetime: closing the fd drops the lock.
        self._fd = os.open(str(self.lock_path), os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            os.close(self._fd)
            self._fd = None
            if e.errno in (errno.EACCES, errno.EAGAIN):
                holder = self._read_breadcrumb()
                raise AlreadyRunning(
                    f"another SMC paper runner already holds {self.lock_path} "
                    f"({holder}). Refusing to start a second instance.") from e
            raise
        os.ftruncate(self._fd, 0)
        os.write(self._fd, (f"pid={os.getpid()} "
                            f"started={dt.datetime.now(dt.timezone.utc).isoformat()}\n"
                            ).encode())
        os.fsync(self._fd)
        logger.info("singleton acquired: %s (pid=%s)", self.lock_path, os.getpid())
        return self

    def _read_breadcrumb(self) -> str:
        try:
            return self.lock_path.read_text().strip() or "no breadcrumb"
        except OSError:
            return "unreadable"

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            try:
                os.close(self._fd)
            finally:
                self._fd = None
                logger.info("singleton released: %s", self.lock_path)

    @property
    def held(self) -> bool:
        return self._fd is not None

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *exc):
        self.release()
        return False
