"""Corrected process-vs-thread comparison.

The earlier benchmark spawned a NEW multiprocessing.Process for every
replay, so its "process" column charged fork + interpreter import + pickling
the bar frame to each round. A production worker process would be
PERSISTENT: started once, fed bars over a queue, returning results over
another. Charging it startup on every bar made the comparison unfair to the
process option, which is the correction heff asked for.

This measures three placements with the SAME event workload:

    blocking          replay runs as a handler inside the event loop
    thread            replay on a worker thread
    process_persistent replay in a LONG-LIVED worker process (startup paid
                      once, before measurement begins)

Read-only. No orders, no broker, no network.
"""
from __future__ import annotations

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

WORKLOAD = [ev.EV_QUOTE, ev.EV_ORDER_ACK, ev.EV_PARTIAL_FILL, ev.EV_FILL,
            ev.EV_QUOTE, ev.EV_CANCEL, ev.EV_HEALTH]


def _series(n_sessions=6):
    from zoneinfo import ZoneInfo
    et = ZoneInfo("America/New_York")
    df = load_all_bars()
    df["t"] = pd.to_datetime(df["t"], utc=True)
    df["_s"] = [t.astimezone(et).date().isoformat() for t in df["t"]]
    keep = sorted(df["_s"].unique())[-n_sessions:]
    return df[df["_s"].isin(keep)].drop(columns="_s").reset_index(drop=True)


def _replay(raw):
    return run_replay(build_continuous_1min_series(raw), HeffSmcConfig())


def _persistent_worker(inq, outq):
    """Long-lived: imports and warms once, then serves requests forever."""
    raw = inq.get()                      # first message is the bar frame
    outq.put(("ready", 0.0))
    while True:
        msg = inq.get()
        if msg == "stop":
            return
        t0 = time.monotonic()
        events, _ = _replay(raw)
        outq.put((len(events), (time.monotonic() - t0) * 1000.0))


def run(mode: str, raw, rounds: int = 3) -> dict:
    bus = EventBus()
    delays, exit_delays, replay_ms = [], [], []
    stop = threading.Event()
    work = []

    def consumer():
        while not stop.is_set():
            if mode == "blocking" and work:
                work.pop(0)
                t0 = time.monotonic()
                _replay(raw)
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

    proc = inq = outq = None
    if mode == "process_persistent":
        inq, outq = mp.Queue(), mp.Queue()
        proc = mp.Process(target=_persistent_worker, args=(inq, outq), daemon=True)
        proc.start()
        inq.put(raw)
        outq.get(timeout=180)            # startup paid ONCE, before measuring

    for _ in range(rounds):
        done = threading.Event()

        def inject():
            for etype in WORKLOAD:
                bus.publish(make_event(etype))
                time.sleep(0.04)
            done.set()

        threading.Thread(target=inject, daemon=True).start()
        t0 = time.monotonic()
        if mode == "blocking":
            work.append(1)
            while work and not done.is_set():
                time.sleep(0.005)
            time.sleep(0.4)
        elif mode == "thread":
            _replay(raw)
            replay_ms.append((time.monotonic() - t0) * 1000.0)
        else:
            inq.put("go")
            _n, ms = outq.get(timeout=180)
            replay_ms.append(ms)
        done.wait(timeout=5)
        time.sleep(0.25)

    stop.set()
    t.join(timeout=2)
    if proc is not None:
        inq.put("stop")
        proc.join(timeout=10)
        if proc.is_alive():
            proc.terminate()

    def pct(xs, p):
        if not xs:
            return 0.0
        xs = sorted(xs)
        return xs[min(int(p * (len(xs) - 1)), len(xs) - 1)]

    h = bus.health()
    return {
        "mode": mode,
        "crit_events": len(delays),
        "p50_ms": round(pct(delays, 0.50), 2),
        "p95_ms": round(pct(delays, 0.95), 2),
        "p99_ms": round(pct(delays, 0.99), 2),
        "max_ms": round(max(delays) if delays else 0, 2),
        "exit_max_ms": round(max(exit_delays) if exit_delays else 0, 2),
        "replay_ms": round(st.median(replay_ms), 1) if replay_ms else 0.0,
        "depth_hw": h["depth_high_water"],
    }


def main():
    raw = _series()
    print(f"series rows: {len(raw)}   rounds: 3")
    results = [run(m, raw) for m in ("blocking", "thread", "process_persistent")]
    keys = [k for k in results[0] if k != "mode"]
    header = f"{'metric':<14}" + "".join(f"{r['mode']:>20}" for r in results)
    print("")
    print(header)
    print("-" * len(header))
    for k in keys:
        print(f"{k:<14}" + "".join(f"{str(r[k]):>20}" for r in results))
    by = {r["mode"]: r["p95_ms"] for r in results}
    print("")
    print("p95 critical queue delay: " + " | ".join(f"{k} {v} ms" for k, v in by.items()))
    thr, proc = by["thread"], by["process_persistent"]
    print("")
    print(f"thread vs persistent process: {thr} ms vs {proc} ms ->",
          "thread still faster" if thr <= proc else "persistent process faster")


if __name__ == "__main__":
    main()
