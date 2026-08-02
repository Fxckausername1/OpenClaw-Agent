"""Telegram notification transports isolated from the trading hot path.

Production uses ``send_direct`` on NotifyQueue's dedicated worker. It talks to
Telegram over a persistent requests Session, avoiding OpenClaw's measured
20-40 second CLI cold start. The legacy detached CLI ``send`` remains for
backward compatibility and its existing safety tests.

Callers commit durable state before publishing. Neither transport is invoked on
the quote reader, order submission, or protective-exit decision threads.
"""
from __future__ import annotations

import collections
import json
import logging
import os
import subprocess
import threading
import time
from pathlib import Path

import requests

logger = logging.getLogger("smc.notify")

OPENCLAW_BIN = "/usr/bin/openclaw"
TIMEOUT_BIN = "/usr/bin/timeout"
_DIRECT_SESSION = requests.Session()
_DIRECT_LOCK = threading.RLock()
_DIRECT_LATENCY_MS = collections.deque(maxlen=10000)
_DIRECT_SENT = 0
_DIRECT_FAILED = 0


def _telegram_token() -> str | None:
    env = os.environ.get("TELEGRAM_BOT_TOKEN")
    if env:
        return env.strip()
    base = Path.home() / ".openclaw"
    candidates = (
        base / "openclaw.json",
        base / "openclaw.json.last-good",
        base / ".openclaw" / "openclaw.json",
    )
    for path in candidates:
        try:
            payload = json.loads(path.read_text())
            token = (((payload.get("channels") or {}).get("telegram") or {})
                     .get("botToken"))
            if token:
                return str(token).strip()
        except (OSError, ValueError, TypeError):
            continue
    return None


def send_direct(message: str, config, *, session=None, token=None) -> bool:
    """Deliver directly through Telegram HTTPS and wait for its acknowledgement.

    This blocks only the notification worker, up to the short configured
    timeout. The secret and response body are never logged.
    """
    global _DIRECT_SENT, _DIRECT_FAILED
    if not getattr(config, "notifications_enabled", True):
        return False
    token = token or _telegram_token()
    if not token:
        with _DIRECT_LOCK:
            _DIRECT_FAILED += 1
        logger.warning("direct Telegram send unavailable: bot token not found")
        return False
    timeout = max(float(getattr(config, "telegram_timeout_seconds", 5.0)), 0.1)
    started = time.monotonic()
    ok = False
    try:
        response = (session or _DIRECT_SESSION).post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={"chat_id": config.telegram_target, "text": message},
            timeout=timeout)
        body = response.json() if response.content else {}
        ok = bool(response.ok and body.get("ok"))
    except Exception as exc:  # noqa: BLE001 -- alert failure cannot affect trading
        # Exception text may contain the request URL and bot token.
        logger.warning("direct Telegram send failed: %s", type(exc).__name__)
    elapsed = (time.monotonic() - started) * 1000.0
    with _DIRECT_LOCK:
        _DIRECT_LATENCY_MS.append(elapsed)
        if ok:
            _DIRECT_SENT += 1
        else:
            _DIRECT_FAILED += 1
    return ok


def direct_health() -> dict:
    with _DIRECT_LOCK:
        xs = sorted(_DIRECT_LATENCY_MS)
        return {
            "sent": _DIRECT_SENT,
            "failed": _DIRECT_FAILED,
            "latency_ms": {
                "n": len(xs),
                "p50": round(xs[int(.50 * (len(xs) - 1))], 3) if xs else None,
                "p95": round(xs[int(.95 * (len(xs) - 1))], 3) if xs else None,
                "max": round(xs[-1], 3) if xs else None,
            },
        }


def send(message: str, config) -> bool:
    """Legacy detached OpenClaw CLI transport; production no longer uses it."""
    if not getattr(config, "notifications_enabled", True):
        return False
    deadline = max(float(getattr(config, "telegram_timeout_seconds", 5.0)), 0.1)
    argv = [TIMEOUT_BIN, f"{deadline:.3f}s", OPENCLAW_BIN, "message", "send",
            "--channel", "telegram", "--target", config.telegram_target, "--message", message]
    try:
        subprocess.Popen(
            argv,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL, start_new_session=True,
        )
        return True
    except Exception as e:  # noqa: BLE001 -- notification failure is never fatal
        logger.warning("telegram launch failed (non-fatal, ignored): %s", e)
        return False


def notify_after_commit(state_committed: bool, message: str, config) -> bool:
    """Refuse to announce an action that has not been durably committed."""
    if not state_committed:
        logger.error(
            "REFUSING to notify before state commit -- this would announce an "
            "action that is not durably recorded. Message suppressed: %.120s", message,
        )
        return False
    return send(message, config)
