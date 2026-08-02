"""Proves the daemon event loop is event-driven, not a busy-spin.

Runs the real EventBus loop idle for a fixed wall-clock window with a few
scheduled deadlines, and reports CPU time consumed. A tick() spin loop on
this single-core box would show CPU time approaching wall time; a
condition-parked loop should be a rounding error.

Also measures deadline wakeup accuracy, since parking is only useful if the
loop still wakes on time.
"""
from __future__ import annotations

import os
import resource
import time

from smc import events as ev
from smc.events import EventBus, make_event

WALL_SECONDS = 10.0


def cpu_time() -> float:
    r = resource.getrusage(resource.RUSAGE_SELF)
    return r.ru_utime + r.ru_stime


def main():
    bus = EventBus()
    # A handful of deadlines across the window: the loop must wake for each
    # and park in between.
    t0 = time.monotonic()
    due = [t0 + 2.0, t0 + 4.0, t0 + 6.0, t0 + 8.0]
    for i, d in enumerate(due):
        bus.schedule(d, make_event(ev.EV_DEADLINE, payload={"i": i, "due": d}))

    cpu0 = cpu_time()
    wakeups, lateness = 0, []
    deadline_wall = t0 + WALL_SECONDS
    while time.monotonic() < deadline_wall:
        remaining = deadline_wall - time.monotonic()
        e = bus.get(timeout=max(remaining, 0.01))
        if e is None:
            continue
        wakeups += 1
        if e.event_type == ev.EV_DEADLINE:
            lateness.append((time.monotonic() - e.payload["due"]) * 1000.0)
    cpu_used = cpu_time() - cpu0
    wall = time.monotonic() - t0

    print(f"wall elapsed        : {wall:.2f} s")
    print(f"CPU time consumed   : {cpu_used*1000:.1f} ms")
    print(f"CPU utilisation     : {100.0*cpu_used/wall:.4f} %")
    print(f"deadline wakeups    : {wakeups} (expected {len(due)})")
    if lateness:
        print(f"wakeup lateness ms  : max {max(lateness):.2f}  "
              f"mean {sum(lateness)/len(lateness):.2f}")
    print(f"bus metrics         : {bus.health()}")
    verdict = "EVENT-DRIVEN (idle CPU negligible)" if (cpu_used / wall) < 0.02 \
        else "SUSPECT: too much CPU for an idle loop"
    print(f"\nverdict: {verdict}")


if __name__ == "__main__":
    main()
