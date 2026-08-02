"""ThetaClient auth wrapper: bounded requests, retry/backoff, secret masking.

TD-STD Section 2 (credential handling) and Section 13 (failure modes:
authentication failure -> stop new requests, retain last snapshot as stale).
The API key lives at credentials/thetadata_key.txt (chmod 600, same
convention as alpaca_key.txt/unusualwhales_key.txt) and never enters a log
line or an exception message unmasked.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Callable, Optional

from thetadata import ThetaClient

ROOT = Path(__file__).resolve().parent.parent
KEY_PATH = ROOT / "credentials" / "thetadata_key.txt"

logger = logging.getLogger("thetadata_pkg.client")


class ThetaDataUnavailable(RuntimeError):
    """Raised when ThetaData cannot honestly answer a request (missing
    credential, auth failure, or exhausted retries). Callers must treat this
    as DATA BLOCKED / UNAVAILABLE, never as "empty but valid"."""


def _load_key() -> str:
    if not KEY_PATH.exists():
        raise ThetaDataUnavailable(f"missing credential file: {KEY_PATH}")
    key = KEY_PATH.read_text().strip()
    if not key:
        raise ThetaDataUnavailable("thetadata_key.txt is empty")
    return key


def _mask(text: str, secret: str) -> str:
    if not secret:
        return text
    return text.replace(secret, "***")


_CLIENT: Optional[ThetaClient] = None


def get_client() -> ThetaClient:
    """Process-wide singleton. dataframe_type='pandas' to match the rest of
    this codebase (pandas/pyarrow already a dependency)."""
    global _CLIENT
    if _CLIENT is None:
        key = _load_key()
        logger.info("thetadata key loaded (%s...%s)", key[:4], key[-2:])
        _CLIENT = ThetaClient(api_key=key, dataframe_type="pandas")
    return _CLIENT


def bounded_call(
    fn: Callable[..., Any],
    *args,
    retries: int = 2,
    backoff_seconds: float = 1.5,
    **kwargs,
) -> Any:
    """Call a ThetaClient bound method with retry/backoff. Raises
    ThetaDataUnavailable on exhausted retries instead of letting a raw
    transport exception (which could carry the key in a URL/header repr)
    propagate unmasked, and instead of a caller silently treating an
    exception as "no data" (that would be fabricating an empty-but-valid
    reading -- TD-STD Section 13's explicit prohibition)."""
    secret = KEY_PATH.read_text().strip() if KEY_PATH.exists() else ""
    last_exc: Optional[Exception] = None
    for attempt in range(retries + 1):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:  # thetadata raises plain Exceptions on transport/auth errors
            last_exc = exc
            logger.warning(
                "thetadata call failed (attempt %d/%d): %s",
                attempt + 1, retries + 1, _mask(str(exc), secret),
            )
            if attempt < retries:
                time.sleep(backoff_seconds * (attempt + 1))
    raise ThetaDataUnavailable(f"exhausted retries: {_mask(str(last_exc), secret)}") from last_exc
