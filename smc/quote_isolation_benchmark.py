"""Repeatable, no-network proof that reader handoff ignores handler latency."""
from __future__ import annotations

import dataclasses
import json
import time

from smc.quote_worker import QuoteExitWorker


@dataclasses.dataclass
class Quote:
    occ: str
    seq: int


def run_case(handler_delay_seconds: float, submissions: int = 2000) -> dict:
    def handler(_quote):
        time.sleep(handler_delay_seconds)

    worker = QuoteExitWorker(handle_quote=handler, maxsize=32)
    worker.start()
    started = time.monotonic()
    for seq in range(submissions):
        # One OCC intentionally forces useful coalescing while the handler is
        # blocked, rather than manufacturing a huge stale backlog.
        worker.submit(Quote("QQQ260803C00500000", seq))
    submit_batch_ms = (time.monotonic() - started) * 1000.0
    worker.stop(drain=True, timeout=max(2.0, handler_delay_seconds * 4))
    health = worker.health()
    return {
        "injected_handler_delay_ms": handler_delay_seconds * 1000.0,
        "submissions": submissions,
        "submit_batch_ms": round(submit_batch_ms, 3),
        "reader_callback_ms": health["reader_callback_ms"],
        "receive_gap_ms": health["receive_gap_ms"],
        "queue_wait_ms": health["queue_wait_ms"],
        "handler_ms": health["handler_ms"],
        "coalesced": health["coalesced"],
        "errors": health["errors"],
    }


def main() -> None:
    result = {
        "claim": "reader_callback_isolated_from_handler_latency",
        "network_used": False,
        "cases": [run_case(delay) for delay in (0.010, 0.050, 0.100, 0.500)],
    }
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
