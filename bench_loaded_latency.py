"""Hot-path interference test: how does the event loop behave WHILE the
~467 ms detector replay is running?

Idle CPU was already shown to be negligible (0.0114%). That is not the
question that matters. The question is whether a CPU-bound replay delays a
fill, a stop-triggering quote or a cancel -- because managing an existing
position is more urgent than detecting a new entry.

Runs the identical event workload under THREE placements of the replay:

    blocking -- replay runs as a HANDLER INSIDE the event loop (true worst
                case: the loop cannot drain while it computes)
    thread   -- replay on a separate thread from the loop (pandas/numpy
                release the GIL frequently, so the loop keeps draining)
    process  -- replay in a multiprocessing worker

The distinction between `blocking` and `thread` is the whole point, and it
was easy to get wrong: an earlier version of this benchmark ran the replay
on the main thread while the consumer drained on another thread, called that
"inline", and reported a 0.1 ms p95 -- which measured GIL release, not loop
blocking.

and reports queue delay percentiles for the CRITICAL events specifically,
plus exit-trigger-to-submit delay.

This box is SINGLE-CORE, so a worker process does not get free parallelism:
it competes for the same CPU. The point is to measure that rather than
assume process isolation fixes it.

Read-only. No orders, no broker, no network.
"""
from __future__ import annotations

import argparse
import multiprocessing as mp
import statistics as st
import threading
import time

import pandas as pd

from smc import events as ev
from smc.events import EventBus, make_event
from thetadata_pipeline.heff_smc_engine import HeffSmcConfig
from thetadata_pipeline.heff_smc_replay import build_continuous_1min_series, run_replay
from thetadata_pipeline.qqq_bars_fetch import load_all_bars

CRITICAL_WORKLOAD = [
    (ev.EV_QUOTE, "quote"),
    (ev.EV_ORDER_ACK, "alpaca ack"),
    (ev.EV_PARTIAL_FILL, "partial fill"),
    (ev.EV_FILL, "final fill"),
    (ev.EV_QUOTE, "stop-triggering quote"),
    (ev.EV_CANCEL, "cancellation"),
    (ev.EV_HEALTH, "dashboard/telegram work"),
]


def _series(n_sessions=6):
    df = load_all_bars()
    df["t"] = pd.to_datetime(df["t"], utc=True)
    from zoneinfo import ZoneInfo
    et = ZoneInfo("America/New_York")
    df["_s"] = [t.astimezone(et).date().isoformat() for t in df["t"]]
    keep = sorted(df["_s"].unique())[-n_sessions:]
    return df[df["_s"].isin(keep)].drop(columns="_s").reset_index(drop=True)


def _replay(raw):
    return run_replay(build_continuous_1min_series(raw), HeffSmcConfig())


def _worker(raw, q):
    t0 = time.monotonic()
    events, _ = _replay(raw)
    q.put((len(events), (time.monotonic() - t0) * 1000.0))


def run(mode: str, raw, rounds: int = 3) -> dict:
    bus = EventBus()
    delays, exit_delays, replay_ms = [], [], []
    stop = threading.Event()

    work = []          # replay jobs the loop must execute itself (blocking mode)

    def consumer():
        while not stop.is_set():
            if mode == "blocking" and work:
                work.pop(0)
                t0 = time.monotonic()
                _replay(raw)                      # BLOCKS the loop
                replay_ms.append((time.monotonic() - t0) * 1000.0)
                continue
            e = bus.get(timeout=0.05)
            if e is None:
                continue
            now = time.monotonic()
            d = e.queue_delay_seconds(now)
            if d is not None and e.critical:
                delays.append(d * 1000.0)
            if e.event_type == ev.EV_CANCEL and d is not None:
                exit_delays.append(d * 1000.0)
            bus.record_handled(e, now, 0.0005)

    t = threading.Thread(target=consumer, daemon=True)
    t.start()

    for _ in range(rounds):
        injector_done = threading.Event()

        def inject():
            # Fire the workload DURING the replay, spaced across it.
            for etype, label in CRITICAL_WORKLOAD:
                bus.publish(make_event(etype, payload={"label": label}))
                time.sleep(0.04)
            injector_done.set()

        inj = threading.Thread(target=inject, daemon=True)
        inj.start()

        t0 = time.monotonic()
        if mode == "blocking":
            work.append(1)                        # loop picks it up and blocks
            while work and not injector_done.is_set():
                time.sleep(0.005)
            time.sleep(0.5)
        elif mode == "thread":
            _replay(raw)
            replay_ms.append((time.monotonic() - t0) * 1000.0)
        else:
            q = mp.Queue()
            p = mp.Process(target=_worker, args=(raw, q))
            p.start()
            p.join(timeout=120)
            try:
                _n, ms = q.get_nowait()
            except Exception:  # noqa: BLE001
                ms = (time.monotonic() - t0) * 1000.0
            replay_ms.append(ms)
        injector_done.wait(timeout=5)
        time.sleep(0.3)

    stop.set()
    t.join(timeout=2)

    def pct(xs, p):
        if not xs:
            return None
        xs = sorted(xs)
        return xs[min(int(p * (len(xs) - 1)), len(xs) - 1)]

    h = bus.health()
    return {
        "mode": mode,
        "critical_events": len(delays),
        "queue_delay_p50_ms": round(pct(delays, 0.50) or 0, 2),
        "queue_delay_p95_ms": round(pct(delays, 0.95) or 0, 2),
        "queue_delay_p99_ms": round(pct(delays, 0.99) or 0, 2),
        "queue_delay_max_ms": round(max(delays) if delays else 0, 2),
        "exit_path_delay_max_ms": round(max(exit_delays) if exit_delays else 0, 2),
        "replay_ms_median": round(st.median(replay_ms), 1),
        "max_loop_lag_ms": h["max_loop_lag_ms"],
        "depth_high_water": h["depth_high_water"],
        "dropped_informational": h["dropped_informational"],
        "coalesced_informational": h["coalesced_informational"],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=3)
    args = ap.parse_args()
    raw = _series()
    print(f"series rows: {len(raw)}")
    results = [run(m, raw, args.rounds) for m in ("blocking", "thread", "process")]
    keys = [k for k in results[0] if k != "mode"]
    names = [r["mode"] for r in results]
    header = f"{'metric':<28}" + "".join(f"{n:>12}" for n in names)
    print("")
    print(header)
    print("-" * len(header))
    for k in keys:
        print(f"{k:<28}" + "".join(f"{str(r[k]):>12}" for r in results))
    by = {r["mode"]: r["queue_delay_p95_ms"] for r in results}
    print("")
    print("p95 critical queue delay: " +
          " | ".join(f"{n} {by[n]} ms" for n in names))
    print("")
    print("verdict: running the replay ON the event loop is",
          "UNACCEPTABLE" if by.get("blocking", 0) > 50 else "tolerable")
    print("         it must not execute as a loop handler.")


if __name__ == "__main__":
    main()
