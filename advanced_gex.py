#!/usr/bin/env python3
"""advanced_gex.py — Phase 2/3 (gex_quant_engine.py) live layer: Net VEX/CHEX,
Wall Stability Score (WSS), and Cascade Breakdown Probability P(C), computed
from the SAME options-chain rows live_gex.py already fetches for the basic
GEX/flip/walls read (strike/is_call/T/iv/oi) -- no second chain fetch, no new
paid data.

HONESTY NOTE -- read before trusting these numbers for anything live:
gex_quant_engine.py's Phase 1 EOI blending and Lee-Ready/EMO trade
classification need tick-level trade/quote data. This box only pulls a
periodic (every ~30min) chain snapshot, not a trade tape, so:
  - net_vex, net_chex, wss_score/wss_flag: REAL, computed from the live chain,
    using the static +1(call)/-1(put) dealer convention -- gex_quant_engine's
    own documented no-flow default, not a hack.
  - P(C)'s F_state (order-flow unwind direction) and the ghost_wall flag: NOT
    computable without intraday V/OI-by-side data. F_state is hard-pinned to
    0.0 and ghost_wall to False here, so P(C) as shipped is gamma+term+vanna
    only -- 0.7 of its intended weight, understating true breakdown risk in a
    genuine bid-side unwind. `p_c_flow_state_tracked: false` is included in
    every row specifically so a consumer can show that caveat instead of
    treating P(C) as complete. Revisit once a tick-level ingestion pipeline
    exists (see the 2026-07-04 handoff note on this exact gap).

VIX/VXV: CBOE's free public daily-close CSVs (cdn.cboe.com), cached to disk
once per calendar day -- this is EOD data, not intraday, the same kind of
staleness this project already accepts for options OI itself (T-1 lag).
"""
import fcntl
import json
import os
from datetime import date, datetime
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import numpy as np
import requests

from gex_quant_engine import (
    CascadeBreakdownEngine,
    DynamicGammaFlipEngine,
    VexExtremeTracker,
    VolatilitySurfaceCalibrator,
    VolatilityTermStructureMonitor,
    WallStabilityEngine,
)

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
VIX_CACHE = DATA / "vix_vxv_cache.json"
IV_STATE_PATH = DATA / "iv_intraday_state.json"
VEX_HISTORY_PATH = DATA / "vex_history.json"
REAL_ADV_PATH = DATA / "real_adv.json"
STATE_LOCK_PATH = DATA / "gex_state.lock"
ET = ZoneInfo("America/New_York")

_wss_engine = WallStabilityEngine()
_cascade_engine = CascadeBreakdownEngine()


def _load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


# 2026-07-04: real 30-trading-day ADV per ticker (Alpaca daily bars, via
# compute_real_adv.py -> data/real_adv.json). Loaded once at import time --
# refresh by re-running that script periodically (e.g. weekly); this is a
# point-in-time snapshot, not auto-refreshing.
_REAL_ADV = _load_json(REAL_ADV_PATH, {})


def load_state() -> tuple:
    """(iv_state, vex_history) dicts, loaded from disk -- empty if absent/corrupt.
    Call once per live_gex.py run, thread through every compute_advanced() call,
    then save_state() once at the end."""
    return _load_json(IV_STATE_PATH, {}), _load_json(VEX_HISTORY_PATH, {})


def _atomic_write(path: Path, data: dict) -> None:
    tmp = path.with_suffix(path.suffix + f".tmp{os.getpid()}")
    tmp.write_text(json.dumps(data))
    tmp.replace(path)  # atomic rename on POSIX -- no reader ever sees a half-written file


def save_state(iv_state: dict, vex_history: dict) -> None:
    """Merges this run's updates on top of the CURRENT on-disk state (re-read here, under
    a lock, rather than blindly overwriting with whatever load_state() returned at the
    START of a run) -- necessary since 2026-07-06, when the 0DTE path (~13s, every 30min,
    own cron lock) started calling load_state()/save_state() too, alongside the ~21min
    full-universe sweep (own separate cron lock) -- the two CAN now run concurrently on
    this single-core box, and the full sweep's in-memory copy is stale for its entire
    ~21min runtime. 0DTE and the full sweep tag their state-dict keys distinctly
    ("{TICKER}-0DTE" vs bare ticker -- see live_gex.py's compute_live_0dte()), so the two
    runs never actually touch the same key; this merge just makes that non-conflict
    durable against the write itself instead of one run's stale snapshot clobbering the
    other's fresher one. flock'd + atomic (temp file + rename) so a crash or a genuine
    same-instant overlap can never corrupt the file, only very rarely cost a few
    milliseconds of wait -- the critical section here is two small JSON read/writes, not
    the whole run, so a plain blocking lock is fine (no risk of a 21min stall)."""
    DATA.mkdir(parents=True, exist_ok=True)
    with open(STATE_LOCK_PATH, "w") as lock_fp:
        fcntl.flock(lock_fp, fcntl.LOCK_EX)
        try:
            cur_iv = _load_json(IV_STATE_PATH, {})
            cur_vex = _load_json(VEX_HISTORY_PATH, {})
            cur_iv.update(iv_state)
            cur_vex.update(vex_history)
            _atomic_write(IV_STATE_PATH, cur_iv)
            _atomic_write(VEX_HISTORY_PATH, cur_vex)
        finally:
            fcntl.flock(lock_fp, fcntl.LOCK_UN)


def fetch_vix_vxv_ratio() -> Optional[float]:
    """VIX/VXV ratio from CBOE's free daily CSVs, cached once per calendar day
    (EOD data -- refetching intraday would return the same number and load
    CBOE's CDN for nothing). Returns None if CBOE is unreachable or the CSVs
    don't parse; callers treat None as "term structure unknown", not contango."""
    today = date.today().isoformat()
    cached = _load_json(VIX_CACHE, {})
    if cached.get("date") == today and cached.get("ratio") is not None:
        return cached["ratio"]
    try:
        vix_csv = requests.get(
            "https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv", timeout=15
        ).text
        vxv_csv = requests.get(
            "https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX3M_History.csv", timeout=15
        ).text
        vix_close = float(vix_csv.strip().splitlines()[-1].split(",")[-1])
        vxv_close = float(vxv_csv.strip().splitlines()[-1].split(",")[-1])
        ratio = vix_close / vxv_close if vxv_close > 0 else None
    except Exception:
        ratio = None
    if ratio is not None:
        DATA.mkdir(parents=True, exist_ok=True)
        VIX_CACHE.write_text(json.dumps({"date": today, "ratio": ratio}))
    return ratio


def _track_iv_delta(ticker: str, avg_iv: float, iv_state: dict) -> float:
    """Intraday ATM-band-average-IV change since the previous cron tick. This
    project's Phase 2 schema calls this iv_trend_15m; at this cron's actual
    ~30min cadence it's really a 30min delta -- named honestly in the return
    dict as delta_iv_30m, not mislabeled to match a schema name it doesn't
    match. Returns 0.0 (no signal) on the first read for a ticker."""
    prev = iv_state.get(ticker)
    iv_state[ticker] = {"iv": avg_iv, "ts": datetime.now(ET).isoformat()}
    if prev is None:
        return 0.0
    return avg_iv - prev["iv"]


def compute_advanced(
    ticker: str,
    rows: list,
    S: float,
    r: float,
    gamma_flip: Optional[float],
    vix_vxv_ratio: Optional[float],
    iv_state: dict,
    vex_history: dict,
) -> dict:
    """Extends one ticker's live_gex.py read with net_vex/net_chex/WSS/P(C).
    `rows` is fetch_chain()'s own output (strike/is_call/T/iv/oi) -- reused
    as-is, no second chain fetch. `iv_state`/`vex_history` are mutated in
    place (per-ticker rolling state) and persisted by the caller via
    save_state() once per run.
    """
    strikes = np.array([x["strike"] for x in rows], dtype=np.float64)
    is_call = np.array([x["is_call"] for x in rows], dtype=bool)
    iv_raw = np.array([x["iv"] for x in rows], dtype=np.float64)
    oi = np.array([x["oi"] for x in rows], dtype=np.float64)
    T = np.array([x["T"] for x in rows], dtype=np.float64)
    w = np.where(is_call, 1.0, -1.0)  # static dealer convention -- see module docstring

    # 2026-07-04: arbitrage-free surface calibration (Phase 2's
    # VolatilitySurfaceCalibrator, self-tested since 2026-07-03 but never
    # previously fed live chain data -- net_vex/net_chex/WSS ran on raw,
    # un-smoothed per-contract IVs until now). Falls back to the raw IVs on
    # any failure (thin/degenerate chain, no solvable quotes, etc.) -- fail
    # open, same discipline as every other gate/tag in this repo. Benchmarked
    # 2026-07-04: ~30ms/ticker on a real 30-contract AAPL chain, negligible
    # against the sweep's per-ticker network I/O.
    iv_surface_calibrated = False
    iv = iv_raw
    try:
        mid_price = np.array([x["mid_price"] for x in rows], dtype=np.float64)
        iv_smooth = VolatilitySurfaceCalibrator(r=r, q=0.0).calibrate(
            strikes, mid_price, float(T[0]), S, is_call
        )
        if np.all(np.isfinite(iv_smooth)) and np.all(iv_smooth > 0):
            iv = iv_smooth
            iv_surface_calibrated = True
    except Exception:
        pass

    engine = DynamicGammaFlipEngine(strikes, eoi=oi, iv=iv, r=r, q=0.0, w=w, is_call=is_call)
    net_vex = engine.net_vex(S, T)
    net_chex = engine.net_chex(S, T)

    # 2026-07-06 (heff's ask): prefer the Newton-Raphson/brentq flip solver
    # (gex_quant_engine.DynamicGammaFlipEngine.calculate_gamma_flip) over
    # gex.py's grid-search find_flip() for precision -- same engine instance
    # already built above for net_vex/net_chex, so this costs nothing extra
    # (no second chain build). Only attempted when gex.py's grid search
    # already found a flip (gamma_flip is not None): this preserves gex.py's
    # MIN_COVERAGE/no-crossing gates exactly as-is (dashboard_snapshot.py's
    # _flip_reason() logic depends on flip being None for those two cases) --
    # calculus refines a flip already known to exist, it doesn't go looking
    # for one gex.py gave up on. Falls back to the grid-search value if
    # Newton+brentq both fail to converge (fail-open, same discipline as
    # iv_surface_calibrated/p_c_flow_state_tracked below).
    flip_source = None
    if gamma_flip is not None:
        flip_calculus = engine.calculate_gamma_flip(current_spot=S, T=T)
        if flip_calculus is not None:
            gamma_flip = flip_calculus
            flip_source = "calculus"
        else:
            flip_source = "grid_fallback"

    avg_iv = float(np.mean(iv))
    delta_iv = _track_iv_delta(ticker, avg_iv, iv_state)
    # 2026-07-04: real 30d ADV when we have it for this ticker; falls back to
    # the old options-notional proxy otherwise (new listing, gap in the
    # real_adv.json snapshot, etc.) -- fail open, same discipline as every
    # other gate/tag in this repo.
    real_adv = _REAL_ADV.get(ticker)
    if real_adv and real_adv > 0:
        scalar = float(real_adv)
    else:
        scalar = float(S * np.sum(oi) * 100.0)
        scalar = scalar if scalar > 0 else 1.0

    tracker = VexExtremeTracker()
    tracker._history = list(vex_history.get(ticker, []))[-tracker.window:]

    now_et = datetime.now(ET)
    close_et = now_et.replace(hour=16, minute=0, second=0, microsecond=0)
    wss_score, wss_flag = _wss_engine.score(
        spot=S, gamma_flip=gamma_flip, net_vex=net_vex, delta_iv=delta_iv,
        net_chex_at_wall=net_chex, delta_t_days=1.0, standardizing_scalar=scalar,
        now=now_et, session_close=close_et,
    )
    # wall_confidence (2026-07-06, heff's ask; REVISED 2026-07-07 after a 2nd trading day
    # of data landed): informational-only tag, NOT wired into wall_proximity_alert.py's
    # "holding"/"cracking" verdict text itself, which stays exactly as it was.
    #
    # RETIRED finding (2026-07-06, one session only): wss_score>=9 looked like a
    # "high-confidence" sub-zone within "holding" calls (58.3% vs 40.4% at raw wss_score>=0,
    # n=12 vs 52). A 2nd day of real data DISPROVED this outright -- the wss_score>=9
    # bucket fell to 39.1% accuracy (n=23 that day), below a coin flip, and >=9 can only
    # ever occur on the "holding" side to begin with (non-negative scores), so it was
    # really just scoring a subset of the losing side and got lucky on day 1 (n=17 total).
    #
    # CURRENT finding (2 days, 236 events, both days moving the same direction): it's the
    # VERDICT SIGN itself that's predictive, not any within-side magnitude threshold.
    # "cracking" verdicts (wss_score<0): 63.5% accurate overall (n=104), 53.1%->68.1%
    # day1->day2 (strengthening). "holding" verdicts (wss_score>=0): only 40.2% accurate
    # (n=132), i.e. wrong more often than right, and getting WORSE (44.8%->36.5%
    # day1->day2) -- betting the opposite of a "holding" call would itself have scored
    # 59.8% these 2 days. Still only 2 days/236 events (same-ish regime both days) --
    # re-check after day 3-4 via wall_alert_scoring.py + data/wall_alert_accuracy_summary.json
    # before treating this as settled, same caution that applied to the retired finding above.
    #
    # "high" = alert's own verdict has run ~63.5% accurate so far, trust as shown.
    # "low" = alert's own verdict has run ~40.2% accurate so far -- historically wrong more
    # than right; the OPPOSITE of what's printed has been the better read on this data.
    #
    # Also see wall_proximity_alert.py's 2026-07-07 fix: wss_score used to log at 0 decimal
    # places, so a real score like -0.4 printed/parsed as a sign-losing "0" -- corrupted
    # ~40 of 236 historical ledger rows' stored score (not their verdict text, which was
    # always correct) before that fix. Doesn't change the verdict-based split above, which
    # never depended on the corrupted field.
    wall_confidence = "high" if wss_score < 0 else "low"

    g = _cascade_engine.g_state(S, gamma_flip)
    t = 0.0 if vix_vxv_ratio is None else VolatilityTermStructureMonitor.t_state_from_ratio(vix_vxv_ratio)
    f = 0.0  # NOT TRACKED -- see module docstring
    v = tracker.v_state(net_vex)
    # AUDIT FIX (2026-07-04): f_state_tracked=False tells probability() to
    # exclude F_state and redistribute w_gamma/w_term/w_vanna to sum to 1.0,
    # instead of silently treating f=0.0 above as a measured "no unwind"
    # reading and permanently capping P(C) at 0.7 (see
    # CascadeBreakdownEngine.probability()'s docstring). p_c_flow_state_tracked
    # below stays False either way -- this fixes P(C)'s ceiling, not the
    # existing partial-data caveat, which still applies and still needs
    # showing to anyone consuming this number.
    p_c = _cascade_engine.probability(g, t, f, v, f_state_tracked=False)
    vex_history[ticker] = tracker._history

    return {
        "flip": round(gamma_flip, 2) if gamma_flip is not None else None,
        "flip_source": flip_source,  # "calculus" / "grid_fallback" / None (grid search itself found nothing)
        "net_vex": round(net_vex, 2),
        "net_chex": round(net_chex, 2),
        "wss_score": round(wss_score, 2),
        "wss_flag": wss_flag,
        "wall_confidence": wall_confidence,  # "high"/"standard" -- see comment above _wss_engine.score() call
        "p_c": round(p_c, 4),
        "p_c_flow_state_tracked": False,  # honesty flag -- see module docstring
        "ghost_wall": False,  # not computable without intraday V/OI-by-side data
        "iv_surface_calibrated": iv_surface_calibrated,  # False -> net_vex/net_chex/WSS used raw per-contract IV this tick
    }
