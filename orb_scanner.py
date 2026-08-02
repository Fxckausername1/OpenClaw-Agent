#!/usr/bin/env python3
"""Live Opening Range Breakout signaler (paper) — the 2nd edge, run alongside
the mean-reversion scanner.

Each 15-min run (after 9:45 ET, market open): for each S&P-100 name (<=$250, not
in earnings blackout) mark the 9:30-9:45 opening range, detect the FIRST break of
the range, and if the breakout bar is on the trend side of VWAP AND has elevated
volume (relvol>=1.5), fire an ORB TRIGGER to Telegram + log it for paper tracking.
Momentum trade: stop = opposite end of the range, exit at the close (no target).
One signal per symbol per day. Reuses the mean-rev scanner's helpers + live
earnings cache.
"""
import sys
import json
import fcntl
import argparse
from pathlib import Path
from datetime import datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

import mean_reversion_scanner as mrs
import sector_rotation as secrot

from log_setup import get_logger
log = get_logger("scanner_orb")

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
STATE_DIR = DATA / "orb_state"
MSG = DATA / "orb_message_latest.txt"
LOCK_PATH = STATE_DIR / ".scan.lock"
ET = ZoneInfo("America/New_York")
OR_END = dtime(9, 45)
# vol_mult/max_range_frac: live-promotable (see data/live_params.json + promote_champion.py).
# Defaults below match the current holdout-confirmed champion exactly -- they're the
# fail-open fallback if live_params.json is ever missing/corrupt, not a second source of truth.
_LIVE = mrs.load_live_params()
VOL_MULT = _LIVE["orb"].get("vol_mult", 1.5)
MAX_RANGE_FRAC = _LIVE["orb"].get("max_range_frac", 0.0066)
USE_VWAP = _LIVE["orb"].get("use_vwap", True)
USE_VOL = _LIVE["orb"].get("use_vol", True)
# Sector-rotation gate ACTIVATED 2026-07-02 (was tag-only since 2026-07-01). Re-derived the
# exact holdout numbers directly against the cached walkforward components rather than trust
# the earlier summary: ORB holdout per-trade R 0.050->0.100 (1.98x) and Sharpe 2.27->3.30 when
# gated to hot-sector names, DIRECTIONALLY CONSISTENT with the search-region read too (a real,
# carried edge, not a fluke) -- it only missed live promotion because the original walkforward
# ratchet ranks candidates by TOTAL R, and gating cuts ORB trade count ~2x, so total R came out
# flat even though per-trade quality nearly doubled (same blind spot tight-range ORB almost got
# buried by). MR's sector gate is a genuine wash (0.97x) and stays untouched/tag-only.
USE_SECTOR_GATE = _LIVE["orb"].get("use_sector_gate", True)
# Range-compression ranking (2026-07-01): ORB signals previously carried no planned_rr, so
# portfolio_gate.rank_key() defaulted every one to NEUTRAL_RR=2.0 -- when multiple ORB
# candidates competed for a slot the gate could not tell them apart and tie-broke on risk$
# alone. portfolio_sim.py showed this mattered: admitted ORB trades underperformed withheld
# ORB trades (42% win/+12.7R vs 55% win/+28.1R) -- the gate was picking somewhat arbitrarily
# among ORB candidates. Tight-range is ORB's one holdout-confirmed refinement (see
# MAX_RANGE_FRAC above), so rank tighter coils higher: planned_rr = 1/range%, capped.
# NOTE this planned_rr now competes directly against mean-rev's genuine entry/stop/target
# R:R in the SAME gate ranking -- it is a quality proxy, not a real reward:risk ratio, so a
# very tight ORB coil can now outrank a merely-average MR setup. Flagged since it changes
# cross-strategy prioritization, not just ORB-vs-ORB -- confirm this is the intended
# tradeoff before trusting the ranking blindly.
ORB_RR_CAP = 5.0
NEUTRAL_RR_FALLBACK = 2.0
# Stale-break guard + lag instrumentation (2026-06-29): a break detected more than STALE_MIN
# minutes after it actually occurred is SKIPPED -- the entry level is no longer actionable
# (the 2026-06-29 incident: IEX bars backfilled ~87min late, so breaks "appeared" hours
# after the move). Normal detection lag is ~6min (5-min bar close + 2-min scan cadence), so
# 15 never rejects a healthy fire. Every fired trigger now also carries detected_at + lag_min
# so the lag is measured directly instead of inferred from file mtimes.
STALE_MIN = _LIVE["orb"].get("stale_minutes", 15.0)


def load_state(d):
    p = STATE_DIR / f"{d.isoformat()}.txt"
    return set(p.read_text().split()) if p.exists() else set()


def add_state(d, syms):
    if not syms:
        return
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with (STATE_DIR / f"{d.isoformat()}.txt").open("a") as f:
        for s in syms:
            f.write(s + "\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--max-tickers", type=int, default=6000)
    args = ap.parse_args()

    now = datetime.now(ET)
    if not args.force and not mrs.market_is_open(now):
        log.info("Market closed; skipping.")
        return
    if not args.force and now.time() < OR_END:
        log.info("Opening range not complete yet (before 9:45).")
        return

    # Non-blocking lock: with the wide universe, a scan can take close to the 2-min cron
    # cadence, so without this, overlapping runs were stacking up uncapped (found + fixed
    # 2026-06-26) -- if a run is still going, just skip this tick rather than pile on.
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    lock_fp = open(LOCK_PATH, "w")
    try:
        fcntl.flock(lock_fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError):
        log.debug("previous ORB scan still running; skipping this tick.")
        return

    today = now.date()
    blk = mrs.earnings_blacklist(today)
    resolved = load_state(today)
    newly, alerts, triggers = [], [], []

    # WIDE universe (2026-06-26): all NYSE/NASDAQ common stock $5-$266, not just the
    # backtested 99 -- see wide_universe.py. Fails open to the curated 99 if it errors.
    core99 = mrs.core99_set()
    try:
        import wide_universe as wu
        tickers = wu.load_universe_for_scanning(rebuild_if_stale=True) or list(core99)
    except Exception:
        tickers = list(core99)
    tickers = tickers[:args.max_tickers]
    # sector-rotation TAG + GATE (tag since 2026-07-01, gate ACTIVATED 2026-07-02 -- see
    # USE_SECTOR_GATE above). Loaded once per run (files are small + static within a run)
    # rather than per-ticker. None on an unmapped ticker or missing rotation data -> tag
    # fields land as null AND the gate fails OPEN (allowed), same discipline as regime_ok
    # and every other gate in this codebase.
    sector_map = secrot.load_sector_map()
    sector_quadrants = secrot.load_latest_quadrants()
    mrs.prefetch_batch_bars(tickers, days=1)   # ORB's vwap/avgvol are same-day only, no multi-day lookback needed

    for tk in tickers:
        if tk in blk or tk in resolved:
            continue
        try:
            df = mrs.get_5m_data(tk, days=2)
        except Exception:
            continue
        if df is None:
            continue
        td = df[df.index.date == today]
        if len(td) < 3:
            continue
        orb = td[td.index.time < OR_END]
        post = td[td.index.time >= OR_END]
        # only evaluate FULLY-CLOSED 5-min bars: the currently-forming bar has partial
        # volume that understates relvol, and since a symbol is "resolved" on its first
        # break, an incomplete bar would burn it before its real volume-confirmed break.
        post = post[post.index <= now - timedelta(minutes=5)]
        if len(orb) < 1 or len(post) < 1:
            continue
        orh = float(orb["High"].max()); orl = float(orb["Low"].min())
        if orh <= 0 or orh - orl <= 0 or orh > mrs.MAX_PRICE:
            continue
        if (orh - orl) / orh > MAX_RANGE_FRAC:
            continue  # opening range too WIDE -> skip (tight-range ORB edge)
        typ = (td["High"] + td["Low"] + td["Close"]) / 3.0
        vwap = (typ * td["Volume"]).cumsum() / td["Volume"].cumsum()
        avgvol = td["Volume"].expanding().mean()
        # sector doesn't change intraday -- compute once per symbol, not per bar
        sec_tag = secrot.ticker_sector_tag(tk, sector_map, sector_quadrants)
        take_sector = (sec_tag["sector_hot"] is not False) if (USE_SECTOR_GATE and sec_tag) else True

        for ts, b in post.iterrows():
            # Break = bar CLOSE beyond the opening range (matches the holdout-validated
            # walkforward component). A High/Low *wick* touch fired on weak early ticks
            # that failed the volume filter, and since the symbol is abandoned after its
            # first break, live never reached the decisive close-break the backtest catches
            # -> live fired ~0 triggers. Close-based break restores fidelity to the edge.
            up = b["Close"] > orh
            dn = b["Close"] < orl
            if not (up or dn):
                continue
            side = ("LONG" if b["Close"] >= b["Open"] else "SHORT") if (up and dn) else ("LONG" if up else "SHORT")
            entry = orh if side == "LONG" else orl
            stop = orl if side == "LONG" else orh
            vw = float(vwap.loc[ts]); av = float(avgvol.loc[ts])
            relvol = b["Volume"] / av if av > 0 else 0
            take_vwap = ((b["Close"] > vw) if side == "LONG" else (b["Close"] < vw)) if USE_VWAP else True
            take_vol = (relvol >= VOL_MULT) if USE_VOL else True
            newly.append(tk)
            if take_vwap and take_vol and not take_sector:
                log.info(f"SKIP {tk} {side}: sector not hot ({(sec_tag or {}).get('sector_etf')} "
                         f"{(sec_tag or {}).get('sector_quadrant')}) -- otherwise a valid ORB break")
            if take_vwap and take_vol and take_sector:
                lag_min = (now - ts).total_seconds() / 60.0
                if lag_min > STALE_MIN:
                    log.info(f"SKIP stale ORB break {tk} {side}: detected {lag_min:.0f}min after the "
                             f"{ts.strftime('%H:%M')} break (cap {STALE_MIN:.0f}min) -- entry no longer fresh")
                    break
                rng = (orh - orl) / orh * 100
                planned_rr = round(min(ORB_RR_CAP, 1.0 / rng), 2) if rng > 0 else NEUTRAL_RR_FALLBACK
                risk = abs(entry - stop)
                # whole-share sizing (shared with mean-rev): ORB stops = the OR width,
                # often wider than 0.7%, so the $8 risk cap genuinely binds here and
                # skips wide-range breakouts (0 sh) that can't fit one whole share.
                shares, notional, risk_dollars = mrs.size_trade(entry, stop)
                size = (f"{shares} sh (${notional:.0f}, risk ${risk_dollars:.2f})"
                        if shares > 0 else "0 sh — SKIP: 1 whole share exceeds $8 risk cap")
                alerts.append(f"{side} {tk} | {size} | entry {entry:.2f} (OR break) | stop {stop:.2f} "
                              f"(risk {risk:.2f}/sh) | exit EOD | range {rng:.1f}% · relvol {relvol:.1f}x")
                triggers.append({"trade_id": f"ORB:{tk}:{today.isoformat()}", "strategy": "ORB",
                                 "ticker": tk, "side": side, "entry": round(entry, 4),
                                 "stop": round(stop, 4), "target": None, "planned_rr": planned_rr,
                                 "entry_time": ts.tz_localize(None).isoformat(),
                                 "detected_at": now.replace(tzinfo=None, microsecond=0).isoformat(),
                                 "lag_min": round(lag_min, 1),
                                 "shares": int(shares), "notional": notional,
                                 "risk_dollars": risk_dollars,
                                 "relvol": round(relvol, 2), "rng": round(rng, 2), "vwap": round(vw, 4),
                                 # technical readout for the dashboard
                                 # "wide500k" (not the old unfiltered "wide") since 2026-06-28
                                 "universe": "core99" if tk in core99 else "wide500k",
                                 **(sec_tag or {"sector_etf": None, "sector_quadrant": None, "sector_hot": None})})
            break

    add_state(today, newly)
    if triggers:
        with (DATA / f"orb_triggers_{today.isoformat()}.jsonl").open("a") as f:
            for t in triggers:
                f.write(json.dumps(t) + "\n")
    if alerts:
        msg = (f"\U0001F680 ORB momentum -- {now.strftime('%b %d %H:%M %Z')} "
               f"(confirm before placing):\n" + "\n".join(f"• {x}" for x in alerts))
        MSG.write_text(msg)
        log.info(msg)
    else:
        log.info(f"No ORB triggers at {now.strftime('%H:%M %Z')}")
        if MSG.exists():
            MSG.unlink()


if __name__ == "__main__":
    main()
