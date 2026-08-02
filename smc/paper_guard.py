"""Hard paper-only enforcement for the SMC execution path.

heff's standing constraint: this pipeline is authorized for Alpaca PAPER
ONLY. Not live credentials, not the live endpoint, not a mix.

TERMINATION POLICY (revised 2026-08-01 on heff's review). The first version
called os._exit(3) inside the guard. That is correct ONLY before the
application has initialized: after startup, os._exit bypasses SQLite commit,
log flushing, stream shutdown and every finally block -- so a guard intended
to make us safe could instead abandon a durable write mid-flight.

So the guard now does exactly one thing: **raise PaperGuardViolation**, a
fatal typed exception. Who catches it determines what happens:

  * `fatal_guard()` -- for the TOP-LEVEL entry point, BEFORE any thread,
    socket or service starts. Converts the violation into SystemExit(3),
    which unwinds normally and still exits nonzero. Nothing is initialized
    yet, so there is nothing to flush.

  * the daemon's own handler -- for a violation detected AFTER
    initialization (config reload, reconnect). There, the correct response
    is an ORDERLY fail-closed: stop accepting entries, halt, flush state,
    shut down, exit nonzero. Never os._exit.

The requirement is unchanged and is still fail-closed: an invalid endpoint
prevents all network and order activity and exits nonzero. What changed is
that we now get there without discarding buffered state.

Other design decisions worth stating:

1. The URL check is exact-host, not substring. "contains 'paper'" would pass
   "https://api.alpaca.markets/?x=paper-api.alpaca.markets" and
   "https://paper-api.alpaca.markets.evil.com".

2. The credential check is deliberately ASYMMETRIC. A key id beginning "AK"
   is a live key -- the dangerous direction -- so it raises. An unrecognized
   prefix only warns, because the prefix convention is an observed Alpaca
   convention rather than a documented guarantee, and hard-failing on an
   unknown-but-possibly-fine key would block a legitimate paper run for a
   reason we cannot verify. We refuse what we can positively identify as
   live; we never claim to positively identify paper.
"""
from __future__ import annotations

import logging
import sys
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

PAPER_HOST = "paper-api.alpaca.markets"
LIVE_HOST = "api.alpaca.markets"

LIVE_KEY_PREFIX = "AK"
PAPER_KEY_PREFIX = "PK"

EXIT_CODE_PAPER_VIOLATION = 3


class PaperGuardViolation(RuntimeError):
    """FATAL. Raised when anything about the broker configuration is not
    unambiguously Alpaca PAPER. Callers must never continue past this; they
    may only choose between SystemExit (pre-init) and an orderly fail-closed
    shutdown (post-init)."""


def resolved_host(base_url: str) -> str:
    if not base_url or not isinstance(base_url, str):
        raise PaperGuardViolation(f"broker base URL is missing or not a string: {base_url!r}")
    parsed = urlparse(base_url.strip())
    if parsed.scheme != "https":
        raise PaperGuardViolation(
            f"broker base URL must be https, got scheme {parsed.scheme!r} in {base_url!r}")
    if parsed.username or parsed.password:
        raise PaperGuardViolation(
            f"broker base URL must not carry userinfo credentials: {base_url!r}")
    host = (parsed.hostname or "").lower()
    if not host:
        raise PaperGuardViolation(f"broker base URL has no host: {base_url!r}")
    return host


def assert_paper_endpoint(base_url: str) -> str:
    host = resolved_host(base_url)
    if host == LIVE_HOST:
        raise PaperGuardViolation(
            f"REFUSING: base URL resolves to the LIVE Alpaca endpoint ({host}). "
            "This pipeline is authorized for PAPER only.")
    if host != PAPER_HOST:
        raise PaperGuardViolation(
            f"REFUSING: base URL host {host!r} is not the authorized paper "
            f"endpoint {PAPER_HOST!r}.")
    return host


def assert_paper_credentials(key_id) -> None:
    if not key_id or not isinstance(key_id, str):
        raise PaperGuardViolation("Alpaca key id is missing or not a string")
    key_id = key_id.strip()
    if key_id.upper().startswith(LIVE_KEY_PREFIX):
        raise PaperGuardViolation(
            "REFUSING: Alpaca key id begins 'AK', which is a LIVE trading key. "
            "This pipeline is authorized for PAPER only.")
    if not key_id.upper().startswith(PAPER_KEY_PREFIX):
        logger.warning(
            "Alpaca key id has an unrecognized prefix (expected '%s' for a paper "
            "key). Proceeding because the prefix convention is not an API "
            "guarantee; the endpoint check remains authoritative.", PAPER_KEY_PREFIX)


def enforce_paper_mode(base_url: str, key_id=None) -> str:
    """The single check. ALWAYS raises PaperGuardViolation on any problem --
    it never exits the process itself, so the caller decides how to die."""
    host = assert_paper_endpoint(base_url)
    if key_id is not None:
        assert_paper_credentials(key_id)
    logger.info("paper guard OK: broker endpoint verified as %s", host)
    return host


def fatal_guard(base_url: str, key_id=None) -> str:
    """For the TOP-LEVEL entry point ONLY, before any thread, socket, DB
    handle or service exists. Converts a violation into SystemExit(3), which
    unwinds normally -- safe precisely because nothing is initialized yet.

    Do NOT call this once the application is running; use the daemon's
    orderly fail-closed path instead, so SQLite and logs are flushed."""
    try:
        return enforce_paper_mode(base_url, key_id)
    except PaperGuardViolation as exc:
        logger.critical("PAPER GUARD VIOLATION (pre-init): %s", exc)
        sys.stderr.write(f"PAPER GUARD VIOLATION: {exc}\n")
        sys.stderr.flush()
        raise SystemExit(EXIT_CODE_PAPER_VIOLATION) from exc


def is_paper(base_url: str, key_id=None) -> bool:
    """Non-raising probe, for a runtime re-check (config reload, reconnect)
    where the caller wants to decide the response rather than be unwound."""
    try:
        enforce_paper_mode(base_url, key_id)
        return True
    except PaperGuardViolation:
        return False
