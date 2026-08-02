"""READ-ONLY benchmark of the live detector tick, to size the p95<1s target.

Times the three stages the cron detector runs on EVERY tick:
    1. fetch_rolling_bars  -- 6 sessions, one paginated HTTP call each
    2. build_continuous_1min_series
    3. run_replay          -- full 6-session stateful replay

Places no orders and writes no state (seen-set and triggers.jsonl are never
touched).
"""
from __future__ import annotations

import datetime as dt
import statistics as st
import time
from zoneinfo import ZoneInfo

import live_heff_smc_detector as det
from thetadata_pipeline.heff_smc_engine import HeffSmcConfig
from thetadata_pipeline.heff_smc_replay import build_continuous_1min_series, run_replay
from thetadata_pipeline.qqq_bars_fetch import SYMBOL, _headers, fetch_day_bars

ET = ZoneInfo("America/New_York")
N = 3


def main():
    now_et = dt.datetime.now(ET)
    dates = det._recent_session_dates(det.ROLLING_SESSIONS, now_et)
    print(f"rolling window = {det.ROLLING_SESSIONS} sessions: {dates}")
    headers = _headers()

    # --- stage 1: network, per session ---------------------------------
    per_call = []
    for d in dates:
        t0 = time.monotonic()
        df = fetch_day_bars(SYMBOL, d, headers)
        dtms = (time.monotonic() - t0) * 1000.0
        per_call.append(dtms)
        print(f"  fetch_day_bars {d}: {dtms:8.1f} ms  rows={len(df)}")
    print(f"\nstage 1 fetch total (6 sessions): {sum(per_call):.1f} ms "
          f"(median/call {st.median(per_call):.1f} ms)")

    t0 = time.monotonic()
    raw = det.fetch_rolling_bars(now_et)
    fetch_all_ms = (time.monotonic() - t0) * 1000.0
    print(f"stage 1 fetch_rolling_bars end-to-end: {fetch_all_ms:.1f} ms, rows={len(raw)}")

    # --- stage 2 + 3: pure compute, repeated ---------------------------
    build_ms, replay_ms = [], []
    n_events = 0
    for _ in range(N):
        t0 = time.monotonic()
        cont = build_continuous_1min_series(raw)
        build_ms.append((time.monotonic() - t0) * 1000.0)
        t0 = time.monotonic()
        events, _ = run_replay(cont, HeffSmcConfig())
        replay_ms.append((time.monotonic() - t0) * 1000.0)
        n_events = len(events)

    print(f"stage 2 build_continuous_1min_series: median {st.median(build_ms):8.1f} ms")
    print(f"stage 3 run_replay (6 sessions):      median {st.median(replay_ms):8.1f} ms "
          f"({n_events} events, {len(cont)} bars)")

    total = fetch_all_ms + st.median(build_ms) + st.median(replay_ms)
    print(f"\nTOTAL detector tick (network + compute): {total:.1f} ms = {total/1000:.2f} s")
    print(f"compute-only (what an in-memory detector would still pay): "
          f"{st.median(build_ms) + st.median(replay_ms):.1f} ms")
    print(f"\np95<1s target: compute-only "
          f"{'MEETS' if st.median(build_ms)+st.median(replay_ms) < 1000 else 'FAILS'}; "
          f"full tick {'MEETS' if total < 1000 else 'FAILS'}")


if __name__ == "__main__":
    main()
