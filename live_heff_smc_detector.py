"""Live HEFF-SMC triangle detector -- Phase 1 of wiring the validated
strategy (triangle signal + moderate_combo selector + baseline exit) to
real paper execution, replacing the options tournament's ORB/MR triggers
with the actual validated signal.

Design: rather than persisting HeffSmcEngine's internal state between cron
ticks (fragile -- pickle risk, session-boundary edge cases), this re-runs
the SAME already-validated run_replay() fresh each tick over a rolling
window (last few sessions + today so far). Continuity (structure/FVG/OB/
pools/HTF carried across session boundaries) falls out naturally from
replaying a multi-day window every time, with zero new state-persistence
code -- 100% reuse of tested backtest code, only the INPUT (live bars
instead of historical) and OUTPUT (react to new events) are new.

Cron-driven (this box's established pattern, no daemons -- see
collector.py's own docstring for why). Each tick: pull a rolling window of
real bars, replay with the UNMODIFIED HeffSmcConfig (same as B1/backtest --
this is Phase 1, detection only, no config changes), find any event in
TODAY's session not already in the persisted seen-set, record it as a new
live trigger.

SHADOW MODE ONLY in this version: writes detected triggers to
data/live_heff_smc/triggers.jsonl and logs them -- does NOT place any
order. Phase 2 (contract selection + entry) consumes this file separately,
kept as its own explicit step rather than combined here, so detection can
be verified live before anything touches real (paper) order placement.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import subprocess
from pathlib import Path
from zoneinfo import ZoneInfo

from thetadata_pipeline.heff_smc_engine import HeffSmcConfig
from thetadata_pipeline.heff_smc_replay import build_continuous_1min_series, run_replay
from thetadata_pipeline.qqq_bars_fetch import SYMBOL, _headers, fetch_day_bars

import pandas as pd

ET = ZoneInfo("America/New_York")
ROOT = Path(__file__).resolve().parent  # this script lives at workspace root, not one level down
OUT_DIR = ROOT / "data" / "live_heff_smc"
SEEN_PATH = OUT_DIR / "seen_events.json"
TRIGGERS_PATH = OUT_DIR / "triggers.jsonl"
LOG_PATH = ROOT / "logs" / "live_heff_smc_detector.log"

ROLLING_SESSIONS = 6  # enough real history for HTF/pivot lookback with margin

logger = logging.getLogger("live_heff_smc_detector")


def _log(msg: str) -> None:
    line = f"{dt.datetime.now(dt.timezone.utc).isoformat()} {msg}"
    print(line, flush=True)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with open(LOG_PATH, "a") as f:
        f.write(line + "\n")


def _load_seen() -> set:
    if not SEEN_PATH.exists():
        return set()
    try:
        return set(json.loads(SEEN_PATH.read_text()))
    except Exception:
        return set()


def _save_seen(seen: set) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    tmp = SEEN_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(sorted(seen)))
    tmp.replace(SEEN_PATH)


def _notify_telegram(msg: str) -> None:
    try:
        subprocess.run(
            ["/usr/bin/openclaw", "message", "send", "--channel", "telegram",
             "--target", "7590346809", "--message", msg],
            capture_output=True, timeout=150,
        )
    except Exception as e:
        _log(f"WARNING: telegram notify failed: {e}")


def _recent_session_dates(n: int, now_et: dt.datetime) -> list:
    """Last n calendar weekdays up to and including today -- doesn't need
    to be an exact trading-calendar match (a stray holiday just yields one
    fewer real session in the window, harmless for lookback purposes)."""
    dates = []
    d = now_et.date()
    while len(dates) < n:
        if d.weekday() < 5:
            dates.append(d.isoformat())
        d -= dt.timedelta(days=1)
    return list(reversed(dates))


def fetch_rolling_bars(now_et: dt.datetime) -> pd.DataFrame:
    headers = _headers()
    dates = _recent_session_dates(ROLLING_SESSIONS, now_et)
    frames = []
    for date in dates:
        try:
            df = fetch_day_bars(SYMBOL, date, headers)
            if not df.empty:
                frames.append(df)
        except RuntimeError as e:
            _log(f"WARNING: bar fetch failed for {date}: {e}")
    if not frames:
        return pd.DataFrame(columns=["t", "o", "h", "l", "c", "v"])
    return pd.concat(frames, ignore_index=True).sort_values("t").reset_index(drop=True)


def run_detection_tick() -> dict:
    now_et = dt.datetime.now(ET)
    today = now_et.date().isoformat()

    raw_bars = fetch_rolling_bars(now_et)
    if raw_bars.empty:
        _log("no bars available this tick, skipping")
        return {"status": "no_bars"}

    continuous = build_continuous_1min_series(raw_bars)
    events, _diag = run_replay(continuous, HeffSmcConfig())  # unmodified live defaults, same as B1

    today_events = [e for e in events if e["session"] == today]
    seen = _load_seen()
    new_events = [e for e in today_events if f"{e['session']}:{e['bar_index']}:{e['side']}" not in seen]

    if new_events:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        with open(TRIGGERS_PATH, "a") as f:
            for e in new_events:
                record = {**e, "detected_at": dt.datetime.now(dt.timezone.utc).isoformat()}
                f.write(json.dumps(record) + "\n")
                seen.add(f"{e['session']}:{e['bar_index']}:{e['side']}")
                _log(f"NEW TRIGGER: {e['side'].upper()} {e['trigger']} score={e['score']:.2f} "
                     f"price={e['price']} time={e['time']}")
        _save_seen(seen)
        lines = ["Real HEFF-SMC triangle(s) fired on QQQ:"]
        for e in new_events:
            lines.append(f"{e['side'].upper()} {e['trigger']} score={e['score']:.2f} "
                         f"price=${e['price']} @ {e['time']}")
        lines.append("Selector is checking for a matching contract now (live wiring, armed).")
        _notify_telegram("\n".join(lines))

    return {"status": "ok", "today_events": len(today_events), "new_events": len(new_events)}


def main():
    logging.basicConfig(level=logging.INFO)
    result = run_detection_tick()
    _log(f"tick complete: {result}")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
