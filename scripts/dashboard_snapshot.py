#!/usr/bin/env python3
"""dashboard_snapshot.py — builds snapshot.json for the BOT_NEXUS trading dashboard
(mean-reversion + ORB paper book only) and pushes it to the private
trading-dashboard-snapshot GitHub repo.

Run: ./venv/bin/python dashboard_snapshot.py
"""
import csv
import json
import math
import re
import subprocess
import sys
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
LOGS = ROOT / "logs"
ET = ZoneInfo("America/New_York")

# scripts/ is one level below the workspace root -- root-level modules (sector_rotation,
# options_eval) aren't importable without this, since only the script's own directory is
# on sys.path by default.
sys.path.insert(0, str(ROOT))

SNAPSHOT_REPO = Path.home() / "trading-dashboard-snapshot"
GIT_NAME = "dashboard-bot"
GIT_EMAIL = "dashboard-bot@heff.local"

LOG_TAIL_LINES = 50
LOG_SCAN_LINES = 2000  # window to search backward for the latest book-state line
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
BOOK_RE = re.compile(
    r"=== alpaca_executor \S+ \[(?P<mode>[^/]+)/\s*(?P<state>[^\]]+)\] === "
    r"book=\$(?P<capital>[\d.]+) alpaca_equity=\$(?P<equity>[\d.]+)"
)


def _load_json_retry(path, default, retries=2, delay=0.5):
    if not path.exists():
        return default
    for attempt in range(retries):
        try:
            with path.open() as f:
                return json.load(f)
        except json.JSONDecodeError:
            if attempt < retries - 1:
                time.sleep(delay)
                continue
            print(f"  ! failed to parse {path}, skipping this cycle")
            return default
    return default


def _load_csv_rows_retry(path, retries=2, delay=0.5):
    if not path.exists():
        return []
    for attempt in range(retries):
        try:
            with path.open() as f:
                return list(csv.DictReader(f))
        except Exception:
            if attempt < retries - 1:
                time.sleep(delay)
                continue
            print(f"  ! failed to read {path}, skipping this cycle")
            return []
    return []


def _load_jsonl(path):
    if not path.exists():
        return []
    out = []
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


POSITION_FIELDS = ("trade_id", "strategy", "ticker", "side", "entry", "stop", "target",
                   "planned_rr", "entry_time", "shares", "notional", "risk_dollars",
                   "z", "rsi", "vwap_dev", "relvol", "rng", "vwap",
                   "sector_etf", "sector_quadrant", "sector_hot")
SIGNAL_FIELDS = ("trade_id", "ticker", "side", "entry", "stop", "t1", "strategy",
                 "shares", "risk_dollars", "z", "rsi", "vwap_dev", "relvol", "rng", "vwap",
                 "entry_time", "detected_at", "sector_etf", "sector_quadrant", "sector_hot")
PREMARKET_FIELDS = ("ticker", "bias", "gap_pct", "pm_rvol", "atr14", "gap_ratio", "vwap",
                    "sd_from_vwap", "equity_pm_drift_pct", "spy_pm_drift_pct", "rel_strength",
                    "lid_ok", "pm_last", "pm_high", "pm_low", "prev_close", "universe",
                    "sector_etf", "sector_quadrant", "sector_hot")
CONTINUATION_FIELDS = ("ticker", "score", "er", "r_max", "s_min", "slope_norm", "vol_z_mean",
                       "residual_alpha", "has_spring", "spring_time", "phase_d_fired",
                       "phase_d_time", "phase_d_price", "last_price", "universe",
                       "sector_etf", "sector_quadrant", "sector_hot")


def open_positions():
    positions = []
    for name in ("paper_open.json", "orb_paper_open.json"):
        data = _load_json_retry(DATA / name, {})
        positions.extend(data.values())
    out = []
    for p in positions:
        row = {k: p.get(k) for k in POSITION_FIELDS}
        if row.get("strategy") is None:
            row["strategy"] = "ORB" if "ORB" in str(p.get("trade_id", "")) else "MR"
        out.append(row)
    return out


def active_signals(today):
    signals = []
    for name in (f"mr_triggers_{today}.jsonl", f"orb_triggers_{today}.jsonl"):
        signals.extend(_load_jsonl(DATA / name))
    rows = [{k: s.get(k) for k in SIGNAL_FIELDS} for s in signals]
    rows.sort(key=lambda r: r.get("entry_time") or "", reverse=True)  # freshest first
    return rows


def premarket_candidates(today):
    path = DATA / f"orb_premarket_{today}.json"
    data = _load_json_retry(path, [])
    scanned_at = None
    if path.exists():
        scanned_at = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat()
    rows = []
    for c in data:
        row = {k: c.get(k) for k in PREMARKET_FIELDS}
        row["trade_id"] = f"PM:{row['ticker']}:{today}"
        row["side"] = row.pop("bias")
        row["strategy"] = "Premarket"
        rows.append(row)
    return rows, scanned_at


def continuation_candidates(today):
    path = DATA / f"continuation_{today}.json"
    data = _load_json_retry(path, {"watching": [], "fired": []})
    scanned_at = None
    if path.exists():
        scanned_at = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat()
    rows = []
    for status, items in (("watching", data.get("watching", [])), ("fired", data.get("fired", []))):
        for c in items:
            row = {k: c.get(k) for k in CONTINUATION_FIELDS}
            row["trade_id"] = f"CONT:{row['ticker']}:{today}"
            row["side"] = "LONG"  # bullish-only screener (see continuation_scanner.py)
            row["strategy"] = "Continuation"
            row["status"] = status
            rows.append(row)
    return rows, scanned_at


def sector_heatmap():
    try:
        import sector_rotation as secrot
    except Exception:
        return {"as_of": None, "sectors": [], "tickers": []}
    sector_map = secrot.load_sector_map()
    quadrants = secrot.load_latest_quadrants()
    as_of = next((q["date"] for q in quadrants.values()), None)
    sectors = []
    for etf in secrot.SECTOR_ETFS:
        q = quadrants.get(etf)
        sectors.append({
            "etf": etf, "name": secrot.ETF_SECTOR_NAME.get(etf, etf),
            "quadrant": q["quadrant"] if q else None,
            "rs_zscore": round(q["rs_zscore"], 3) if q else None,
            "rs_mom": round(q["rs_mom"], 3) if q else None,
            "ticker_count": sum(1 for v in sector_map.values() if v == etf),
        })
    tickers = [
        {"ticker": t, "sector_etf": etf, "sector_name": secrot.ETF_SECTOR_NAME.get(etf, etf),
         "quadrant": (quadrants.get(etf) or {}).get("quadrant")}
        for t, etf in sector_map.items() if etf
    ]
    tickers.sort(key=lambda r: (r["sector_etf"] or "", r["ticker"]))
    return {"as_of": as_of, "sectors": sectors, "tickers": tickers}


VIX_REGIME_BANDS = ((15, "calm"), (20, "normal"), (30, "elevated"), (float("inf"), "stressed"))


def vix_regime():
    """VIX level (already in market_pulse) + VVIX + a plain-English regime label. Same
    yfinance source options_orchestrator.py's fetch_vix_vvix already relies on for live
    regime tagging -- this is a read-only display duplicate, not a new dependency."""
    try:
        import yfinance as yf
    except Exception:
        return {"vix": None, "vvix": None, "label": None}
    try:
        vix_h = yf.Ticker("^VIX").history(period="5d")["Close"].dropna()
        vvix_h = yf.Ticker("^VVIX").history(period="5d")["Close"].dropna()
        vix = float(vix_h.iloc[-1]) if len(vix_h) else None
        vvix = float(vvix_h.iloc[-1]) if len(vvix_h) else None
    except Exception:
        vix = vvix = None
    label = None
    if vix is not None:
        for ceiling, name in VIX_REGIME_BANDS:
            if vix < ceiling:
                label = name
                break
    return {"vix": round(vix, 2) if vix is not None else None,
            "vvix": round(vvix, 2) if vvix is not None else None, "label": label}


def scanner_accuracy():
    """Aggregate hit-rate stats per scanner from data/scanner_outcomes.jsonl (built by
    scanner_outcome_tracker.py). Same aggregation as that script's --summarize, kept as a
    small standalone copy here rather than importing it (no other side effects to inherit)."""
    rows = _load_jsonl(DATA / "scanner_outcomes.jsonl")
    by_scanner = {}
    for r in rows:
        by_scanner.setdefault(r.get("scanner", "unknown"), []).append(r)
    out = []
    for scanner, rs in by_scanner.items():
        n = len(rs)
        correct = sum(1 for r in rs if r.get("direction_correct"))
        moves = [r["move_pct"] for r in rs if r.get("move_pct") is not None]
        entry = {"scanner": scanner, "n": n, "correct": correct,
                 "hit_rate": round(correct / n, 4) if n else None,
                 "avg_move_pct": round(sum(moves) / len(moves), 2) if moves else None}
        if scanner == "continuation":
            fired = sum(1 for r in rs if r.get("breakout_confirmed"))
            entry["breakout_confirmed_rate"] = round(fired / n, 4) if n else None
        out.append(entry)
    return out


def options_leaderboard():
    """Per-strategy (S1-S10) tournament standing from options_eval.db: win-rate posterior,
    DSR/PSR, trade count, plus realized $ P&L added on top (leaderboard() itself doesn't
    include dollars -- summed separately from the same ledger table it reads)."""
    db_path = DATA / "options_eval.db"
    if not db_path.exists():
        return []
    try:
        import sqlite3
        from options_eval import connect as eval_connect, leaderboard as eval_leaderboard
        conn = eval_connect(db_path)
        board = eval_leaderboard(conn)
        pnl_rows = conn.execute(
            "SELECT strategy_id, COALESCE(SUM(realized_pnl), 0) AS pnl "
            "FROM trades_ledger WHERE realized_pnl IS NOT NULL GROUP BY strategy_id").fetchall()
        pnl_by_strategy = {r["strategy_id"]: round(r["pnl"], 2) for r in pnl_rows}
        conn.close()
        for entry in board:
            entry["realized_pnl"] = pnl_by_strategy.get(entry["strategy_id"], 0.0)
        return board
    except Exception as e:
        print(f"  ! options_leaderboard failed: {e}")
        return []


RECON_HISTORY_LIMIT = 60


def slippage_recon():
    """Real-fill vs idealized-sim slippage (the real-money go-live gate) -- written by
    alpaca_recon.py, which currently only runs once/day (folded into night_report.py at the
    close), so `history` is naturally a daily trend series, not an intraday one."""
    latest = _load_json_retry(DATA / "alpaca_recon_snapshot.json", None)
    history = _load_jsonl(DATA / "alpaca_recon_history.jsonl")[-RECON_HISTORY_LIMIT:]
    return {"latest": latest, "history": history}


TOURNAMENT_DAILY_LOSS_LIMIT = 500.0  # mirrors options_orchestrator.py's own constant -- kept as
                                     # a read-only display copy, not imported, same "minimal
                                     # dependency surface" call guardrails.py/portfolio_gate.py
                                     # already made for their own local live_params loaders.
TOURNAMENT_MAX_DRAWDOWN_LIMIT = 2500.0  # mirrors options_orchestrator.py's MAX_DRAWDOWN_LIMIT
                                        # (2026-07-05), same read-only-copy convention as above.


def _tournament_peak_drawdown():
    """Read-only mirror of options_orchestrator.py's tournament_peak_drawdown() -- same
    all-time rolling-peak logic over trades_ledger's CLOSED rows, kept as a display copy
    rather than importing that heavier live-trading module (same convention as
    TOURNAMENT_DAILY_LOSS_LIMIT above). Returns None on any error so the panel can show
    "unavailable" honestly instead of a stale/fabricated number."""
    db_path = DATA / "options_eval.db"
    if not db_path.exists():
        return None
    try:
        import sqlite3
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT exit_time, realized_pnl FROM trades_ledger "
            "WHERE status = 'CLOSED' AND realized_pnl IS NOT NULL "
            "ORDER BY exit_time ASC").fetchall()
        conn.close()
        cum = peak = 0.0
        for row in rows:
            cum += row["realized_pnl"]
            peak = max(peak, cum)
        drawdown = max(0.0, peak - cum)
        return {"peak": round(peak, 2), "current": round(cum, 2), "drawdown": round(drawdown, 2)}
    except Exception as e:
        print(f"  ! _tournament_peak_drawdown failed: {e}")
        return None


def _tournament_realized_today():
    db_path = DATA / "options_eval.db"
    if not db_path.exists():
        return None
    try:
        import sqlite3
        midnight_et = datetime.now(ET).replace(hour=0, minute=0, second=0, microsecond=0)
        conn = sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT COALESCE(SUM(realized_pnl), 0) FROM trades_ledger "
            "WHERE realized_pnl IS NOT NULL AND exit_time >= ?",
            (int(midnight_et.timestamp()),)).fetchone()
        conn.close()
        return float(row[0])
    except Exception as e:
        print(f"  ! _tournament_realized_today failed: {e}")
        return None


def _real_committed_equity_positions():
    """Real committed equity slots (open positions + resting entry orders), pulled straight
    from Alpaca -- the EXACT computation alpaca_executor.py's own gate uses before arming a
    trade (see its "committed slots (positions+resting orders)" log line). NOT the same thing
    as data/paper_open.json / orb_paper_open.json: those are the idealized evaluators, which
    deliberately track EVERY signal ungated (2026-06-23 decision, to measure raw edge) -- the
    portfolio gate governs only the executable book. Using the paper-open files as an "open
    position count" proxy here was wrong and made the dashboard show 11/4 (raw signal count)
    while the real live book was correctly gated at 4/4 -- fixed 2026-07-02. Returns None (not
    a stale/wrong number) if the Alpaca call fails, so callers can show "unavailable" honestly."""
    try:
        import alpaca_executor as ax
        api = ax.Alpaca()
        positions = api.positions()
        equity_positions = [p for p in positions if len(p.get("symbol", "")) <= 6]  # excl. OCC option legs (>6 chars)
        try:
            open_orders = api._get("/v2/orders?status=open&nested=true&limit=200")
        except Exception:
            open_orders = []
        committed = {}
        for p in equity_positions:
            committed[p["symbol"]] = {"ticker": p["symbol"],
                                      "side": "LONG" if float(p["qty"]) > 0 else "SHORT",
                                      "notional": abs(float(p["market_value"]))}
        for o in open_orders:
            sym = o.get("symbol")
            if not sym or sym in committed or not o.get("side"):
                continue
            committed[sym] = {"ticker": sym, "side": "LONG" if o["side"] == "buy" else "SHORT",
                              "notional": float(o.get("limit_price") or 0) * float(o.get("qty") or 0)}
        return list(committed.values())
    except Exception as e:
        print(f"  ! _real_committed_equity_positions failed: {e}")
        return None


def guardrail_status(real_committed):
    """Equity book's live halt state (guardrails.check_guardrails(), same function the staging
    path gates on) + the options tournament's own separate daily-loss halt (added 2026-07-01
    after the equity-halt fix de-scoped it, see options-tournament memory) + the most recent
    blocks logged to data/guardrail_blocks.jsonl. Closes the blind spot that let two whole-
    account-contamination bugs go undetected until someone manually dug through logs.
    `real_committed` (from _real_committed_equity_positions) overrides the position-cap check's
    default proxy -- daily_realized_pnl is left on its default (today_realized_pnl() from
    paper_trades.csv), which IS the right source for this book's own P&L, unlike position count."""
    try:
        import guardrails as gr
        equity = (gr.check_guardrails(open_position_count=len(real_committed))
                  if real_committed is not None else None)
    except Exception as e:
        print(f"  ! guardrail_status equity check failed: {e}")
        equity = None
    recent_blocks = _load_jsonl(DATA / "guardrail_blocks.jsonl")[-10:]
    tourn_pnl = _tournament_realized_today()
    tourn_dd = _tournament_peak_drawdown()
    tournament = None
    if tourn_pnl is not None:
        daily_halt = tourn_pnl <= -TOURNAMENT_DAILY_LOSS_LIMIT
        drawdown_halt = tourn_dd is not None and tourn_dd["drawdown"] > TOURNAMENT_MAX_DRAWDOWN_LIMIT
        tournament = {
            "realized_pnl": round(tourn_pnl, 2),
            "limit": -TOURNAMENT_DAILY_LOSS_LIMIT,
            # ORs the daily-loss AND max-drawdown halts together, same as guardrails.py's own
            # halted_for_day for the equity book -- either one alone should trip the banner.
            "halted": daily_halt or drawdown_halt,
        }
        if tourn_dd is not None:
            tournament["max_drawdown"] = {
                "peak": tourn_dd["peak"], "current": tourn_dd["current"],
                "drawdown": tourn_dd["drawdown"], "limit": TOURNAMENT_MAX_DRAWDOWN_LIMIT,
                "halt": drawdown_halt,
            }
    return {"equity": equity, "recent_blocks": recent_blocks, "tournament": tournament}


def _flip_reason(flip, n_oi):
    """WHY a row's flip is null, so the frontend can stop showing an identical "--" for two
    very different situations. gex.py's find_flip() returns NaN (-> None here) for two
    distinct reasons: (1) MIN_COVERAGE=6 -- too few OI-bearing strikes for a trusted read at
    all (a real data gap), or (2) net $GEX never changes sign within the +/-15% FLIP_BAND --
    sufficient coverage, but this name's options positioning is one-sided enough that there's
    no nearby flip today (a stable regime, not a gap). "6" duplicates gex.py's MIN_COVERAGE;
    this script only reads gex.py's pre-computed output and never imports gex.py directly
    (same minimal-dependency-surface call as live_gex.py's own module docstring). Returns
    None when flip IS present (nothing to explain)."""
    if flip is not None:
        return None
    return "thin_chain" if (n_oi or 0) < 6 else "no_crossing"


def _gex_view_live():
    """data/live_gex_snapshot.json (live_gex.py, added 2026-07-02): Alpaca's FREE indicative
    chain + free OI, refreshed every ~5min RTH -- a genuinely current read, not a frozen
    research pull. Returns [] (not an exception) if the file is missing/empty so the caller
    falls back cleanly."""
    snap = _load_json_retry(DATA / "live_gex_snapshot.json", None)
    if not snap or not snap.get("results"):
        return []
    out = []
    for r in snap["results"]:
        if r.get("error"):
            continue
        out.append({
            "ticker": r.get("ticker"), "as_of": r.get("as_of"), "regime": r.get("regime"),
            "net_gex": r.get("net_gex"), "flip": r.get("flip"),
            "flip_reason": _flip_reason(r.get("flip"), r.get("n_oi")),
            # "calculus" (Newton-Raphson/brentq, gex_quant_engine.py) / "grid_fallback"
            # (gex.py's grid search, used when calculus didn't converge) / None (grid
            # search itself found no flip -- see flip_reason instead). Added 2026-07-06.
            "flip_source": r.get("flip_source"),
            # CVD reversal-watch (2026-07-05, heff's ask): only ever set for tickers
            # wall_proximity_alert.py's own 5-min cycle found near a wall AND where the
            # free bar-based CVD proxy (momentum_breakdown.py's CVD_Engine) confirmed the
            # "reversal" direction (net buying building near a put wall, or net selling
            # building near a call wall). None on every other row -- absence isn't a gap,
            # it means that ticker isn't currently near a wall with a confirming read.
            "cvd_reversal_note": r.get("cvd_reversal_note"),
            "call_wall": r.get("call_wall"), "put_wall": r.get("put_wall"),
            "spot": r.get("spot"), "coverage": r.get("coverage"),
            # Phase 2/3 second-order-Greek layer (gex_quant_engine.py via
            # advanced_gex.py), added 2026-07-04 -- pass-through only, no new
            # computation here. p_c_flow_state_tracked=False on every row means
            # P(C) is gamma+term+vanna only (order-flow state isn't tracked
            # yet); see advanced_gex.py's module docstring. Present in the data
            # layer now; frontend rendering is staged separately, not deployed
            # (Netlify deploy freeze until 2026-07-08).
            "net_vex": r.get("net_vex"), "net_chex": r.get("net_chex"),
            "wss_score": r.get("wss_score"), "wss_flag": r.get("wss_flag"),
            # "high"/"standard" -- 2026-07-06 day-1 cross-check found "holding" (wss_score>=0)
            # calls got meaningfully more reliable above wss_score>=9 (58% vs 40% at raw >=0);
            # informational only, does NOT change wall_proximity_alert.py's own verdict logic.
            # See advanced_gex.py's compute_advanced() and data/wall_alert_accuracy_summary.json.
            "wall_confidence": r.get("wall_confidence"),
            "p_c": r.get("p_c"), "p_c_flow_state_tracked": r.get("p_c_flow_state_tracked"),
            "ghost_wall": r.get("ghost_wall"),
            # 2026-07-04: whether net_vex/net_chex/WSS above used the arb-free
            # calibrated IV surface (VolatilitySurfaceCalibrator) or fell back
            # to raw per-contract IV this tick -- same pass-through-only
            # pattern as p_c_flow_state_tracked, frontend rendering optional.
            "iv_surface_calibrated": r.get("iv_surface_calibrated"),
        })
    return out


def _gex_view_0dte():
    """data/live_gex_0dte_snapshot.json (live_gex.py --0dte, added 2026-07-03): same-day
    SPY GEX, refreshed every ~2min RTH via its own independent cron/lock -- separate from the
    ~30 DTE monthly read in _gex_view_live() since 0DTE dealer positioning isn't the same thing
    as the monthly-cycle number. Same output field shape as the other two GEX views (ticker
    already tagged "SPY-0DTE" by live_gex.py itself) so it just shows up as one more row on the
    existing panel -- no frontend changes. Returns [] if the file is missing/empty/errored
    (e.g. a holiday with no same-day expiration) so the caller falls back cleanly.

    Field set was originally a narrow subset (just flip/walls/net_gex) written before the
    Phase 2/3 layer existed. Widened 2026-07-06 to match _gex_view_live()'s full field list
    now that live_gex.py's --0dte mode also runs advanced_gex.compute_advanced() (calculus
    flip solver + net_vex/net_chex/WSS/P(C)) -- otherwise this function would have kept
    silently dropping all of that on the floor for every 0DTE row. No cvd_reversal_note here:
    wall_proximity_alert.py's CVD reversal-watch only patches the ~30-DTE snapshot, a
    separate, not-yet-extended feature -- not an oversight in this pass."""
    snap = _load_json_retry(DATA / "live_gex_0dte_snapshot.json", None)
    if not snap or not snap.get("results"):
        return []
    out = []
    for r in snap["results"]:
        if r.get("error"):
            continue
        out.append({
            "ticker": r.get("ticker"), "as_of": r.get("as_of"), "regime": r.get("regime"),
            "net_gex": r.get("net_gex"), "flip": r.get("flip"),
            "flip_reason": _flip_reason(r.get("flip"), r.get("n_oi")),
            "flip_source": r.get("flip_source"),
            "call_wall": r.get("call_wall"), "put_wall": r.get("put_wall"),
            "spot": r.get("spot"), "coverage": r.get("coverage"),
            "net_vex": r.get("net_vex"), "net_chex": r.get("net_chex"),
            "wss_score": r.get("wss_score"), "wss_flag": r.get("wss_flag"),
            # "high"/"standard" -- 2026-07-06 day-1 cross-check found "holding" (wss_score>=0)
            # calls got meaningfully more reliable above wss_score>=9 (58% vs 40% at raw >=0);
            # informational only, does NOT change wall_proximity_alert.py's own verdict logic.
            # See advanced_gex.py's compute_advanced() and data/wall_alert_accuracy_summary.json.
            "wall_confidence": r.get("wall_confidence"),
            "p_c": r.get("p_c"), "p_c_flow_state_tracked": r.get("p_c_flow_state_tracked"),
            "ghost_wall": r.get("ghost_wall"),
            "iv_surface_calibrated": r.get("iv_surface_calibrated"),
        })
    return out


def _gex_view_historical():
    """Net GEX/flip/walls per name from data/options/{SYM}_gex.parquet (gex.py) -- the
    post-close Databento research pull, frozen at whatever date that one-time pull finished.
    Fallback only, used until live_gex.py's first successful run (or if it ever goes stale/
    fails); each row's own `as_of` date is surfaced either way so it always reads honestly."""
    try:
        import pandas as pd
    except Exception:
        return []
    out = []
    options_dir = DATA / "options"
    if not options_dir.exists():
        return out
    for path in sorted(options_dir.glob("*_gex.parquet")):
        sym = path.stem[:-len("_gex")]
        try:
            df = pd.read_parquet(path)
        except Exception:
            continue
        if df.empty:
            continue
        last = df.iloc[-1]

        def _num(v):
            try:
                return round(float(v), 2) if pd.notna(v) else None
            except Exception:
                return None
        regime = last.get("regime")
        out.append({
            "ticker": sym,
            "as_of": str(last["date"])[:10],
            "regime": regime if regime and regime != "none" else None,
            "net_gex": _num(last.get("net_gex")),
            "flip": _num(last.get("flip")),
            "flip_reason": _flip_reason(_num(last.get("flip")), last.get("n_oi")),
            "call_wall": _num(last.get("call_wall")),
            "put_wall": _num(last.get("put_wall")),
            "spot": _num(last.get("spot")),
            "coverage": _num(last.get("coverage")),
        })
    return out


CONFLUENCE_STALE_HOURS = 30  # underlying UW pulls are daily/post-close, not intraday --
# matches catalyst_alert.py's own staleness threshold, same reasoning.


def confluence_view():
    """data/confluence_score.json (confluence_score.py, added 2026-07-04):
    cross-signal confluence read (Ghost Wall, dark-pool lean, put/call
    ratio, sweep alerts, sector RRG) across the UW-tracked universe,
    refreshed daily post-close via uw_confluence_refresh_wrapper.sh. This is
    diagnostic, UW-dependent data with a short expected lifespan (the UW
    trial ends ~2026-07-1[1-7], see [[unusual-whales-api]]) -- returns an
    honest stale/unavailable marker rather than silently showing an old
    read once the underlying pulls stop, matching p_c_flow_state_tracked's
    honesty pattern elsewhere in this file."""
    payload = _load_json_retry(DATA / "confluence_score.json", None)
    if not payload:
        return {"available": False, "rows": [], "generated_at": None}
    try:
        generated = datetime.fromisoformat(payload["generated_at"])
        age_h = (datetime.now(timezone.utc) - generated).total_seconds() / 3600.0
    except Exception:
        age_h = None
    stale = age_h is None or age_h > CONFLUENCE_STALE_HOURS
    return {
        "available": not stale,
        "generated_at": payload.get("generated_at"),
        "rows": [] if stale else payload.get("rows", []),
    }


HOT_SETUP_CHAINS_STALE_MINUTES = 20  # wall_proximity_alert.py rewrites this file every
# ~5min cycle it finds an active hot setup -- a much faster cadence than confluence_view's
# 30-HOUR daily-pull staleness window above, so this needs its own much tighter threshold.
# Past this age the cron isn't currently finding a live 🎯 hit (or isn't running RTH) --
# an old setup isn't still "current", same honest-staleness discipline as confluence_view.


def hot_setup_chains():
    """data/hot_setup_chains.json (wall_proximity_alert.py, added 2026-07-10): near-wall
    strikes/IV/bid-ask for whichever ticker(s) currently have an active 🎯 high-conviction
    setup (negative gamma + 'holding' verdict, is_ultra_tight flagging the <=0.05%-distance
    sub-tier) -- the exact same live chain pull (live_gex.fetch_chain, Alpaca-backed) that
    already feeds the standalone Telegram alert, surfaced here so the dashboard can show a
    chain quick-view panel without a second data source or Netlify-side broker credentials.
    Returns an honest stale/unavailable marker (not an old read) once the file ages past
    HOT_SETUP_CHAINS_STALE_MINUTES."""
    payload = _load_json_retry(DATA / "hot_setup_chains.json", None)
    if not payload:
        return {"available": False, "setups": [], "generated_at": None}
    try:
        generated = datetime.fromisoformat(payload["generated_at"])
        age_min = (datetime.now(timezone.utc) - generated).total_seconds() / 60.0
    except Exception:
        age_min = None
    stale = age_min is None or age_min > HOT_SETUP_CHAINS_STALE_MINUTES
    return {
        "available": not stale,
        "generated_at": payload.get("generated_at"),
        "setups": [] if stale else payload.get("setups", []),
    }


def _etf_tickers():
    """SPY + the 11 SPDR sector ETFs (same list live_gex.py itself pulls from, which in turn
    reuses sector_rotation.SECTOR_ETFS -- one source of truth, not a second hardcoded copy)."""
    import sector_rotation as secrot
    return {"SPY"} | set(secrot.SECTOR_ETFS)


_MAG7 = {"AAPL", "MSFT", "GOOGL", "GOOG", "AMZN", "NVDA", "META", "TSLA"}  # 2026-07-03,
# heff's direction: Mag 7 sorted as their own tier, right below the ETFs. Same 8 tickers
# live_gex.py's --0dte mode pulls by default (GOOG included alongside GOOGL for completeness,
# even though it only lists Friday expirations vs GOOGL's Mon/Wed/Fri -- see live_gex.py).


def _gex_sort_tier(ticker, etfs):
    base = ticker.split("-")[0]  # e.g. "SPY-0DTE" -> "SPY", "AAPL-0DTE" -> "AAPL"
    if base in etfs:
        return 0
    if base in _MAG7:
        return 1
    return 2


ALERT_HISTORY_LIMIT_PER_TICKER = 50  # 486 total ledger rows across ~171 tickers today
# (2026-07-10) -- nowhere near this in practice, just a defensive cap so a name that
# accumulates a long history over months doesn't balloon the snapshot payload.


def _alert_history_by_ticker():
    """data/wall_alert_ledger.jsonl (wall_alert_scoring.py's ledger, growing daily via the
    5:10pm ET scoring cron) -- every scored wall-proximity alert ever, replayed onto the
    dashboard's charts as setMarkers() points (2026-07-10, heff's ask: "plot historical
    wall-proximity alerts directly on the chart"). Each alert's date+time is logged in ET
    wall-clock time (wall_proximity_alert.py runs during RTH only); converted to a UTC
    epoch second here so it lines up with /api/chart's Yahoo-sourced bar timestamps, which
    chart.js passes through unconverted. Grouped by ticker, most-recent-last, capped at
    ALERT_HISTORY_LIMIT_PER_TICKER. Returns {} (not an exception) if the ledger is missing
    so gex_view() falls back to an empty history per row, same fail-open discipline as
    every other file read in this script."""
    rows = _load_jsonl(DATA / "wall_alert_ledger.jsonl")
    by_ticker = {}
    for r in rows:
        ticker, date_s, time_s = r.get("ticker"), r.get("date"), r.get("time")
        if not ticker or not date_s or not time_s:
            continue
        try:
            dt = datetime.strptime(f"{date_s} {time_s}", "%Y-%m-%d %H:%M").replace(tzinfo=ET)
            epoch = int(dt.timestamp())
        except ValueError:
            continue
        by_ticker.setdefault(ticker, []).append({
            "time": epoch,
            "price": r.get("price"),
            "wall_type": r.get("wall_type"),
            "wall_level": r.get("wall_level"),
            "verdict": r.get("verdict"),
            "actual": r.get("actual"),
            "correct": r.get("correct"),
            "dist_pct": r.get("dist_pct"),
        })
    for ticker, hist in by_ticker.items():
        hist.sort(key=lambda h: h["time"])
        if len(hist) > ALERT_HISTORY_LIMIT_PER_TICKER:
            by_ticker[ticker] = hist[-ALERT_HISTORY_LIMIT_PER_TICKER:]
    return by_ticker


def gex_view():
    """Net GEX/flip/walls per name -- prefers the LIVE snapshot (live_gex.py), falls back to
    the historical Databento research pull only if the live file doesn't exist/is empty yet.
    The 0DTE reads (_gex_view_0dte, added 2026-07-03: SPY + Mag 7) are ALWAYS appended on top
    when present, regardless of which of the other two supplies the base list, since they're a
    distinct same-day-expiration view rather than a substitute for the per-name monthly reads.
    Output field shape is identical across all three (ticker/as_of/regime/net_gex/flip/
    call_wall/put_wall/spot/coverage) so the frontend needed zero changes to pick up any of them.
    Rows are sorted into three tiers (2026-07-03, heff's direction): ETFs (SPY, the 11 sector
    SPDRs, and their -0DTE variants) first, then the Mag 7 (and their -0DTE variants) right
    below, then the remaining ~180 individual names -- alphabetically within each tier so the
    order is stable run to run."""
    live = _gex_view_live()
    base = live if live else _gex_view_historical()
    combined = base + _gex_view_0dte()
    etfs = _etf_tickers()
    combined.sort(key=lambda r: (_gex_sort_tier(r["ticker"], etfs), r["ticker"]))
    history = _alert_history_by_ticker()
    for row in combined:
        row["alert_history"] = history.get(row["ticker"].split("-")[0], [])
    return combined


RD_SEARCH_LIMIT = 100


def _numify(row, fields):
    out = dict(row)
    for k in fields:
        v = out.get(k)
        if v not in (None, ""):
            try:
                out[k] = float(v)
            except (TypeError, ValueError):
                pass
    return out


def rd_history():
    """Ongoing walkforward/continuous-search R&D results -- what's been tested, search-vs-
    holdout R/Sharpe, carried vs mirage, current champion config. Previously only visible via
    Telegram + raw CSVs."""
    search_fields = ("search_total_r", "search_n", "search_per_trade", "search_sharpe")
    wf_fields = ("total_r", "n", "per_trade", "sharpe", "corr")
    search_rows = [_numify(r, search_fields)
                   for r in _load_csv_rows_retry(DATA / "continuous_search_ledger.csv")]
    search_rows.reverse()  # ledger is append-only -> newest first for display
    wf_rows = [_numify(r, wf_fields) for r in _load_csv_rows_retry(DATA / "wf_ledger.csv")]
    champion = _load_json_retry(DATA / "continuous_champion.json", None)
    return {
        "champion": champion,
        "continuous_search": search_rows[:RD_SEARCH_LIMIT],
        "walkforward": wf_rows,
    }


def _wss_edge_scoreboard():
    """Live rolling accuracy for the researched WSS edge (negative-gamma regime + 'holding'
    verdict -- the "🎯 high-conviction" cut wall_proximity_alert.py alerts on), computed
    FRESH from data/wall_alert_ledger.jsonl every time this script runs (~every 2min) -- not
    a frozen one-time snapshot -- so this panel keeps validating (or breaking) automatically
    as wall_alert_scoring.py's 5:10pm ET daily cron appends new days. See wall-proximity-
    alert.md's 2026-07-09 "sharper edge" entries for the research this reproduces.

    Declustered by (ticker, date) WITHIN the filtered subset (regime=='negative' and
    verdict=='holding'), NOT globally first -- reproduces a real bug caught and fixed
    2026-07-09: declustering the whole ledger before filtering silently dropped real
    distinct events whose (ticker, date) pair happened to recur outside the filtered
    subset too.

    `accuracy` is the verdict's own raw hit rate (how often 'holding' correctly predicted
    the wall held) -- it's expected to sit BELOW 50%, that's the edge. `inverted_accuracy`
    (1 - accuracy) is the tradeable read: how often fading the verdict would have been
    right. `z` is a one-sample proportion z-test of `accuracy` against a 50% null --
    matches the exact convention already used in the manual research (verified 2026-07-10
    against the real ledger: n=31/days=4/inverted_accuracy=0.8065/z=-3.41, exact match)."""
    rows = _load_jsonl(DATA / "wall_alert_ledger.jsonl")
    filtered = [r for r in rows if r.get("regime") == "negative" and r.get("verdict") == "holding"]
    seen, declustered = set(), []
    for r in filtered:
        key = (r.get("ticker"), r.get("date"))
        if key in seen:
            continue
        seen.add(key)
        declustered.append(r)

    n = len(declustered)
    if n == 0:
        return {"n": 0, "days": 0, "accuracy": None, "inverted_accuracy": None, "z": None,
                "n_correct": 0, "n_wrong": 0}
    n_correct = sum(1 for r in declustered if r.get("correct") is True)
    n_wrong = n - n_correct
    accuracy = n_correct / n
    se = math.sqrt(0.5 * 0.5 / n)
    z = (accuracy - 0.5) / se
    days = len({r.get("date") for r in declustered})
    return {
        "n": n, "days": days,
        "accuracy": round(accuracy, 4), "inverted_accuracy": round(1 - accuracy, 4),
        "z": round(z, 2), "n_correct": n_correct, "n_wrong": n_wrong,
    }


def capacity_utilization(real_committed):
    """Slots used vs max for both books: equity (portfolio_gate.py's MAX_CONCURRENT/
    MAX_PER_SIDE) and options (the MILP gate's MAX_CONCURRENT_POSITIONS/MAX_POSITIONS_PER_SIDE
    in options_lib.py). `real_committed` (from _real_committed_equity_positions, shared with
    guardrail_status to avoid a second Alpaca round-trip) is the real gated book's positions +
    resting orders -- NOT the ungated paper_open.json/orb_paper_open.json proxy this used to
    read, which tracks every raw signal regardless of the portfolio gate."""
    try:
        import portfolio_gate as pg
        eq_max_concurrent, eq_max_per_side = pg.MAX_CONCURRENT, pg.MAX_PER_SIDE
        eq_capital = pg.TOTAL_CAPITAL
    except Exception as e:
        print(f"  ! capacity_utilization equity config failed: {e}")
        eq_max_concurrent = eq_max_per_side = eq_capital = None
    if real_committed is None:
        equity = None
    else:
        eq_side = {"LONG": 0, "SHORT": 0}
        eq_notional = 0.0
        for p in real_committed:
            s = str(p.get("side", "")).upper()
            if s in eq_side:
                eq_side[s] += 1
            try:
                eq_notional += float(p.get("notional") or 0)
            except (TypeError, ValueError):
                pass
        equity = {
            "open": len(real_committed), "max_concurrent": eq_max_concurrent,
            "long": eq_side["LONG"], "short": eq_side["SHORT"], "max_per_side": eq_max_per_side,
            "notional": round(eq_notional, 2), "total_capital": eq_capital,
        }

    options = None
    db_path = DATA / "options_eval.db"
    if db_path.exists():
        try:
            import sqlite3
            import options_lib as ol
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT legs_metadata FROM trades_ledger WHERE status IN ('OPEN','PARTIAL_CLOSE')"
            ).fetchall()
            conn.close()
            bull = bear = 0
            for r in rows:
                try:
                    d = json.loads(r["legs_metadata"]).get("direction")
                except Exception:
                    d = None
                if d == 1:
                    bull += 1
                elif d == -1:
                    bear += 1
            options = {
                "open": len(rows), "max_concurrent": ol.MAX_CONCURRENT_POSITIONS,
                "bull": bull, "bear": bear, "max_per_side": ol.MAX_POSITIONS_PER_SIDE,
            }
        except Exception as e:
            print(f"  ! capacity_utilization options query failed: {e}")
    return {"equity": equity, "options": options}


def closed_trades_today(today):
    closed = []
    for name in ("paper_trades.csv", "orb_paper_trades.csv"):
        for row in _load_csv_rows_retry(DATA / name):
            ct = row.get("close_time") or ""
            if ct[:10] != today:
                continue
            try:
                outcome_r = float(row["outcome_r"])
                dollar_pnl = float(row.get("dollar_pnl") or 0)
            except (KeyError, ValueError):
                continue
            closed.append({
                "trade_id": row.get("trade_id"), "ticker": row.get("ticker"),
                "outcome_r": outcome_r, "dollar_pnl": dollar_pnl,
                "close_time": ct, "exit_reason": row.get("exit_reason"),
            })
    closed.sort(key=lambda r: r["close_time"])
    return closed


def metrics(closed, open_pos):
    total = len(closed)
    wins = sum(1 for r in closed if r["outcome_r"] > 0)
    losses = total - wins
    return {
        "daily_pnl": round(sum(r["dollar_pnl"] for r in closed), 2),
        "win_rate": round(wins / total, 4) if total else 0,
        "wins": wins, "losses": losses, "total_closed": total,
        "active_trades": len(open_pos),
    }


def _read_log_lines():
    path = LOGS / "executor.log"
    if not path.exists():
        return []
    lines = deque(maxlen=LOG_SCAN_LINES)
    try:
        with path.open(errors="replace") as f:
            for line in f:
                lines.append(ANSI_RE.sub("", line.rstrip("\n")))
    except Exception:
        return []
    return list(lines)


def book_state_and_tail(lines):
    tail = lines[-LOG_TAIL_LINES:]
    book = {"capital": None, "alpaca_equity": None, "mode": None}
    for line in reversed(lines):
        m = BOOK_RE.search(line)
        if m:
            book = {
                "capital": float(m["capital"]),
                "alpaca_equity": float(m["equity"]),
                "mode": f"{m['mode'].strip()} ({m['state'].strip()})",
            }
            break
    return book, tail


MARKET_SYMBOLS = (("^GSPC", "S&P 500"), ("^IXIC", "Nasdaq"), ("^VIX", "VIX"))


def market_pulse():
    try:
        import yfinance as yf
    except Exception:
        return []
    out = []
    for sym, name in MARKET_SYMBOLS:
        try:
            h = yf.Ticker(sym).history(period="5d")["Close"].dropna()
            last = float(h.iloc[-1])
            prev = float(h.iloc[-2])
            out.append({
                "symbol": sym, "name": name,
                "last": round(last, 2),
                "change_pct": round((last / prev - 1) * 100, 2),
            })
        except Exception:
            out.append({"symbol": sym, "name": name, "last": None, "change_pct": None})
    return out


def git_push(repo_dir):
    def _sh(*args):
        return subprocess.run(args, cwd=repo_dir, text=True, capture_output=True)
    # pull first so an out-of-band commit (e.g. a manual schema.md edit) doesn't
    # cause a rejected push every cycle until someone notices and reconciles it
    _sh("git", "fetch", "origin", "--quiet")
    _sh("git", "-c", f"user.name={GIT_NAME}", "-c", f"user.email={GIT_EMAIL}",
        "pull", "--no-rebase", "--quiet")
    _sh("git", "add", "snapshot.json")
    if _sh("git", "diff", "--cached", "--quiet", "--", "snapshot.json").returncode == 0:
        return
    _sh("git", "-c", f"user.name={GIT_NAME}", "-c", f"user.email={GIT_EMAIL}",
        "commit", "-m", f"snapshot {datetime.now(timezone.utc).isoformat()}", "--quiet")
    r = _sh("git", "push", "--quiet")
    if r.returncode:
        print(f"  ! git push deferred: {r.stderr.strip()}")
    else:
        print("  pushed snapshot.json to origin")


def smc_paper_pipeline():
    """Additive bridge from the persistent daemon into BOT_NEXUS."""
    path = DATA / "live_heff_smc" / "daemon_dashboard.json"
    payload = _load_json_retry(path, None)
    if not payload:
        return {"available": False, "reason": "daemon dashboard unavailable"}
    payload["available"] = True
    payload["source_path"] = str(path)
    return payload


def main():
    today = datetime.now(ET).strftime("%Y-%m-%d")

    positions = open_positions()
    signals = active_signals(today)
    closed = closed_trades_today(today)
    log_lines = _read_log_lines()
    book, tail = book_state_and_tail(log_lines)
    premarket, premarket_scanned_at = premarket_candidates(today)
    continuation, continuation_scanned_at = continuation_candidates(today)
    real_committed = _real_committed_equity_positions()

    snapshot = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "session_date": today,
        "book": book,
        "metrics": metrics(closed, positions),
        "market_pulse": market_pulse(),
        "vix_regime": vix_regime(),
        "open_positions": positions,
        "active_signals": signals,
        "closed_trades_today": closed,
        "execution_log_tail": tail,
        "premarket_candidates": premarket,
        "premarket_scanned_at": premarket_scanned_at,
        "continuation_candidates": continuation,
        "continuation_scanned_at": continuation_scanned_at,
        "sector_heatmap": sector_heatmap(),
        "scanner_accuracy": scanner_accuracy(),
        "options_leaderboard": options_leaderboard(),
        "slippage_recon": slippage_recon(),
        "guardrail_status": guardrail_status(real_committed),
        "gex_view": gex_view(),
        "confluence": confluence_view(),
        "hot_setup_chains": hot_setup_chains(),
        "wss_edge_scoreboard": _wss_edge_scoreboard(),
        "rd_history": rd_history(),
        "capacity": capacity_utilization(real_committed),
        "smc_paper_pipeline": smc_paper_pipeline(),
    }

    if not SNAPSHOT_REPO.exists():
        print(f"  ! snapshot repo not found at {SNAPSHOT_REPO}, skipping push")
        return

    (SNAPSHOT_REPO / "snapshot.json").write_text(json.dumps(snapshot, indent=2))
    git_push(SNAPSHOT_REPO)


if __name__ == "__main__":
    main()
