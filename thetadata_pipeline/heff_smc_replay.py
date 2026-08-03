"""BT-3 B1 replay driver -- runs HeffSmcEngine bar-by-bar across the real,
continuous 62-session QQQ 1-min bar history (qqq_bars_fetch.py's own output)
plus HTF bias precomputed by heff_smc_htf.py, producing a real, timestamped
list of "triangle" events (Module 10's own JSON-alert shape: side, trigger,
score, factor breakdown) for BT-3 B1 to feed into BT-2.

Isolation: reads qqq_1min_bars/ (this project's own output, never
backfill_60session/raw) and writes only under data/thetadata/heff_smc_replay/.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import pandas as pd

from .heff_smc_engine import HeffSmcConfig, HeffSmcEngine
from .heff_smc_htf import align_htf_dir_to_1min, compute_htf_dir_series
from .qqq_bars_fetch import BARS_RAW_DIR, SYMBOL, list_target_sessions, load_all_bars

ET = ZoneInfo("America/New_York")

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "thetadata"
REPLAY_DIR = DATA / "heff_smc_replay"
TRIANGLE_EVENTS_PATH = REPLAY_DIR / "triangle_events.json"

logger = logging.getLogger("thetadata_pkg.heff_smc_replay")


def build_continuous_1min_series(raw: pd.DataFrame) -> pd.DataFrame:
    """Reindexes each session onto its full 09:30-15:59 ET 390-minute grid
    and fills any minute Alpaca's IEX-only feed returned no real trade for:
    interior/trailing gaps carry the last REAL close forward (O=H=L=C set to
    that close, V=0 -- the standard "no trade this minute" convention, also
    what a full consolidated-tape feed would show as a flat print); a rare
    LEADING gap (the session's own first minute(s) missing) is seeded
    backward from the next real print instead, so no price is fabricated
    from nothing. Never fills across a session boundary -- an overnight gap
    is a real gap, not a missing tick, and forward-carrying the prior day's
    close into it would fabricate continuity that didn't exist. A `synthetic`
    column marks every filled bar, never hidden.

    This exists because pivLen/trigCooldown/reclaimWin/etc. are BAR-COUNT
    windows in the .pine file, not literal-clock-time windows -- leaving raw
    gaps in the bar sequence would silently compress those windows. See the
    B1 report's own data-limitations section: this is a documented modeling
    choice, not a claim that every bar is a real, independent trade print."""
    raw = raw.copy()
    raw["date"] = raw["t"].dt.date
    frames = []
    for date, grp in raw.groupby("date", sort=True):
        grp = grp.sort_values("t").drop_duplicates(subset="t")
        day_start = pd.Timestamp.combine(date, dt.time(9, 30)).tz_localize(ET)
        idx = pd.date_range(day_start, periods=390, freq="1min")
        grp = grp.set_index("t").reindex(idx)
        synthetic = grp["o"].isna()
        filled = grp["c"].ffill().bfill()
        for col in ("o", "h", "l", "c"):
            grp[col] = grp[col].where(~synthetic, filled)
        grp["v"] = grp["v"].fillna(0.0)
        grp["synthetic"] = synthetic
        grp.index.name = "t"
        frames.append(grp.reset_index())
    out = pd.concat(frames, ignore_index=True).sort_values("t").reset_index(drop=True)
    return out[["t", "o", "h", "l", "c", "v", "synthetic"]]


def run_replay(one_min: pd.DataFrame, config: HeffSmcConfig = HeffSmcConfig()) -> tuple:
    """One continuous HeffSmcEngine replay across every session in
    `one_min` (build_continuous_1min_series's output), chronological order,
    structure/FVG/OB/pool/HTF state carried across day boundaries (matching
    the live indicator's own forever-`var` Pine state). Returns
    (triangle_events, diagnostic_frame): triangle_events is the real
    Module-10-shaped payload for every bar where long_signal or
    short_signal fired; diagnostic_frame has one row per bar (every factor
    score, trend, HTF bias) for auditing any specific bar later."""
    htf_dir1 = compute_htf_dir_series(one_min, config.htf_tf1_minutes, config.htf_piv_len)
    htf_dir2 = compute_htf_dir_series(one_min, config.htf_tf2_minutes, config.htf_piv_len)
    dir1_series = align_htf_dir_to_1min(one_min, htf_dir1)
    dir2_series = align_htf_dir_to_1min(one_min, htf_dir2)
    dir1_by_idx = dict(enumerate(dir1_series.tolist()))
    dir2_by_idx = dict(enumerate(dir2_series.tolist()))

    def _htf_lookup(bar_index):
        return dir1_by_idx.get(bar_index, 0), dir2_by_idx.get(bar_index, 0)

    engine = HeffSmcEngine(config=config, htf_dir_lookup=_htf_lookup)

    one_min = one_min.sort_values("t").reset_index(drop=True)
    one_min = one_min.copy()
    one_min["date"] = one_min["t"].dt.date
    session_dates = sorted(one_min["date"].unique())

    prior_session_bars: Optional[pd.DataFrame] = None
    diag_rows = []
    triangle_events = []

    for date in session_dates:
        day_df = one_min[one_min["date"] == date]
        if prior_session_bars is not None and not prior_session_bars.empty:
            pdh = float(prior_session_bars["h"].max())
            pdl = float(prior_session_bars["l"].min())
        else:
            pdh = pdl = None
        engine.start_new_session(pdh, pdl)

        for row in day_df.itertuples(index=False):
            res = engine.process_bar(row.t, row.o, row.h, row.l, row.c, row.v)
            diag_rows.append(res)
            for side, sig_flag, score, factors, trig in (
                ("long", res["long_signal"], res["score_long"], res["factors_long"], res["trig_txt_long"]),
                ("short", res["short_signal"], res["score_short"], res["factors_short"], res["trig_txt_short"]),
            ):
                if sig_flag:
                    triangle_events.append({
                        "event": "CONFLUENCE", "ticker": SYMBOL, "tf": "1",
                        "side": side, "price": round(float(row.c), 4), "score": round(float(score), 4),
                        "threshold": config.min_score, "trigger": trig, "mode": "honest",
                        "factors": {k: round(float(v), 4) for k, v in factors.items()},
                        "htf_bias": res["htf_bias"], "time": row.t.strftime("%Y-%m-%d %H:%M:%S"),
                        "session": str(date), "bar_index": res["bar_index"],
                    })
        prior_session_bars = day_df

    diag_df = pd.DataFrame(diag_rows)
    return triangle_events, diag_df


def _atomic_write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".tmp{os.getpid()}")
    tmp.write_text(json.dumps(payload, indent=2, default=str))
    os.replace(tmp, path)


def summarize_events(events: list) -> dict:
    if not events:
        return {"n_total": 0}
    long_n = sum(1 for e in events if e["side"] == "long")
    short_n = sum(1 for e in events if e["side"] == "short")
    by_trigger: dict = {}
    for e in events:
        by_trigger[e["trigger"]] = by_trigger.get(e["trigger"], 0) + 1
    sessions = sorted({e["session"] for e in events})
    return {
        "n_total": len(events), "n_long": long_n, "n_short": short_n,
        "by_trigger": by_trigger, "n_sessions_with_signal": len(sessions),
        "avg_score": round(sum(e["score"] for e in events) / len(events), 3),
    }


def run_and_persist(
    config: HeffSmcConfig = HeffSmcConfig(), dates: list = None,
    raw_dir: Path = BARS_RAW_DIR, out_path: Path = TRIANGLE_EVENTS_PATH,
) -> dict:
    dates = dates if dates is not None else list_target_sessions()
    raw = load_all_bars(SYMBOL, dates, raw_dir)
    if raw.empty:
        raise RuntimeError("No QQQ 1-min bars found -- run qqq_bars_fetch.fetch_and_persist_all() first")
    continuous = build_continuous_1min_series(raw)
    n_synthetic = int(continuous["synthetic"].sum())
    events, diag_df = run_replay(continuous, config)
    summary = summarize_events(events)
    summary["n_bars_total"] = len(continuous)
    summary["n_bars_synthetic_filled"] = n_synthetic
    summary["pct_bars_synthetic_filled"] = round(n_synthetic / len(continuous), 4) if len(continuous) else None
    summary["sessions"] = dates
    payload = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "config": dataclasses_asdict(config),
        "summary": summary,
        "events": events,
    }
    _atomic_write_json(out_path, payload)
    logger.info("HEFF SMC replay: %d triangle events across %d bars (%d synthetic-filled)", len(events), len(continuous), n_synthetic)
    return payload


def dataclasses_asdict(config: HeffSmcConfig) -> dict:
    import dataclasses
    d = dataclasses.asdict(config)
    for k, v in list(d.items()):
        if isinstance(v, dt.time):
            d[k] = v.isoformat()
    return d


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    result = run_and_persist()
    print(json.dumps(result["summary"], indent=2, default=str))
