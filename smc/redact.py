"""Secret redaction for anything that can escape the process.

trade_updates.py retains the raw Alpaca payload on every event for
diagnostics. That raw dict can reach logs, the dashboard, Telegram and
report files, so it must be scrubbed at the boundary rather than trusted.

Threat model, stated honestly: Alpaca's *inbound* trade_updates messages are
not known to contain credentials -- the key and secret travel in the
*outbound* authenticate frame. This module exists anyway, because:

  * the authenticate frame is constructed in the same client and one
    mis-logged exception could carry it;
  * a future Alpaca field, or a future caller passing a different dict, must
    not become a leak by default;
  * "we checked once and it looked clean" is not a control.

Fails CLOSED on structure: unknown keys whose NAME matches a secret pattern
are redacted regardless of value, and long high-entropy-looking values under
a suspicious key are redacted even if the key is not on the exact list.

Allowlist-first for the payload we actually keep: `retain_order_fields`
keeps only what order-state reconstruction and diagnostics need, which is
the stronger control -- redaction is the backstop, not the primary defense.
"""
from __future__ import annotations

import re
from typing import Any

REDACTED = "[REDACTED]"

# Key names that must never have their value persisted or displayed.
SECRET_KEY_PATTERN = re.compile(
    r"(?i)(key|secret|token|password|passwd|credential|authorization|auth|"
    r"session|cookie|bearer|api[_-]?key|access[_-]?token|refresh[_-]?token)")

# Keys that merely CONTAIN one of those words but are known-safe business
# fields. Without this, `client_order_id` would be redacted by the `_id`
# path and order reconstruction would break.
SAFE_KEY_EXACT = frozenset({
    "client_order_id", "order_id", "broker_order_id", "replaced_by",
    "replaces", "order_class", "order_type",
})

# Fields worth retaining from an Alpaca order object. Anything not listed is
# discarded rather than redacted -- less data is a better control than
# masked data.
ORDER_FIELDS_RETAINED = (
    "id", "client_order_id", "symbol", "asset_class", "side", "type",
    "order_class", "time_in_force", "qty", "filled_qty", "limit_price",
    "stop_price", "filled_avg_price", "status", "created_at", "submitted_at",
    "updated_at", "filled_at", "canceled_at", "expired_at", "failed_at",
    "replaced_at", "replaced_by", "replaces", "extended_hours",
)
EVENT_FIELDS_RETAINED = (
    "event", "timestamp", "price", "qty", "position_qty", "execution_id",
)


def _looks_secretish(key: str) -> bool:
    if key in SAFE_KEY_EXACT:
        return False
    return bool(SECRET_KEY_PATTERN.search(key or ""))


def redact(obj: Any, _depth: int = 0) -> Any:
    """Recursively redact secret-looking values. Structure is preserved so a
    redacted payload is still diagnosable."""
    if _depth > 12:
        return REDACTED
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if _looks_secretish(str(k)):
                out[k] = REDACTED
            else:
                out[k] = redact(v, _depth + 1)
        return out
    if isinstance(obj, (list, tuple)):
        return [redact(v, _depth + 1) for v in obj]
    return obj


def retain_order_fields(msg: dict) -> dict:
    """Allowlist projection of one trade_updates message: keeps only the
    fields needed to reconstruct order state, then redacts what survives as
    a belt-and-braces second pass.

    Returns a NEW dict; never mutates the caller's payload."""
    if not isinstance(msg, dict):
        return {}
    data = msg.get("data") or {}
    order = data.get("order") or {}
    kept = {
        "stream": msg.get("stream"),
        "data": {k: data.get(k) for k in EVENT_FIELDS_RETAINED if k in data},
    }
    if order:
        kept["data"]["order"] = {
            k: order.get(k) for k in ORDER_FIELDS_RETAINED if k in order
        }
    return redact(kept)


def scrub_text(text: str) -> str:
    """Last-resort scrub for free-text destined for a log line or Telegram:
    masks anything shaped like an Alpaca key/secret or a bearer token."""
    if not text:
        return text
    text = re.sub(r"(?i)\b(PK|AK)[A-Z0-9]{10,}\b", REDACTED, text)
    text = re.sub(r"(?i)bearer\s+[A-Za-z0-9._\-]{12,}", f"Bearer {REDACTED}", text)
    text = re.sub(r"(?i)\b(secret_key|key_id|api_key|token|password)"
                  r"(\"?\s*[:=]\s*\"?)([^\s\",}]{6,})", rf"\1\2{REDACTED}", text)
    return text
