"""Real listed-expiration discovery from ThetaData contract metadata.

Replaces calendar inference. The previous universe warm computed candidate
expirations as today+0/1/2 CALENDAR days and probed each, which on a Friday
asks ThetaData for Saturday and Sunday chains -- dates that are not listed
contracts at all. Those probes produced six "No data found" errors per
refresh and, worse, a weekend date could reach the Greek cache and count
toward a "warm" universe.

Expirations are now DISCOVERED (option_list_expirations) rather than
guessed, and each candidate is confirmed to carry real contracts
(option_list_contracts) before Greeks are loaded. Nothing here hardcodes a
weekday or assumes Monday.

Every discovery records its provenance -- source, retrieval timestamp,
per-expiration contract count, and which candidates were rejected and why --
because "the universe is warm" is a claim that should be auditable after the
fact, not a boolean.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import logging
from typing import Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger("smc.expirations")

ET = ZoneInfo("America/New_York")
SOURCE_THETADATA = "thetadata.option_list_expirations+option_list_strikes"

REJECT_WEEKEND = "weekend"
REJECT_PAST = "in_the_past"
REJECT_BEYOND_DTE = "beyond_dte_window"
REJECT_NO_CONTRACTS = "zero_contracts_listed"


@dataclasses.dataclass(frozen=True)
class ExpirationCandidate:
    expiration: dt.date
    dte: int
    contract_count: int
    accepted: bool
    reason: str = ""

    def as_dict(self) -> dict:
        return {"expiration": self.expiration.isoformat(), "dte": self.dte,
                "contract_count": self.contract_count,
                "accepted": self.accepted, "reason": self.reason}


@dataclasses.dataclass(frozen=True)
class ExpirationDiscovery:
    symbol: str
    source: str
    retrieved_ts: str
    today: dt.date
    candidates: tuple
    selected: tuple
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return bool(self.selected) and self.error is None

    def as_dict(self) -> dict:
        return {
            "symbol": self.symbol, "source": self.source,
            "retrieved_ts": self.retrieved_ts, "today": self.today.isoformat(),
            "selected": [d.isoformat() for d in self.selected],
            "selected_dte": [(d - self.today).days for d in self.selected],
            "candidates": [c.as_dict() for c in self.candidates],
            "rejected": {c.expiration.isoformat(): c.reason
                         for c in self.candidates if not c.accepted},
            "total_contracts": sum(c.contract_count for c in self.candidates
                                   if c.accepted),
            "ok": self.ok, "error": self.error,
        }


def _to_date(value) -> Optional[dt.date]:
    if isinstance(value, dt.date) and not isinstance(value, dt.datetime):
        return value
    if isinstance(value, dt.datetime):
        return value.date()
    try:
        s = str(value).strip()
        if len(s) == 8 and s.isdigit():
            return dt.date(int(s[:4]), int(s[4:6]), int(s[6:8]))
        return dt.date.fromisoformat(s[:10])
    except (ValueError, TypeError):
        return None


def discover_expirations(client, symbol: str = "QQQ", *,
                         dte_window: tuple = (0, 1, 2),
                         today: Optional[dt.date] = None,
                         count_contracts: bool = True) -> ExpirationDiscovery:
    """Lists real expirations, filters to the DTE window, and confirms each
    survivor actually has listed contracts.

    A candidate with zero contracts is REJECTED rather than loaded: a hollow
    expiration reaching the Greek cache is how the universe went green on
    nothing."""
    today = today or dt.datetime.now(ET).date()
    retrieved = dt.datetime.now(dt.timezone.utc).isoformat()

    try:
        raw = client.option_list_expirations(symbol)
    except Exception as e:  # noqa: BLE001
        logger.error("expiration discovery failed for %s: %s", symbol, e)
        return ExpirationDiscovery(symbol=symbol, source=SOURCE_THETADATA,
                                   retrieved_ts=retrieved, today=today,
                                   candidates=(), selected=(), error=repr(e))

    listed = []
    for row in _iter_values(raw):
        d = _to_date(row)
        if d is not None:
            listed.append(d)
    listed = sorted(set(listed))

    max_dte = max(dte_window)
    candidates = []
    for d in listed:
        dte = (d - today).days
        if dte < 0:
            continue                      # silently skip history, not a rejection
        if dte > max_dte:
            continue
        if d.weekday() >= 5:
            candidates.append(ExpirationCandidate(d, dte, 0, False, REJECT_WEEKEND))
            continue
        n = _contract_count(client, symbol, d) if count_contracts else 1
        if n <= 0:
            candidates.append(ExpirationCandidate(d, dte, 0, False, REJECT_NO_CONTRACTS))
            continue
        candidates.append(ExpirationCandidate(d, dte, n, True))

    selected = tuple(c.expiration for c in candidates if c.accepted)
    return ExpirationDiscovery(symbol=symbol, source=SOURCE_THETADATA,
                               retrieved_ts=retrieved, today=today,
                               candidates=tuple(candidates), selected=selected)


def _iter_values(raw):
    """ThetaData returns a DataFrame, a list, or a dict depending on
    endpoint and version -- normalise without assuming one shape."""
    if raw is None:
        return []
    if hasattr(raw, "itertuples") and hasattr(raw, "columns"):
        cols = [c for c in raw.columns if "expir" in str(c).lower()] or list(raw.columns)[:1]
        return list(raw[cols[0]]) if cols else []
    if isinstance(raw, dict):
        for key in ("response", "expirations", "data"):
            if key in raw:
                return raw[key]
        return []
    return list(raw)


def _contract_count(client, symbol: str, expiration: dt.date) -> int:
    """Counts listed STRIKES for the expiration, doubled for calls+puts.

    Uses option_list_strikes(symbol, expiration) rather than
    option_list_contracts: the latter takes a `request_type` first, not a
    symbol, and passing "QQQ" there produced
    "Unsupported request type: QQQ" -- a real signature error found by
    running it live, not readable from the name."""
    try:
        strikes = client.option_list_strikes(symbol, expiration)
    except Exception as e:  # noqa: BLE001
        logger.warning("strike listing failed for %s %s: %s", symbol, expiration, e)
        return 0
    if strikes is None:
        return 0
    try:
        n = len(strikes)
    except TypeError:
        return 0
    return int(n) * 2
