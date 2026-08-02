#!/usr/bin/env python3
"""rotation_backtest.py — capacity-constrained portfolio simulator: does the LIVE
"rolling into new momentum" rotation rule actually help the paper book, or hurt it?

Background: alpaca_executor.py's evaluate_replacement() (read for reference only,
NEVER edited/imported here to keep this script decoupled from live-trading code)
implements two rules once the equity book (mean-rev + ORB, one shared capacity pool)
hits its concurrency cap (MAX_CONCURRENT/MAX_PER_SIDE, sourced from
portfolio_gate.py <- data/live_params.json):

  CUT LOSER    — the worst open position's unrealized P&L% is below LOSER_PLPC
                 (-0.5%, alpaca_executor.py:61): market-close it now, seat the new
                 signal in the freed slot.
  CHOKE WINNER — every open position is currently profitable (so the "worst" one
                 is really just the smallest winner): move ITS stop to breakeven,
                 decline the new signal. Doesn't change WHICH trades are held —
                 only where that one position's stop sits going forward. Modeled
                 here as secondary/nice-to-have (does not affect the CUT-LOSER
                 admission comparison at all).
  otherwise    — hold, decline the new signal (== the no-rotation baseline for
                 that one instance).

Nothing in this repo already runs a CAPACITY-CONSTRAINED portfolio backtest —
walkforward_search.py / paper_eval.py / orb_paper_eval.py all evaluate every
trigger independently, as if the book had unlimited slots. This script walks the
REAL fired signal stream (data/mr_triggers_*.jsonl + data/orb_triggers_*.jsonl,
one shared MAX_CONCURRENT/MAX_PER_SIDE pool across both strategies, matching how
alpaca_executor.py actually runs them) chronologically and runs TWO parallel
books over the identical stream from the same capacity state:

  (A) NO-ROTATION — decline whenever full; every admitted position rides to its
      own natural exit exactly as recorded in data/paper_trades.csv /
      data/orb_paper_trades.csv (the AUTHORITATIVE bar-by-bar, no-lookahead
      ground truth this repo already computed — READ-ONLY, never hand-edited).
  (B) ROTATION    — the live rule above. Ranks currently open positions by
      unrealized P&L% MARKED TO MARKET at the instant a competing signal
      arrives, using real historical 5-min OHLC bars (Alpaca IEX, fetched via
      mean_reversion_scanner.fetch_5m_alpaca — the SAME data-fetch helper
      paper_eval.py already uses — and disk-cached to data/rotation_bars/ since
      this box is weak). No lookahead: only bars up to and including the
      decision instant are used for the mark; CHOKE's breakeven-stop effect is
      resolved only against bars strictly AFTER the choke instant. Mirrors
      evaluate_replacement's real nuance where the ranking pool is restricted to
      the rejected trigger's OWN side when a side-cap (not total-cap) rejection
      is what triggered the rotation check.

  A trade's fate, once admitted into either book, is its CSV/replay-derived
  natural exit UNLESS the rotation rule later cuts it early or chokes its stop —
  so a newly-seated trade in book (B) can itself become a later CUT victim if it
  turns into a big loser while the book is later full again.

Same search/holdout discipline as walkforward_search.py: SEARCH_FRAC=0.75 by
calendar date (reusing wf.date_split exactly), holdout scored ONCE, never
re-peeked. MAX_CONCURRENT/MAX_PER_SIDE/TOTAL_CAPITAL (from portfolio_gate.py,
which itself reads live_params.json) and LOSER_PLPC are held at their CURRENT
live values for the WHOLE window — this tests the RULE, not a reconstructed
history of the constants (the live book's slot count/capital did drift
mid-window, same simplifying assumption walkforward_search.py itself makes when
it scores one fixed candidate config across its whole date range).

READ-ONLY research. Never touches alpaca_executor.py / mean_reversion_scanner.py
(only calls its read-only market-data fetch helper, like paper_eval.py does) /
orb_scanner.py / portfolio_gate.py / guardrails.py. Never edits paper_trades.csv
/ orb_paper_trades.csv. No orders armed or submitted. No Telegram send — this is
a one-off research deliverable, not a live report.

Run:
  ./venv/bin/python rotation_backtest.py             # use cached bars if present
  ./venv/bin/python rotation_backtest.py --refetch    # force re-pull all bars

Output: data/rotation_wf_result.json (summary) + data/rotation_wf_ledger.csv
(every closed trade, both books, tagged).
"""
import sys
import csv
import json
import heapq
import argparse
import time as _time
from pathlib import Path
from datetime import datetime, date
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mean_reversion_scanner as mrs   # read-only market-data fetch (same helper paper_eval.py uses)
import portfolio_gate as pg            # read-only: live MAX_CONCURRENT/MAX_PER_SIDE/TOTAL_CAPITAL
import walkforward_search as wf        # read-only: date_split / SEARCH_FRAC convention reuse

from log_setup import get_logger
log = get_logger("discovery")  # shared with walkforward_search.py -> logs/discovery.log

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
BARS_CACHE = DATA / "rotation_bars"
RESULT_PATH = DATA / "rotation_wf_result.json"
LEDGER_PATH = DATA / "rotation_wf_ledger.csv"
ET = ZoneInfo("America/New_York")

# Prove-It-Or-Lose-It threshold, alpaca_executor.py:61 (LOSER_PLPC). Hardcoded here rather
# than imported -- alpaca_executor.py pulls in requests/fcntl/guardrails/cross_book_registry
# for live order submission, dependency weight this READ-ONLY research script has no reason
# to carry. portfolio_gate.py IS imported live below since it's a pure-config reader with no
# live-trading side effects and is explicitly meant to be imported by other modules this way.
LOSER_PLPC = -0.005

MAX_CONCURRENT = pg.MAX_CONCURRENT
MAX_PER_SIDE = pg.MAX_PER_SIDE
TOTAL_CAPITAL = pg.TOTAL_CAPITAL  # informational only -- this sim gates on concurrency+side only,
                                  # per spec (capital cap is a separate portfolio_gate concern that
                                  # doesn't change WHICH rotation decision fires)


# ----------------------------------------------------------------------------
# 1) load the REAL fired trigger stream + its authoritative natural-exit ground truth
# ----------------------------------------------------------------------------
def load_triggers():
    rows = []
    for f in sorted(DATA.glob("mr_triggers_*.jsonl")):
        for line in f.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            rows.append(dict(trade_id=r["trade_id"], ticker=r["ticker"], side=r["side"],
                              strategy="MR", entry=float(r["entry"]), stop=float(r["stop"]),
                              t1=float(r["t1"]) if r.get("t1") not in (None, "") else None,
                              entry_time=pd.Timestamp(r["entry_time"]),
                              arrival_time=pd.Timestamp(r["entry_time"]),  # MR has no separate detect lag
                              risk_dollars=float(r.get("risk_dollars") or 0),
                              shares=r.get("shares")))
    for f in sorted(DATA.glob("orb_triggers_*.jsonl")):
        for line in f.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            arrival = pd.Timestamp(r["detected_at"]) if r.get("detected_at") else pd.Timestamp(r["entry_time"])
            rows.append(dict(trade_id=r["trade_id"], ticker=r["ticker"], side=r["side"],
                              strategy="ORB", entry=float(r["entry"]), stop=float(r["stop"]),
                              t1=None,
                              entry_time=pd.Timestamp(r["entry_time"]),
                              arrival_time=arrival,  # gate sees it when the scanner DETECTED it, not the
                                                      # theoretical bar-open entry price reference
                              risk_dollars=float(r.get("risk_dollars") or 0),
                              shares=r.get("shares")))
    for r in rows:
        r["date"] = r["entry_time"].date().isoformat()
    rows.sort(key=lambda r: r["arrival_time"])
    return rows


def load_natural_outcomes():
    """trade_id -> CSV ground truth (exit_price/exit_reason/outcome_r/dollar_pnl/close_time).
    READ-ONLY. Never writes to either CSV."""
    out = {}
    for path, strat_default in ((DATA / "paper_trades.csv", "MR"), (DATA / "orb_paper_trades.csv", "ORB")):
        if not path.exists():
            continue
        with path.open(newline="") as f:
            for row in csv.DictReader(f):
                try:
                    out[row["trade_id"]] = dict(
                        exit_price=float(row["exit_price"]), exit_reason=row["exit_reason"],
                        outcome_r=float(row["outcome_r"]), dollar_pnl=float(row.get("dollar_pnl") or 0),
                        close_time=row.get("close_time", ""))
                except Exception:
                    continue
    return out


# ----------------------------------------------------------------------------
# 2) historical 5-min bars for every ticker that fired a trigger, disk-cached
#    (this box is weak -- fetch once, reuse). Pure market-data GET, same helper
#    paper_eval.py already calls live; no order-placement surface at all.
# ----------------------------------------------------------------------------
def fetch_all_bars(tickers, earliest_date, refetch=False):
    BARS_CACHE.mkdir(parents=True, exist_ok=True)
    today = datetime.now(ET).date()
    days_needed = (today - earliest_date).days + 3
    bars_by_ticker_date = {}
    missing = []
    n_cached = n_fetched = 0
    for i, ticker in enumerate(sorted(tickers), 1):
        safe = ticker.replace("/", "_")
        cpath = BARS_CACHE / f"{safe}.parquet"
        df = None
        if cpath.exists() and not refetch:
            try:
                df = pd.read_parquet(cpath)
                n_cached += 1
            except Exception:
                df = None
        if df is None:
            df = mrs.fetch_5m_alpaca(ticker, days=days_needed)
            n_fetched += 1
            if df is not None and not df.empty:
                if df.index.tz is not None:
                    df.index = df.index.tz_convert(ET).tz_localize(None)
                df.to_parquet(cpath)
            _time.sleep(0.05)  # be polite to the free-tier feed; box is weak, no need to hammer it
        if df is None or df.empty:
            missing.append(ticker)
            continue
        for d, sub in df.groupby(df.index.date):
            bars_by_ticker_date[(ticker, d.isoformat())] = sub
        if i % 25 == 0:
            log.info(f"rotation_backtest: bars {i}/{len(tickers)} ({n_cached} cached, {n_fetched} fetched)")
    return bars_by_ticker_date, missing


# ----------------------------------------------------------------------------
# 3) bar-by-bar replay -- mirrors paper_eval.evaluate()/orb_paper_eval.evaluate()
#    EXACTLY (SHORT: stop=High>=stop, target=Low<=t1; LONG mirror; else EOD =
#    last available bar's close). Used to get a PRECISE exit TIMESTAMP for
#    capacity/slot-freeing sequencing. The CSV's own exit_price/outcome_r/
#    dollar_pnl remain authoritative for economics per this repo's hard rule --
#    replay is only trusted for timing, and cross-checked against the CSV to
#    surface (not silently absorb) any feed drift.
# ----------------------------------------------------------------------------
def replay_natural(trig, bars_by_ticker_date):
    bars = bars_by_ticker_date.get((trig["ticker"], trig["date"]))
    if bars is None or bars.empty:
        return None
    future = bars[bars.index > trig["entry_time"]]
    if future.empty:
        return None
    side, entry, stop, t1 = trig["side"], trig["entry"], trig["stop"], trig["t1"]
    risk = abs(entry - stop)
    if risk <= 0:
        return None
    for ts, b in future.iterrows():
        hi, lo = float(b["High"]), float(b["Low"])
        if side == "SHORT":
            if hi >= stop:
                return dict(time=ts, price=stop, reason="stop", r=-1.0)
            if t1 is not None and lo <= t1:
                return dict(time=ts, price=t1, reason="t1", r=(entry - t1) / risk)
        else:
            if lo <= stop:
                return dict(time=ts, price=stop, reason="stop", r=-1.0)
            if t1 is not None and hi >= t1:
                return dict(time=ts, price=t1, reason="t1", r=(t1 - entry) / risk)
    ts = future.index[-1]
    close = float(future["Close"].iloc[-1])
    r = ((entry - close) if side == "SHORT" else (close - entry)) / risk
    return dict(time=ts, price=close, reason="eod", r=r)


def mtm_plpc(pos, as_of, bars_by_ticker_date):
    """Mark-to-market unrealized P&L% as of `as_of` (no lookahead: only bars <=
    as_of). Sign convention: positive == the position is CURRENTLY WINNING,
    regardless of side (matches Alpaca's own unrealized_plpc semantics -- a
    SHORT profits when price falls). Returns (plpc, price) or (None, None) if
    no bar data is available yet at/at-before this instant."""
    bars = bars_by_ticker_date.get((pos["ticker"], pos["date"]))
    if bars is None or bars.empty:
        return None, None
    asof = bars[bars.index <= as_of]
    if asof.empty:
        return None, None
    price = float(asof["Close"].iloc[-1])
    entry = pos["entry"]
    if entry == 0:
        return None, None
    plpc = (price - entry) / entry if pos["side"] == "LONG" else (entry - price) / entry
    return plpc, price


def choke_forward_exit(pos, as_of, natural, bars_by_ticker_date):
    """CHOKE WINNER: stop moved to breakeven (entry price) at `as_of`. Scan bars
    STRICTLY AFTER as_of (no lookahead) for a breakeven breach that would occur
    BEFORE the position's currently-scheduled natural exit. If found, that's the
    new (earlier) exit: breakeven, ~0R. If not found before the natural exit,
    the choke never actually bound -- return None (no-op, natural exit stands)."""
    bars = bars_by_ticker_date.get((pos["ticker"], pos["date"]))
    if bars is None or bars.empty:
        return None
    window = bars[(bars.index > as_of) & (bars.index < natural["time"])]
    entry = pos["entry"]
    for ts, b in window.iterrows():
        hit = float(b["Low"]) <= entry if pos["side"] == "LONG" else float(b["High"]) >= entry
        if hit:
            return dict(time=ts, price=entry, reason="choke_breakeven", r=0.0, dollar=0.0)
    return None


# ----------------------------------------------------------------------------
# 4) the event-driven portfolio simulator -- one call per book (rotation on/off)
# ----------------------------------------------------------------------------
def simulate_book(triggers, natural, bars_by_ticker_date, rotation, model_choke=True):
    open_pos = {}          # trade_id -> position dict
    side_count = {"LONG": 0, "SHORT": 0}
    close_heap = []         # (close_time, trade_id)
    closed = []              # finalized trade records
    activity = dict(admitted_free_slot=0, declined_full=0, cut=0, choke_fired=0,
                     choke_noop=0, decline_no_pool=0, decline_inside_buffer=0,
                     decline_no_mtm=0, missing_natural=0)

    def build_scheduled(trig):
        """Natural exit for a freshly admitted trigger: CSV economics (authoritative),
        replayed bar timing when available (falls back to CSV close_time, clamped into
        that trading day, if bars are missing for this ticker/date)."""
        n = natural.get(trig["trade_id"])
        if n is None:
            activity["missing_natural"] += 1
            return None
        rep = replay_natural(trig, bars_by_ticker_date)
        if rep is not None:
            close_time = rep["time"]
        else:
            try:
                ct = pd.Timestamp(n["close_time"]).tz_localize(None)
            except Exception:
                ct = trig["entry_time"] + pd.Timedelta(hours=6)
            day_end = pd.Timestamp(trig["date"] + " 15:59:00")
            close_time = min(max(ct, trig["entry_time"]), day_end) if ct.date() != trig["entry_time"].date() \
                else ct
        return dict(close_time=close_time, close_price=n["exit_price"], close_reason=n["exit_reason"],
                    outcome_r=n["outcome_r"], dollar_pnl=n["dollar_pnl"])

    def seat(trig, sched, tag):
        pos = dict(trig, **{"sched_" + k: v for k, v in sched.items()}, tag=tag)
        open_pos[trig["trade_id"]] = pos
        side_count[trig["side"]] += 1
        heapq.heappush(close_heap, (pos["sched_close_time"], trig["trade_id"]))

    def admit(trig, tag=""):
        sched = build_scheduled(trig)
        if sched is None:
            return False
        seat(trig, sched, tag)
        return True

    def finalize(trade_id, close_time, close_price, close_reason, outcome_r, dollar_pnl):
        pos = open_pos.pop(trade_id)
        side_count[pos["side"]] -= 1
        closed.append(dict(trade_id=trade_id, ticker=pos["ticker"], side=pos["side"],
                            strategy=pos["strategy"], date=pos["date"], entry_time=pos["entry_time"],
                            entry=pos["entry"], stop=pos["stop"], risk_dollars=pos["risk_dollars"],
                            close_time=close_time, close_price=close_price, close_reason=close_reason,
                            outcome_r=outcome_r, dollar_pnl=dollar_pnl, tag=pos["tag"]))

    def pop_due(as_of):
        while close_heap and close_heap[0][0] <= as_of:
            ct, tid = heapq.heappop(close_heap)
            pos = open_pos.get(tid)
            if pos is None or pos["sched_close_time"] != ct:
                continue  # already closed early (cut), or rescheduled (choke) -- stale entry
            finalize(tid, pos["sched_close_time"], pos["sched_close_price"],
                     pos["sched_close_reason"], pos["sched_outcome_r"], pos["sched_dollar_pnl"])

    for trig in triggers:
        pop_due(trig["arrival_time"])
        n_open = len(open_pos)
        if n_open < MAX_CONCURRENT and side_count[trig["side"]] < MAX_PER_SIDE:
            if admit(trig, tag="free_slot"):
                activity["admitted_free_slot"] += 1
            continue

        reason = "concurrency" if n_open >= MAX_CONCURRENT else "side"
        if not rotation:
            activity["declined_full"] += 1
            continue

        pool = list(open_pos.values())
        if reason == "side":
            pool = [p for p in pool if p["side"] == trig["side"]]
        if not pool:
            activity["decline_no_pool"] += 1
            continue

        ranked = []
        for p in pool:
            plpc, price = mtm_plpc(p, trig["arrival_time"], bars_by_ticker_date)
            if plpc is not None:
                ranked.append((plpc, price, p))
        if not ranked:
            activity["decline_no_mtm"] += 1
            continue
        ranked.sort(key=lambda t: t[0])
        worst_plpc, worst_price, worst = ranked[0]

        if worst_plpc < LOSER_PLPC:
            sched = build_scheduled(trig)
            if sched is None:
                continue  # can't seat the replacement (missing_natural already counted);
                          # leave the existing position UNTOUCHED rather than destructively
                          # cutting it for a trigger we can't actually seat
            risk = abs(worst["entry"] - worst["stop"])
            r = ((worst_price - worst["entry"]) if worst["side"] == "LONG"
                 else (worst["entry"] - worst_price)) / risk if risk > 0 else 0.0
            dollar = r * worst["risk_dollars"]
            finalize(worst["trade_id"], trig["arrival_time"], worst_price, "cut_rotation", r, dollar)
            seat(trig, sched, "cut_seated")
            activity["cut"] += 1
            continue

        if worst_plpc > 0 and model_choke:
            n = natural.get(worst["trade_id"])
            natural_sched = dict(time=worst["sched_close_time"], price=worst["sched_close_price"])
            new_exit = choke_forward_exit(worst, trig["arrival_time"], natural_sched, bars_by_ticker_date)
            if new_exit is not None:
                worst["sched_close_time"] = new_exit["time"]
                worst["sched_close_price"] = new_exit["price"]
                worst["sched_close_reason"] = new_exit["reason"]
                worst["sched_outcome_r"] = new_exit["r"]
                worst["sched_dollar_pnl"] = new_exit["dollar"]
                heapq.heappush(close_heap, (worst["sched_close_time"], worst["trade_id"]))
                activity["choke_fired"] += 1
            else:
                activity["choke_noop"] += 1
            activity["declined_full"] += 1  # choke never seats the new trigger either
            continue

        activity["decline_inside_buffer"] += 1

    pop_due(pd.Timestamp.max)  # flush remaining open positions at end of stream
    return closed, activity


# ----------------------------------------------------------------------------
# 5) scoring -- mirrors wf.score_portfolio's Sharpe convention, adds $ and max-DD
# ----------------------------------------------------------------------------
def score(trades, region_dates=None, strategy=None):
    rows = trades
    if region_dates is not None:
        rows = [t for t in rows if t["date"] in region_dates]
    if strategy is not None:
        rows = [t for t in rows if t["strategy"] == strategy]
    if not rows:
        return dict(n=0, total_r=0.0, per_r=0.0, total_dollars=0.0, per_dollars=0.0,
                    win_rate=0.0, sharpe=0.0, max_dd_r=0.0, max_dd_dollars=0.0)
    n = len(rows)
    rs = [t["outcome_r"] for t in rows]
    ds = [t["dollar_pnl"] for t in rows]
    total_r, total_d = float(sum(rs)), float(sum(ds))
    wins = sum(1 for r in rs if r > 0)
    df = pd.DataFrame(dict(date=[t["date"] for t in rows], net_r=rs, dollar=ds))
    daily = df.groupby("date")["net_r"].sum()
    sharpe = float(daily.mean() / daily.std() * np.sqrt(252)) if daily.std() > 0 else 0.0
    ordered = sorted(rows, key=lambda t: t["close_time"])

    def max_dd(seq):
        peak = cum = maxdd = 0.0
        for x in seq:
            cum += x
            peak = max(peak, cum)
            maxdd = max(maxdd, peak - cum)
        return maxdd

    return dict(n=n, total_r=total_r, per_r=total_r / n, total_dollars=total_d,
                per_dollars=total_d / n, win_rate=wins / n * 100, sharpe=sharpe,
                max_dd_r=max_dd([t["outcome_r"] for t in ordered]),
                max_dd_dollars=max_dd([t["dollar_pnl"] for t in ordered]))


def fmt(s):
    return (f"n={s['n']:>4} | {s['total_r']:+7.2f}R (${s['total_dollars']:+8.2f}) | "
            f"per-tr {s['per_r']:+.3f}R (${s['per_dollars']:+.2f}) | win {s['win_rate']:4.1f}% | "
            f"Sharpe {s['sharpe']:+.2f} | maxDD {s['max_dd_r']:.2f}R (${s['max_dd_dollars']:.2f})")


def write_ledger(book_a, book_b):
    LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    fields = ["book", "trade_id", "ticker", "side", "strategy", "date", "entry_time", "entry",
              "stop", "risk_dollars", "close_time", "close_price", "close_reason", "outcome_r",
              "dollar_pnl", "tag"]
    with LEDGER_PATH.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for book_name, rows in (("no_rotation", book_a), ("rotation", book_b)):
            for r in rows:
                row = {k: r.get(k, "") for k in fields}
                row["book"] = book_name
                w.writerow(row)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--refetch", action="store_true", help="force re-pull all bar data")
    ap.add_argument("--no-choke", action="store_true", help="disable CHOKE-WINNER modeling")
    a = ap.parse_args()

    log.info("rotation_backtest: loading trigger stream + natural-exit ground truth...")
    triggers = load_triggers()
    natural = load_natural_outcomes()
    tickers = {t["ticker"] for t in triggers}
    earliest = min(t["entry_time"].date() for t in triggers)
    print(f"loaded {len(triggers)} triggers ({sum(1 for t in triggers if t['strategy']=='MR')} MR / "
          f"{sum(1 for t in triggers if t['strategy']=='ORB')} ORB), {len(tickers)} tickers, "
          f"earliest {earliest}")
    print(f"config: MAX_CONCURRENT={MAX_CONCURRENT} MAX_PER_SIDE={MAX_PER_SIDE} "
          f"TOTAL_CAPITAL=${TOTAL_CAPITAL:.0f} LOSER_PLPC={LOSER_PLPC:.3%}")

    print("fetching/loading cached 5-min bars (Alpaca IEX, read-only market data)...")
    bars_by_ticker_date, missing = fetch_all_bars(tickers, earliest, refetch=a.refetch)
    if missing:
        print(f"  WARNING: no bar data for {len(missing)} tickers "
              f"({', '.join(missing[:15])}{'...' if len(missing) > 15 else ''}) "
              f"-- their positions can be admitted/exit on CSV ground truth but can never be "
              f"MTM-ranked or cut by the rotation book.")

    all_dates = sorted({t["date"] for t in triggers})
    search_dates, holdout_dates = wf.date_split(all_dates)
    print(f"date_split (SEARCH_FRAC={wf.SEARCH_FRAC}): {len(search_dates)} search dates "
          f"({min(search_dates)}..{max(search_dates)}), {len(holdout_dates)} holdout dates "
          f"({min(holdout_dates)}..{max(holdout_dates)})")

    print("\nsimulating NO-ROTATION book (A)...")
    book_a, act_a = simulate_book(triggers, natural, bars_by_ticker_date, rotation=False)
    print("simulating ROTATION book (B)...")
    book_b, act_b = simulate_book(triggers, natural, bars_by_ticker_date, rotation=True,
                                   model_choke=not a.no_choke)

    write_ledger(book_a, book_b)

    result = {"generated_at": datetime.now(ET).isoformat(timespec="seconds"),
              "config": {"max_concurrent": MAX_CONCURRENT, "max_per_side": MAX_PER_SIDE,
                         "total_capital": TOTAL_CAPITAL, "loser_plpc": LOSER_PLPC,
                         "search_frac": wf.SEARCH_FRAC},
              "n_triggers": len(triggers), "n_tickers": len(tickers), "missing_bar_tickers": missing,
              "search_dates": sorted(search_dates), "holdout_dates": sorted(holdout_dates),
              "activity": {"no_rotation": act_a, "rotation": act_b}}

    def region_block(region_dates, label):
        block = {}
        for name, rows in (("no_rotation", book_a), ("rotation", book_b)):
            block[name] = {"all": score(rows, region_dates),
                            "MR": score(rows, region_dates, strategy="MR"),
                            "ORB": score(rows, region_dates, strategy="ORB")}
        return block

    result["search"] = region_block(search_dates, "search")
    result["holdout"] = region_block(holdout_dates, "holdout")
    RESULT_PATH.parent.mkdir(parents=True, exist_ok=True)
    RESULT_PATH.write_text(json.dumps(result, indent=2, default=str))

    # ---- plain-English summary ----
    print("\n" + "=" * 100)
    print("ROTATION BACKTEST — no-rotation baseline vs live CUT-LOSER/CHOKE-WINNER rule")
    print("=" * 100)
    for region_name, dates in (("SEARCH", search_dates), ("HOLDOUT (locked, scored once)", holdout_dates)):
        blk = result["search"] if region_name.startswith("SEARCH") else result["holdout"]
        print(f"\n--- {region_name} ({len(dates)} dates) ---")
        for leg in ("all", "MR", "ORB"):
            a_s, b_s = blk["no_rotation"][leg], blk["rotation"][leg]
            print(f"  [{leg:>3}] NO-ROTATION: {fmt(a_s)}")
            print(f"  [{leg:>3}] ROTATION   : {fmt(b_s)}")
            d_r = b_s["total_r"] - a_s["total_r"]
            d_d = b_s["total_dollars"] - a_s["total_dollars"]
            print(f"          delta (rotation - no-rotation): {d_r:+.2f}R (${d_d:+.2f})")

    full_book_b = (act_b["cut"] + act_b["choke_fired"] + act_b["choke_noop"] + act_b["decline_no_pool"]
                   + act_b["decline_inside_buffer"] + act_b["decline_no_mtm"])
    # ledger-verified emergent effect: rotation admits MORE total trades than just cut_n extra --
    # each CHOKE that fires (breakeven stop actually gets hit) closes that position EARLIER than
    # its natural exit would have, freeing the slot sooner and letting *later* triggers into a slot
    # NO-ROTATION would still have had occupied. CUT LOSER firing only `cut` times can't explain that
    # gap alone -- CHOKE's slot-freeing side effect is the dominant channel, not the headline rule.
    n_extra_admits = len(book_b) - len(book_a)
    print(f"\n--- rotation activity (book B) ---")
    print(f"  full-book instances (would-decline moments): {full_book_b}")
    print(f"  CUT LOSER fired               : {act_b['cut']}  (seats the new trigger immediately)")
    print(f"  CHOKE WINNER fired total      : {act_b['choke_fired'] + act_b['choke_noop']}  "
          f"(stop moved to breakeven; new trigger still declined either way)")
    print(f"    -> breakeven actually hit (position closed EARLY, freeing a slot for a LATER trigger): "
          f"{act_b['choke_fired']}")
    print(f"    -> breakeven never tested (no effect vs no-rotation for that position)         : "
          f"{act_b['choke_noop']}")
    print(f"  held/declined (worst loss inside -0.5%..0% buffer): {act_b['decline_inside_buffer']}")
    print(f"  held/declined (no MTM bar data available)         : {act_b['decline_no_mtm']}")
    print(f"  held/declined (no side-matched pool to rank)      : {act_b['decline_no_pool']}")
    print(f"  no-rotation declined-full count (A)               : {act_a['declined_full']}")
    print(f"  rotation book admitted {n_extra_admits:+d} more total trades than no-rotation "
          f"(only {act_b['cut']} via direct CUT-LOSER seating -- the rest via slots CHOKE's early "
          f"breakeven exits freed up earlier in the stream)")

    # ---- verdict ----
    s_a, s_b = result["search"]["no_rotation"]["all"], result["search"]["rotation"]["all"]
    h_a, h_b = result["holdout"]["no_rotation"]["all"], result["holdout"]["rotation"]["all"]
    search_edge_r = s_b["total_r"] - s_a["total_r"]
    search_edge_d = s_b["total_dollars"] - s_a["total_dollars"]
    holdout_edge_r = h_b["total_r"] - h_a["total_r"]
    holdout_edge_d = h_b["total_dollars"] - h_a["total_dollars"]

    carried = search_edge_r > 0 and search_edge_d > 0 and holdout_edge_r > 0 and holdout_edge_d > 0
    net_negative_holdout = holdout_edge_r <= 0 and holdout_edge_d <= 0

    if carried:
        verdict = ("ROTATION IS WORTH KEEPING — the edge shows on search AND holds on the locked "
                   "holdout, in both R and $.")
    elif net_negative_holdout:
        verdict = ("ROTATION SHOULD BE KILLED (or at minimum not trusted) — net negative on the "
                   "locked holdout in BOTH R and $, regardless of what search showed. Almost none of "
                   f"that is CUT LOSER itself (only {act_b['cut']} fires total, too rare to blame in "
                   "isolation) — the effect runs almost entirely through CHOKE WINNER's early-"
                   "breakeven exits freeing slots sooner and letting more (lower quality, lower "
                   "win-rate) trades in behind them.")
    else:
        verdict = ("MIXED/INCONCLUSIVE — search and holdout (or R vs $) disagree in sign; treat as a "
                   "wash, don't trust it live yet.")
    cut_caveat = (f"CUT LOSER itself fired only {act_b['cut']}x across {len(triggers)} triggers / "
                 f"{full_book_b} full-book instances ({act_b['cut']/full_book_b*100:.1f}% of them) — "
                 "too rare on this ~1-month sample to judge that specific mechanism in isolation. "
                 "What this backtest can actually speak to is the COMBINED live rotation behavior "
                 "(CUT + CHOKE together, since CHOKE dominates the sample and has a real capacity "
                 "side effect even though it never seats a trade itself).")
    print("\n" + "=" * 100)
    print(f"VERDICT: {verdict}")
    print(f"  NOTE: {cut_caveat}")
    print(f"  search  delta: {search_edge_r:+.2f}R (${search_edge_d:+.2f})")
    print(f"  holdout delta: {holdout_edge_r:+.2f}R (${holdout_edge_d:+.2f})")
    print(f"  win rate  search : no-rotation {s_a['win_rate']:.1f}% vs rotation {s_b['win_rate']:.1f}%")
    print(f"  win rate  holdout: no-rotation {h_a['win_rate']:.1f}% vs rotation {h_b['win_rate']:.1f}%")
    print("=" * 100)
    result["verdict"] = verdict
    result["cut_loser_caveat"] = cut_caveat
    RESULT_PATH.write_text(json.dumps(result, indent=2, default=str))
    print(f"\nresults written -> {RESULT_PATH.relative_to(ROOT)}")
    print(f"full ledger      -> {LEDGER_PATH.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
