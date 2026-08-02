"""Low-latency Alpaca PAPER transport for the hot path.

Why this exists: smc/broker.py sets `self.session = session or requests` --
the bare MODULE. Every `requests.post(...)` builds a Session, opens TCP,
completes a TLS handshake, issues one request and throws the connection
away.

MEASURED, and scoped precisely: this is **connection-reuse saving on an
Alpaca PAPER HTTPS request**, benchmarked with a read-only GET /v2/clock
(n=12) from this box:

    bare requests (handshake per call)   median  90.9 ms   p95  94.3 ms
    pooled + pre-warmed Session          median  11.7 ms   p95  12.1 ms
    -> 79.2 ms saved per HTTPS request, ~7.8x

That is NOT a measurement of order-submission latency. A POST to /v2/orders
carries different server-side work, and its acknowledgement latency has not
been measured. The transport saving is real and applies to any request on
this session; what it implies for an actual order ack is an expectation, not
a result. **Real POST acknowledgement latency is a Monday controlled-PAPER
measurement.**

TWO SAFETY RULES this module enforces, both of which matter more than speed:

1. **max_retries=0: our adapter must never automatically replay an order
    POST.** Stated as our own requirement rather than as a claim about
    library defaults, because the defaults are in fact already safe here --
    requests' HTTPAdapter defaults to max_retries=0, and even a bare
    urllib3.Retry() excludes POST from allowed_methods (OPTIONS/GET/TRACE/
    HEAD/DELETE/PUT). Setting it explicitly makes the guarantee ours and
    survives a future library change or a caller passing its own adapter.
    An automatic replay of a submission whose response was lost would create
    a duplicate position from a request that actually succeeded. A submit
    whose fate is unknown is resolved BY deterministic client_order_id
    lookup, never by blind replay.

    Note urllib3 DOES allow retrying DELETE by default, which would matter
    for cancel if retries were ever enabled; max_retries=0 covers that too,
    and cancel is separately idempotent (cancelling an already-terminal
    order is a no-op we tolerate).

2. **Paper-only, checked at construction.** The guard runs before any socket
    is opened, so a misconfigured runner dies at startup rather than at the
    first order.

Latency is measured with time.monotonic() around the network call itself and
returned with every result, so "how long did submission take" is data the
runner records rather than something reconstructed from log timestamps.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import logging
import time
from typing import Optional

import requests
from requests.adapters import HTTPAdapter

from smc.broker import BrokerRejected, BrokerTimeout
from smc.paper_guard import enforce_paper_mode

logger = logging.getLogger("smc.fast_broker")

DEFAULT_TIMEOUT = 10.0
POOL_CONNECTIONS = 4
POOL_MAXSIZE = 8


@dataclasses.dataclass(frozen=True)
class BrokerCall:
    """One round trip, with the timing the runner needs to prove latency."""
    ok: bool
    status: Optional[int]
    body: Optional[dict]
    started_ts: dt.datetime
    latency_ms: float
    error: Optional[str] = None


class FastPaperBroker:
    def __init__(self, base_url: str, headers: dict, timeout: float = DEFAULT_TIMEOUT,
                 session=None, prewarm: bool = True):
        self.base = base_url.rstrip("/")
        # Raises PaperGuardViolation here, before any socket exists, if this
        # is not the paper endpoint. It does NOT kill the process: the
        # top-level entry point calls paper_guard.fatal_guard() first (which
        # turns a violation into SystemExit(3) while nothing is initialized),
        # and a violation detected later must unwind into the daemon's
        # orderly fail-closed path so SQLite and logs still flush.
        enforce_paper_mode(self.base, (headers or {}).get("APCA-API-KEY-ID"))
        self.headers = dict(headers or {})
        self.timeout = float(timeout)
        self.session = session or self._build_session()
        self._prewarmed = False
        if prewarm:
            self.prewarm()

    @staticmethod
    def _build_session():
        s = requests.Session()
        # max_retries=0 is the safety rule, not a tuning choice. See docstring.
        adapter = HTTPAdapter(pool_connections=POOL_CONNECTIONS,
                              pool_maxsize=POOL_MAXSIZE, max_retries=0)
        s.mount("https://", adapter)
        return s

    # ------------------------------------------------------------- plumbing
    def prewarm(self) -> Optional[BrokerCall]:
        """Pay the TCP+TLS handshake ONCE at startup so the first real order
        does not. Failure is non-fatal: a cold first order is slower, not
        broken, and refusing to start over a warmup blip would be worse."""
        try:
            call = self._request("GET", "/v2/clock")
            self._prewarmed = call.ok
            if call.ok:
                logger.info("broker connection pre-warmed in %.1f ms", call.latency_ms)
            return call
        except Exception as e:  # noqa: BLE001
            logger.warning("broker pre-warm failed (non-fatal): %s", e)
            return None

    @property
    def prewarmed(self) -> bool:
        return self._prewarmed

    def _request(self, method: str, path: str, *, json_body=None, params=None) -> BrokerCall:
        started = dt.datetime.now(dt.timezone.utc)
        t0 = time.monotonic()
        try:
            resp = self.session.request(
                method, self.base + path, headers=self.headers,
                json=json_body, params=params, timeout=self.timeout)
        except Exception as e:  # noqa: BLE001 -- fate unknown for writes
            return BrokerCall(ok=False, status=None, body=None, started_ts=started,
                              latency_ms=(time.monotonic() - t0) * 1000.0, error=repr(e))
        latency_ms = (time.monotonic() - t0) * 1000.0
        try:
            body = resp.json() if resp.content else None
        except ValueError:
            body = {"_raw": resp.text[:500]}
        return BrokerCall(ok=resp.ok, status=resp.status_code, body=body,
                          started_ts=started, latency_ms=latency_ms,
                          error=None if resp.ok else str(body)[:300])

    # ---------------------------------------------------------- order flow
    def submit_order(self, payload: dict) -> BrokerCall:
        """Submits and returns timing. Refuses a payload without a
        client_order_id: without it an unknown-fate submit is unresolvable,
        which is the one situation that can silently double a position."""
        if not payload.get("client_order_id"):
            raise ValueError("refusing to submit an order without a client_order_id")
        call = self._request("POST", "/v2/orders", json_body=payload)
        if call.error and call.status is None:
            raise BrokerTimeout(
                f"submit transport failure for {payload['client_order_id']}: "
                f"{call.error} -- order fate UNKNOWN, resolve by client_order_id, "
                "DO NOT resubmit")
        if not call.ok and call.status and 400 <= call.status < 500:
            raise BrokerRejected(
                f"submit rejected ({call.status}) for {payload['client_order_id']}: "
                f"{call.error}")
        return call

    def cancel_order(self, broker_order_id: str) -> BrokerCall:
        """Requests cancellation. A 2xx here means the request was ACCEPTED,
        NOT that the order is cancelled -- the venue may still fill it. The
        caller must treat cancellation as confirmed only on a `canceled`
        trade_updates event or a REST status read."""
        return self._request("DELETE", f"/v2/orders/{broker_order_id}")

    def get_order_by_client_id(self, client_order_id: str) -> BrokerCall:
        """The ONLY correct way to resolve an unknown-fate submit."""
        return self._request("GET", "/v2/orders:by_client_order_id",
                             params={"client_order_id": client_order_id})

    def open_orders(self) -> BrokerCall:
        return self._request("GET", "/v2/orders", params={"status": "open", "limit": 500})

    def positions(self) -> BrokerCall:
        return self._request("GET", "/v2/positions")

    def get_position_qty(self, occ: str) -> int:
        """Broker-authoritative option quantity for risk and exit sizing."""
        call = self._request("GET", f"/v2/positions/{occ}")
        if call.status == 404:
            return 0
        if not call.ok:
            raise BrokerTimeout(
                f"position lookup failed for {occ}: status={call.status} {call.error}")
        try:
            return abs(int(float((call.body or {}).get("qty", 0))))
        except (TypeError, ValueError) as e:
            raise BrokerTimeout(
                f"position lookup returned invalid qty for {occ}: "
                f"{(call.body or {}).get('qty')!r}") from e

    def clock(self) -> BrokerCall:
        return self._request("GET", "/v2/clock")

    def close(self) -> None:
        try:
            self.session.close()
        except Exception:  # noqa: BLE001
            pass
