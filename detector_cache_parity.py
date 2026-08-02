"""Cache-parity proof for smc/detector.py.

The replay function is unchanged, but the INPUT ASSEMBLY is new: history now
comes from an in-memory cache instead of six fresh fetches, and today's frame
is appended incrementally. build_continuous_1min_series reindexes onto a
390-minute grid and forward-fills, so a different assembly path could in
principle yield a different series. That makes parity a tested property, not
an assumed one.

Method: for every one of the 162 sessions, run the PersistentDetector with
cached prior sessions and today's bars fed INCREMENTALLY in chunks (so the
append/dedup/cursor path is genuinely exercised, not bypassed), then compare
every emitted signal against the frozen v2.2 event set.

Also runs restart-and-dedup simulations at five points, because the property
that matters operationally is not just "same events" but "never the same
event twice".

Read-only: no orders, no network, no state writes outside a temp dir.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from smc.detector import PersistentDetector
from thetadata_pipeline.bt3_b1_v22 import V22_EVENTS_PATH
from thetadata_pipeline.heff_smc_engine import HeffSmcConfig
from thetadata_pipeline.qqq_bars_fetch import load_all_bars

ET = ZoneInfo("America/New_York")
CHUNKS = 4          # how many incremental appends per session


def build_session_index() -> dict:
    df = load_all_bars()
    df["t"] = pd.to_datetime(df["t"], utc=True)
    df["_sess"] = [t.astimezone(ET).date().isoformat() for t in df["t"]]
    return {s: g.drop(columns="_sess").sort_values("t").reset_index(drop=True)
            for s, g in df.groupby("_sess")}


def frozen_events() -> dict:
    payload = json.loads(Path(V22_EVENTS_PATH).read_text())
    events = payload["events"] if isinstance(payload, dict) else payload
    out: dict = {}
    for e in events:
        out.setdefault(e["session"], []).append(e)
    return out


def key_of(e) -> tuple:
    """Stable identity: session, time, side, trigger, score, price.

    bar_index is deliberately EXCLUDED. It is an index into whichever series
    was replayed, so the frozen set (162-session continuous) and the live
    detector (6-session rolling window) legitimately differ by a constant
    multiple of 390 bars. Comparing it would report a mismatch on a signal
    that is identical in every meaningful field. `index_offsets` below proves
    the difference really is a pure constant offset per session and not a
    reordering hiding behind a matching count."""
    if isinstance(e, dict):
        return (e["session"], e["time"], e["side"], e["trigger"],
                round(float(e["score"]), 6), round(float(e["price"]), 6))
    return (e.session, e.bar_time_et, e.side, e.trigger,
            round(e.score, 6), round(e.price, 6))


def index_offsets(frozen_list, emitted) -> set:
    """Per-session set of (frozen_bar_index - emitted_bar_index). A single
    value means a pure window-origin shift; more than one means real
    reordering."""
    by_id = {(e["session"], e["time"], e["side"]): int(e["bar_index"])
             for e in frozen_list}
    out = set()
    for e in emitted:
        k = (e.session, e.bar_time_et, e.side)
        if k in by_id:
            out.add(by_id[k] - e.bar_index)
    return out


def run_session(sessions: list, idx: int, index: dict, tmpdir: Path,
                chunks: int = CHUNKS, restart_at: int = None,
                tag: str = "") -> list:
    """Runs one session incrementally. `restart_at` rebuilds the detector
    from its persisted cursor after that chunk, simulating a crash."""
    session = sessions[idx]
    prior = sessions[max(0, idx - 5):idx]
    # Each invocation gets its OWN cursor file. Sharing one across runs
    # made the restart baseline read a cursor that already had every
    # signal marked emitted, so it correctly emitted nothing -- a harness
    # bug that looked like a detector bug.
    cursor_path = tmpdir / f"cursor_{session}{tag}.json"
    cursor_path.parent.mkdir(parents=True, exist_ok=True)

    today = index[session]
    bounds = [len(today) * (i + 1) // chunks for i in range(chunks)]

    def make():
        det = PersistentDetector(
            fetch_session_bars=lambda s: (index.get(s) if s != session
                                          else today.iloc[:0].copy()),
            cursor_path=cursor_path, config=HeffSmcConfig())
        det.warmup(session=session, prior_sessions=prior)
        return det

    det = make()
    emitted = []
    for n, upto in enumerate(bounds):
        det.ingest_today(today.iloc[:upto].copy())
        emitted.extend(det.detect())
        if restart_at is not None and n == restart_at:
            det = make()                      # cursor reloaded from disk
            det.ingest_today(today.iloc[:upto].copy())
            emitted.extend(det.detect())      # must emit NOTHING new
    return emitted


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--out", default="DETECTOR_CACHE_PARITY.json")
    args = ap.parse_args()

    index = build_session_index()
    frozen = frozen_events()
    sessions = sorted(index)
    if args.limit:
        sessions_to_run = sessions[:args.limit]
    else:
        sessions_to_run = sessions

    tmp = Path(tempfile.mkdtemp(prefix="detparity_"))
    all_emitted, mismatches, offset_anomalies = [], [], []
    dup_emissions = 0

    for i, s in enumerate(sessions_to_run):
        emitted = run_session(sessions, i, index, tmp, tag="_main")
        keys = [key_of(e) for e in emitted]
        if len(keys) != len(set(keys)):
            dup_emissions += len(keys) - len(set(keys))
        all_emitted.extend(emitted)

        offs = index_offsets(frozen.get(s, []), emitted)
        if len(offs) > 1:
            offset_anomalies.append({"session": s, "offsets": sorted(offs)})
        want = sorted(key_of(e) for e in frozen.get(s, []))
        got = sorted(keys)
        if want != got:
            mismatches.append({
                "session": s,
                "missing": [list(k) for k in sorted(set(want) - set(got))][:8],
                "extra": [list(k) for k in sorted(set(got) - set(want))][:8],
                "n_want": len(want), "n_got": len(got)})
        if (i + 1) % 20 == 0:
            print(f"  ... {i+1}/{len(sessions_to_run)} sessions, "
                  f"{len(all_emitted)} events, {len(mismatches)} mismatched",
                  flush=True)

    total_frozen = sum(len(frozen.get(s, [])) for s in sessions_to_run)

    # ---- restart / dedup simulations ---------------------------------
    restart_results = []
    probe_sessions = [s for s in sessions_to_run if frozen.get(s)][:5]
    for n, s in enumerate(probe_sessions):
        idx = sessions.index(s)
        base = run_session(sessions, idx, index, tmp, tag=f"_base{n}")
        with_restart = run_session(sessions, idx, index, tmp,
                                   restart_at=n % CHUNKS, tag=f"_restart{n}")
        bk = sorted(key_of(e) for e in base)
        rk = sorted(key_of(e) for e in with_restart)
        restart_results.append({
            "session": s, "restart_after_chunk": n % CHUNKS,
            "events_without_restart": len(bk), "events_with_restart": len(rk),
            "identical": bk == rk,
            "duplicate_emissions": len(rk) - len(set(rk))})

    report = {
        "sessions_run": len(sessions_to_run),
        "frozen_events_expected": total_frozen,
        "events_emitted": len(all_emitted),
        "sessions_mismatched": len(mismatches),
        "duplicate_emissions": dup_emissions,
        "mismatch_examples": mismatches[:5],
        "index_offset_anomalies": offset_anomalies[:5],
        "restart_simulations": restart_results,
        "PARITY_PASS": (not mismatches and not offset_anomalies
                        and dup_emissions == 0
                        and len(all_emitted) == total_frozen
                        and all(r["identical"] and r["duplicate_emissions"] == 0
                                for r in restart_results)),
    }
    Path(args.out).write_text(json.dumps(report, indent=2))

    print("=" * 62)
    print("DETECTOR CACHE PARITY")
    print("=" * 62)
    print(f"sessions run              : {report['sessions_run']}")
    print(f"frozen events expected    : {total_frozen}")
    print(f"events emitted (cached)   : {len(all_emitted)}")
    print(f"sessions mismatched       : {len(mismatches)}")
    print(f"duplicate emissions       : {dup_emissions}")
    print(f"index-offset anomalies    : {len(offset_anomalies)}")
    print("- restart simulations -")
    for r in restart_results:
        print(f"  {r['session']} restart@chunk{r['restart_after_chunk']}: "
              f"{r['events_without_restart']} vs {r['events_with_restart']} "
              f"identical={r['identical']} dups={r['duplicate_emissions']}")
    print("-" * 62)
    print(f"PARITY_PASS: {report['PARITY_PASS']}")
    return 0 if report["PARITY_PASS"] else 1


if __name__ == "__main__":
    sys.exit(main())
