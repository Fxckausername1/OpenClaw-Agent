"""Frozen deterministic signal-identity schema.

A signal key decides whether an order is placed. If the same real signal can
produce two different keys, it gets ordered twice; if two different signals
collapse to one key, the second is silently dropped. Both happened in
embryo: the previous key was f"{session}:{bar_index}:{side}", and bar_index
is relative to whichever series was replayed -- it shifts by 390 per session
whenever the rolling window composition changes. The cache-parity test
caught that as an exact 390/780 offset.

FROZEN SCHEMA v1. Included, in this order:

    strategy          HEFF_SMC          which model produced it
    model_version     v2.2              frozen indicator version
    symbol            QQQ               underlying
    timeframe         1Min              bar interval
    bar_close_utc     ...Z              canonical UTC bar CLOSE, normalized
    side              long|short
    trigger           MSS|BOS|...       winning trigger
    occurrence        0,1,2...          discriminator when more than one
                                        same-side event can exist on one bar

DELIBERATELY EXCLUDED, each for a specific failure it would cause:

    bar_index         window-relative -- the bug this schema replaces
    sequence numbers  process-local; a restart resets them
    autoincrement ids database-local; differs across restores/replicas
    receipt time      local wall clock; differs per process and per restart

TIMESTAMPS ARE NORMALIZED BEFORE HASHING. Everything is converted to UTC and
rendered to whole seconds in a single canonical format, so an ET-local
render, a +00:00 offset and a Z suffix for the same instant all hash
identically. Without that, a DST transition or a formatting change would
silently mint new keys for signals already traded.

Bar CLOSE, not bar open: providers timestamp a 1-minute bar at its START, so
close = start + timeframe. Using close makes the identity mean "the bar that
had finished when this fired", which is what a confirmed-bar signal actually
is.

The unhashed fields are stored ALONGSIDE the hash. A bare hash is
undiagnosable: if two signals ever collide, the fields are what let you see
why.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import re
from typing import Optional

SCHEMA_VERSION = "sigid-v1"
STRATEGY = "HEFF_SMC"
MODEL_VERSION = "v2.2"

TIMEFRAME_SECONDS = {"1Min": 60, "5Min": 300, "15Min": 900, "1Hour": 3600}
_CANONICAL_TS = "%Y-%m-%dT%H:%M:%SZ"
_VALID_SIDE = frozenset({"long", "short"})
_VALID_SYMBOL = re.compile(r"^[A-Z]{1,6}$")


class SignalIdentityError(ValueError):
    """Refuses to mint an identity from fields that cannot be trusted."""


def canonical_utc(ts) -> str:
    """Normalizes any accepted timestamp to one canonical UTC string.

    Accepts an aware datetime, a naive datetime (assumed UTC), or an ISO
    string. Naive-as-UTC is a deliberate choice, not an oversight: the
    detector always hands us aware timestamps, so a naive one means a caller
    bug, and silently guessing a LOCAL zone would be far worse than assuming
    the one canonical zone this system stores everything in."""
    if isinstance(ts, str):
        raw = ts.strip().replace("Z", "+00:00")
        try:
            ts = dt.datetime.fromisoformat(raw)
        except ValueError as e:
            raise SignalIdentityError(f"unparseable timestamp {ts!r}") from e
    if not isinstance(ts, dt.datetime):
        raise SignalIdentityError(f"timestamp must be datetime or ISO string, got {type(ts)}")
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=dt.timezone.utc)
    return ts.astimezone(dt.timezone.utc).replace(microsecond=0).strftime(_CANONICAL_TS)


def bar_close_utc(bar_open, timeframe: str = "1Min") -> str:
    """Canonical UTC close of the bar that opened at `bar_open`."""
    secs = TIMEFRAME_SECONDS.get(timeframe)
    if secs is None:
        raise SignalIdentityError(f"unknown timeframe {timeframe!r}")
    opened = dt.datetime.strptime(canonical_utc(bar_open), _CANONICAL_TS).replace(
        tzinfo=dt.timezone.utc)
    return canonical_utc(opened + dt.timedelta(seconds=secs))


@dataclasses.dataclass(frozen=True)
class SignalIdentity:
    """The identity fields AND their hash. Both are persisted."""
    schema: str
    strategy: str
    model_version: str
    symbol: str
    timeframe: str
    bar_close_utc: str
    side: str
    trigger: str
    occurrence: int
    signal_key: str

    def as_dict(self) -> dict:
        return dataclasses.asdict(self)

    @property
    def fields(self) -> tuple:
        """The exact tuple that was hashed, for collision diagnosis."""
        return (self.schema, self.strategy, self.model_version, self.symbol,
                self.timeframe, self.bar_close_utc, self.side, self.trigger,
                str(self.occurrence))


def make_signal_identity(*, symbol: str, timeframe: str, bar_open, side: str,
                         trigger: str, occurrence: int = 0,
                         strategy: str = STRATEGY,
                         model_version: str = MODEL_VERSION,
                         bar_close=None) -> SignalIdentity:
    """Mints a stable identity. Raises rather than guessing on bad input --
    a malformed identity is worse than no identity, because it will be
    silently treated as a brand-new signal."""
    side = (side or "").strip().lower()
    if side not in _VALID_SIDE:
        raise SignalIdentityError(f"side must be long|short, got {side!r}")
    symbol = (symbol or "").strip().upper()
    if not _VALID_SYMBOL.match(symbol):
        raise SignalIdentityError(f"invalid symbol {symbol!r}")
    trigger = (trigger or "").strip().upper()
    if not trigger:
        raise SignalIdentityError("trigger is required")
    if timeframe not in TIMEFRAME_SECONDS:
        raise SignalIdentityError(f"unknown timeframe {timeframe!r}")
    if not isinstance(occurrence, int) or occurrence < 0:
        raise SignalIdentityError(f"occurrence must be a non-negative int, got {occurrence!r}")

    close = canonical_utc(bar_close) if bar_close is not None else bar_close_utc(
        bar_open, timeframe)

    fields = (SCHEMA_VERSION, strategy, model_version, symbol, timeframe,
              close, side, trigger, str(occurrence))
    digest = hashlib.sha256("|".join(fields).encode()).hexdigest()[:32]
    return SignalIdentity(
        schema=SCHEMA_VERSION, strategy=strategy, model_version=model_version,
        symbol=symbol, timeframe=timeframe, bar_close_utc=close, side=side,
        trigger=trigger, occurrence=occurrence, signal_key=digest)


def occurrence_for(existing_keys, *, symbol, timeframe, bar_open, side, trigger,
                   **kw) -> int:
    """Lowest unused occurrence for an otherwise-identical identity, so a
    genuine second same-side same-trigger event on one bar gets its own key
    instead of being swallowed as a duplicate."""
    n = 0
    while True:
        ident = make_signal_identity(symbol=symbol, timeframe=timeframe,
                                     bar_open=bar_open, side=side,
                                     trigger=trigger, occurrence=n, **kw)
        if ident.signal_key not in existing_keys:
            return n
        n += 1
