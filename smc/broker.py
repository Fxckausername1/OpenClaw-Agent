"""Broker adapter for SMC execution.

Two properties this exists to guarantee, both of which the 2026-07-31 code
violated:

1. A POST that times out is NOT a failed order. The old `submit_entry` /
   `submit_close` caught the exception and returned `{"submitted": False}`, which
   the caller treated as "nothing happened." Alpaca may well have accepted that
   order. Because every order here carries a deterministic client_order_id, a
   timeout is resolvable: `resolve_after_timeout()` asks the broker "do you have
   this client_order_id?" and returns the real answer instead of a guess.

2. Alpaca's `indicative` options feed is NOT NBBO. The old urgent-exit fix priced
   a limit 5% under an indicative bid and called it marketable -- it isn't
   necessarily, because indicative can lag the real book, which is exactly how a
   stop rested unfilled for 10 minutes while the market fell. `get_quote` reports
   which feed a quote came from and whether it is real NBBO, and callers must not
   describe indicative data as NBBO.

The `Broker` protocol is what execution code depends on, so tests inject a fake
and never touch the network.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import logging
import time
from typing import Optional, Protocol

logger = logging.getLogger("smc.broker")

FEED_OPRA = "opra"
FEED_INDICATIVE = "indicative"
# ThetaData option quotes are OPRA NBBO delivered by a different provider
# -- a real NBBO source, not a derived/aggregated one.
FEED_THETADATA = "thetadata"
NBBO_FEEDS = frozenset({FEED_OPRA, FEED_THETADATA})

# Alpaca order statuses -> our lifecycle vocabulary.
BROKER_TERMINAL_FILLED = ("filled",)
BROKER_TERMINAL_UNFILLED = ("canceled", "expired", "rejected", "replaced", "done_for_day")
BROKER_WORKING = ("new", "accepted", "pending_new", "partially_filled", "accepted_for_bidding")
BROKER_PENDING_CANCEL = ("pending_cancel", "pending_replace")


class BrokerTimeout(Exception):
    """The request did not complete. The order's fate is UNKNOWN, never assumed
    failed -- resolve it by client_order_id."""


class BrokerRejected(Exception):
    """The broker explicitly refused the order (e.g. market orders unsupported for
    this option). Distinct from a timeout: this one IS a definitive no."""


@dataclasses.dataclass(frozen=True)
class Quote:
    occ: str
    bid: Optional[float]
    ask: Optional[float]
    bid_size: Optional[float]
    ask_size: Optional[float]
    ts: Optional[str]
    feed: str

    @property
    def is_nbbo(self) -> bool:
        """Real NBBO only. Indicative is a derived/aggregated feed and must
        never be presented as NBBO.

        ThetaData counts: its option quotes are OPRA NBBO carried by a
        different provider, not a synthesised book. Widening this to a set
        rather than relabelling ThetaData as "opra" keeps WHICH provider
        priced a fill visible in the record."""
        return self.feed in NBBO_FEEDS

    @property
    def two_sided(self) -> bool:
        return (self.bid is not None and self.ask is not None
                and self.bid > 0 and self.ask > 0 and self.bid < self.ask)

    @property
    def label(self) -> str:
        if self.feed == FEED_THETADATA:
            return "NBBO/ThetaData"
        return "NBBO/OPRA" if self.is_nbbo else "INDICATIVE (not NBBO)"


@dataclasses.dataclass(frozen=True)
class BrokerOrder:
    broker_order_id: Optional[str]
    client_order_id: Optional[str]
    occ: Optional[str]
    status: Optional[str]
    filled_qty: int
    intended_qty: int
    avg_fill_price: Optional[float]
    raw: dict = dataclasses.field(default_factory=dict)

    @property
    def is_filled(self) -> bool:
        return self.status in BROKER_TERMINAL_FILLED

    @property
    def is_terminal(self) -> bool:
        return self.status in BROKER_TERMINAL_FILLED + BROKER_TERMINAL_UNFILLED

    @property
    def is_terminal_unfilled(self) -> bool:
        return self.status in BROKER_TERMINAL_UNFILLED

    @property
    def is_working(self) -> bool:
        return self.status in BROKER_WORKING + BROKER_PENDING_CANCEL


class Broker(Protocol):
    def submit_order(self, payload: dict) -> BrokerOrder: ...
    def get_order_by_client_id(self, client_order_id: str) -> Optional[BrokerOrder]: ...
    def cancel_order(self, broker_order_id: str) -> None: ...
    def get_position_qty(self, occ: str) -> int: ...
    def list_option_positions(self) -> list: ...
    def list_open_orders(self) -> list: ...
    def get_quote(self, occ: str) -> Optional[Quote]: ...
    def get_calendar(self, start: dt.date, end: dt.date) -> list: ...


class AlpacaBroker:
    """Real Alpaca adapter. Reuses options_orchestrator's credentials/base URLs so
    this is the SAME coordinated API consumer as the rest of the box, not a second
    uncoordinated one competing for the same rate limit."""

    def __init__(self, session=None, paper_base=None, data_base=None, headers=None,
                 timeout: float = 20.0, opra_reprobe_seconds: float = 600.0):
        import requests  # local import keeps this module importable without network deps
        import options_orchestrator as oo
        self._requests = requests
        self.session = session or requests
        self.paper = paper_base or oo.PAPER
        self.data = data_base or oo.DATA
        self.headers = headers or oo.H
        self.timeout = timeout
        self._opra_available: Optional[bool] = None
        self._opra_checked_at: Optional[float] = None
        self._opra_reprobe_seconds = float(opra_reprobe_seconds)

    # ------------------------------------------------------------ order flow
    def submit_order(self, payload: dict) -> BrokerOrder:
        """payload MUST include client_order_id. Raises BrokerTimeout on any
        transport failure (fate unknown) and BrokerRejected on an explicit refusal."""
        if not payload.get("client_order_id"):
            raise ValueError("refusing to submit an order without a client_order_id")
        try:
            resp = self.session.post(self.paper + "/v2/orders", headers=self.headers,
                                      json=payload, timeout=self.timeout)
        except Exception as e:  # noqa: BLE001 -- transport failure: fate is UNKNOWN
            raise BrokerTimeout(
                f"submit transport failure for client_order_id={payload['client_order_id']}: {e} "
                "-- order fate UNKNOWN, must be resolved by client_order_id"
            ) from e

        if resp.status_code in (200, 201):
            return _parse_order(resp.json())
        body = (resp.text or "")[:400]
        if resp.status_code in (403, 422):
            raise BrokerRejected(f"broker refused order ({resp.status_code}): {body}")
        # 5xx / 429 / anything else: outcome genuinely ambiguous, treat as timeout.
        raise BrokerTimeout(f"submit returned {resp.status_code}, outcome ambiguous: {body}")

    def get_order_by_client_id(self, client_order_id: str) -> Optional[BrokerOrder]:
        """The timeout-resolution primitive. Returns None ONLY when the broker
        affirmatively reports it has never seen this id (404)."""
        try:
            resp = self.session.get(self.paper + "/v2/orders:by_client_order_id",
                                     headers=self.headers,
                                     params={"client_order_id": client_order_id},
                                     timeout=self.timeout)
        except Exception as e:  # noqa: BLE001
            raise BrokerTimeout(f"could not look up client_order_id={client_order_id}: {e}") from e
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            raise BrokerTimeout(
                f"ambiguous lookup for {client_order_id}: HTTP {resp.status_code} "
                f"{(resp.text or '')[:200]} -- refusing to conclude the order does not exist"
            )
        return _parse_order(resp.json())

    def cancel_order(self, broker_order_id: str) -> None:
        try:
            resp = self.session.delete(self.paper + f"/v2/orders/{broker_order_id}",
                                        headers=self.headers, timeout=self.timeout)
        except Exception as e:  # noqa: BLE001
            raise BrokerTimeout(f"cancel transport failure for {broker_order_id}: {e}") from e
        # 404 = already gone; 422 = already terminal. Both mean "not working anymore".
        if resp.status_code not in (200, 204, 404, 422):
            raise BrokerTimeout(f"cancel returned {resp.status_code} for {broker_order_id}")

    # ------------------------------------------------------------- positions
    def list_option_positions(self) -> list:
        try:
            resp = self.session.get(self.paper + "/v2/positions", headers=self.headers,
                                     timeout=self.timeout)
            resp.raise_for_status()
        except Exception as e:  # noqa: BLE001
            raise BrokerTimeout(f"position read failed: {e}") from e
        return [p for p in resp.json() if p.get("asset_class") == "us_option"]

    def get_position_qty(self, occ: str) -> int:
        for p in self.list_option_positions():
            if p.get("symbol") == occ:
                return int(float(p.get("qty") or 0))
        return 0

    def list_open_orders(self) -> list:
        try:
            resp = self.session.get(self.paper + "/v2/orders", headers=self.headers,
                                     params={"status": "open", "nested": "true", "limit": 500},
                                     timeout=self.timeout)
            resp.raise_for_status()
        except Exception as e:  # noqa: BLE001
            raise BrokerTimeout(f"open-order read failed: {e}") from e
        return [_parse_order(o) for o in resp.json()]

    # ---------------------------------------------------------------- quotes
    def get_quote(self, occ: str) -> Optional[Quote]:
        """Tries OPRA first (real NBBO) and falls back to indicative, labelling
        which one it actually got. A negative OPRA probe is cached for
        opra_reprobe_seconds -- see _feed_order for why it is a TTL rather than
        either a permanent latch or no cache at all."""
        for feed in self._feed_order():
            q = self._fetch_quote(occ, feed)
            if q is not None:
                if feed == FEED_OPRA:
                    self._opra_available = True
                    self._opra_checked_at = time.monotonic()
                return q
            if feed == FEED_OPRA:
                self._opra_available = False
                self._opra_checked_at = time.monotonic()
                logger.warning(
                    "OPRA quote unavailable for %s -- falling back to INDICATIVE. Any "
                    "marketability judgement from this data is an estimate, NOT real NBBO.", occ)
        return None

    def _feed_order(self) -> tuple:
        """OPRA first, unless a recent probe said it isn't available.

        The negative result is cached with a TTL rather than latched permanently.
        Two failure modes are being balanced:
          * latching forever (the original behaviour) means one transient blip
            silently downgrades every later quote for the process lifetime;
          * not caching at all (how this was left after the 2026-08-01 review)
            means every quote pays a doomed 403 round-trip -- and on this account
            OPRA is *permanently* unavailable ("agreement is not signed"), so
            that cost is paid on every single sub-second urgent-exit poll.
        A TTL gets both: no permanent latch, no per-call 403, and a newly-signed
        OPRA agreement is picked up automatically without a restart."""
        if self._opra_available is False and self._opra_checked_at is not None:
            age = time.monotonic() - self._opra_checked_at
            if age < self._opra_reprobe_seconds:
                return (FEED_INDICATIVE,)
        return (FEED_OPRA, FEED_INDICATIVE)

    def _fetch_quote(self, occ: str, feed: str) -> Optional[Quote]:
        try:
            resp = self.session.get(self.data + "/v1beta1/options/quotes/latest",
                                     headers=self.headers,
                                     params={"symbols": occ, "feed": feed},
                                     timeout=self.timeout)
        except Exception as e:  # noqa: BLE001
            logger.warning("quote fetch failed for %s feed=%s: %s", occ, feed, e)
            return None
        if resp.status_code != 200:
            return None
        qq = (resp.json().get("quotes") or {}).get(occ)
        if not qq:
            return None
        return Quote(occ=occ, bid=qq.get("bp"), ask=qq.get("ap"), bid_size=qq.get("bs"),
                     ask_size=qq.get("as"), ts=qq.get("t"), feed=feed)

    def opra_available(self) -> Optional[bool]:
        return self._opra_available

    # -------------------------------------------------------------- calendar
    def get_calendar(self, start: dt.date, end: dt.date) -> list:
        try:
            resp = self.session.get(self.paper + "/v2/calendar", headers=self.headers,
                                     params={"start": start.isoformat(), "end": end.isoformat()},
                                     timeout=self.timeout)
            resp.raise_for_status()
        except Exception as e:  # noqa: BLE001
            logger.warning("calendar fetch failed: %s", e)
            return []
        return resp.json()


def _parse_order(o: dict) -> BrokerOrder:
    return BrokerOrder(
        broker_order_id=o.get("id"),
        client_order_id=o.get("client_order_id"),
        occ=o.get("symbol"),
        status=o.get("status"),
        filled_qty=int(float(o.get("filled_qty") or 0)),
        intended_qty=int(float(o.get("qty") or 0)),
        avg_fill_price=(float(o["filled_avg_price"])
                        if o.get("filled_avg_price") not in (None, "") else None),
        raw=o,
    )


def resolve_after_timeout(broker: Broker, client_order_id: str) -> Optional[BrokerOrder]:
    """The rule the old code got wrong. After a transport failure NEVER conclude
    "no order exists" -- ask the broker by client_order_id. Returns:
      * BrokerOrder  -- the broker has it; use its real status/filled_qty
      * None         -- broker affirmatively 404s: the order truly never landed
    Re-raises BrokerTimeout if even the lookup is ambiguous, so the caller keeps
    the local row in SUBMITTED (unknown) rather than falsely finalising it."""
    return broker.get_order_by_client_id(client_order_id)
