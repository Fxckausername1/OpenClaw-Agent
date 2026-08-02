#!/usr/bin/env python3
"""continuous_search.py - ongoing, incremental strategy search across the FULL equity
dataset (99-symbol, 2yr cache), using a much WIDER parameter grid than the hand-curated
CANDIDATES list in walkforward_search.py.

Per heff's direction (2026-06-25): use all the data, loosen the search (more z/vdev/min_rr/
vol_mult/max_range_frac values, filters toggled on/off), run continuously via cron post-close
instead of one-off manual runs, Telegram every result so progress is visible even on quiet
nights.

THE ONE THING THIS DOES NOT LOOSEN: the locked 75/25 holdout carry check. Reason, stated
plainly: with a wide grid, SOME combination will look good on the search region by pure
chance -- that's not an edge, it's noise that fits. The holdout check is what tells the two
apart; loosening it would not find more edges, it would just promote more mirages (exactly
what killed risk-off / regime-ORB / VIX / time-of-day earlier in this project). Everything
else -- which configs, how many, how wide the ranges, which filters are on/off -- is wide
open here.

Incremental: every config tried is logged to data/continuous_search_ledger.csv keyed by its
component hash (same hashing walkforward_search.py's component cache already uses), so a
config is never re-tested. Each run pulls the next BATCH of untried configs from the grid
(--budget-configs, default 25, sized to fit one post-close window on the 1.9GB box) and
scores them against the current champion legs, coordinate-wise (vary one leg, hold the other
at its best-known config) -- the same way every manual sweep in this project was already
done, just automated and widened. A new champion leg is promoted ONLY if the resulting
portfolio beats the current champion on search AND carries on the locked holdout.

GEX regime conditioning is intentionally NOT folded into this generic grid (it needs a
per-symbol regime lookup the standard df,p generator signature doesn't carry, and the data
is thin/asymmetric per BT1 2026-06-25) -- that stays on its own track (gex_regime_backtest.py
+ the live MR-side regime tag). This script covers the broad, data-rich equity-only space.

Usage:
  ./venv/bin/python continuous_search.py                  # one incremental batch, Telegram report
  ./venv/bin/python continuous_search.py --budget-configs 50
"""
import sys
import json
import argparse
import hashlib
from datetime import datetime, timezone, time as dtime, date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from walkforward_search import (
    load_cached, generate_component, component_key, date_split,
    score_portfolio, comp_mr, comp_orb, comp_orbsec, MR_CAP, ORB_CAP, MIN_TRADES, tg, ROOT,
)
import mean_reversion_scanner as mr
import wide_universe

from log_setup import get_logger
log = get_logger("continuous_search")

LEDGER = ROOT / "data" / "continuous_search_ledger.csv"
CHAMP_OUT = ROOT / "data" / "continuous_champion.json"


# ----------------------------------------------------------------------------
# WIDE grid - loosened per heff's direction (2026-06-25). Coordinate-wise: every mean-rev
# variant is paired with the current champion's ORB leg, every ORB variant with the current
# champion's mean-rev leg, so the grid stays additive (not a combinatorial explosion) while
# still covering a much wider space than the hand-picked CANDIDATES list.
# ----------------------------------------------------------------------------
def build_grid():
    # BUG FOUND + FIXED 2026-07-02: this used to be one flat list, MR's ~174 combos fully
    # BEFORE any of ORB's ~431 combos. At the default --budget-configs=25/night, that meant
    # ~7 nights just to exhaust MR before ORB was EVER reached -- confirmed live: 75 logged
    # nights, 75/75 rows tagged leg=mr in data/continuous_search_ledger.csv, ZERO orb rows,
    # despite the code fully supporting an ORB grid the whole time. Meanwhile MR's
    # neighborhood near the champion has shown ~0% hit rate (75 attempts, all mirage/
    # no-improve) while ORB is the leg with an actual track record (tight-range + sector-gate
    # both holdout-confirmed) -- the search budget was going entirely to the worse-odds leg.
    # Fix: build each leg's list separately, then ROUND-ROBIN INTERLEAVE them so every night's
    # budget covers both legs proportionally instead of fully draining one first.
    mr_grid = []
    for z in [1.0, 1.25, 1.5, 1.75, 2.0, 2.25, 2.5]:
        for vdev in [0.005, 0.010, 0.015, 0.020, 0.025]:
            for min_rr in [1.0, 1.25, 1.5, 1.75, 2.0]:
                if z == 1.5 and vdev == 0.015 and min_rr == 1.5:
                    continue  # == current champion mr leg, already known
                name = f"mr_z{z}_v{vdev}_rr{min_rr}"
                mr_grid.append(("mr", comp_mr(name, z=z, vdev=vdev, min_rr=min_rr)))
    orb_grid = []
    for vol_mult in [1.0, 1.25, 1.5, 1.75, 2.0, 2.5]:
        for max_range_frac in [None, 0.0060, 0.0066, 0.0080, 0.0100, 0.0120]:
            for use_vwap in (True, False):
                for use_vol in (True, False):
                    # SECTOR-ROTATION AXIS (2026-07-05): sibling sector-gated variant of every
                    # ORB config, via comp_orbsec/gen_orb_sector (already holdout-confirmed live
                    # as use_sector_gate -- see live_params.json's 2026-07-02 promotion note --
                    # just never before covered by THIS search's own grid). Additive: doubles
                    # ORB's grid (~144->~288), so it takes ~2x as many nights to fully cycle, but
                    # every ORB config the search ever tries now also gets a sector-gated sibling
                    # tested against the same locked holdout.
                    for sector_gate in (False, True):
                        if (vol_mult == 1.5 and max_range_frac is None and use_vwap and use_vol
                                and not sector_gate):
                            continue  # == current champion orb leg, already known
                        base_name = f"orb_vm{vol_mult}_mr{max_range_frac}_{use_vwap}{use_vol}"
                        if sector_gate:
                            comp = comp_orbsec(base_name + "_sector", vol_mult=vol_mult,
                                                max_range_frac=max_range_frac, use_vwap=use_vwap,
                                                use_vol=use_vol)
                        else:
                            comp = comp_orb(base_name, vol_mult=vol_mult, max_range_frac=max_range_frac,
                                             use_vwap=use_vwap, use_vol=use_vol)
                        orb_grid.append(("orb", comp))
    grid = []
    for i in range(max(len(mr_grid), len(orb_grid))):
        if i < len(mr_grid):
            grid.append(mr_grid[i])
        if i < len(orb_grid):
            grid.append(orb_grid[i])
    return grid


def load_tried():
    if not LEDGER.exists():
        return set()
    try:
        return set(pd.read_csv(LEDGER)["hash"].astype(str))
    except Exception:
        return set()


def append_ledger(rows):
    df = pd.DataFrame(rows)
    if LEDGER.exists():
        df.to_csv(LEDGER, mode="a", header=False, index=False)
    else:
        df.to_csv(LEDGER, index=False)


def load_champion():
    if CHAMP_OUT.exists():
        try:
            champ = json.loads(CHAMP_OUT.read_text())
            # or_end round-trips through save_champion's json.dumps(default=str) as a plain
            # "HH:MM:SS" string (datetime.time isn't JSON-native) -- reconstruct it here, or
            # gen_orb's `times < or_end` comparison TypeErrors (str vs datetime.time). Latent
            # since day one; only surfaced 2026-06-28 when the wf_comp disk cache (which
            # always had a real time object baked in from whatever run first generated that
            # exact component) got cleared for the float32-downcast cache migration, forcing
            # a fresh generation that actually exercises this code path for the first time.
            oe = champ.get("orb", {}).get("p", {}).get("or_end")
            if isinstance(oe, str):
                champ["orb"]["p"]["or_end"] = dtime.fromisoformat(oe)
            return champ
        except Exception:
            pass
    return {"mr": MR_CAP, "orb": ORB_CAP, "search": None, "holdout": None}


def save_champion(champ):
    CHAMP_OUT.write_text(json.dumps(champ, indent=2, default=str))


def _nth_weekday(year, month, weekday, n):
    """n-th occurrence of  (Mon=0) in /, e.g. 3rd Monday of January."""
    d = date(year, month, 1)
    d += timedelta(days=(weekday - d.weekday()) % 7 + 7 * (n - 1))
    return d


def _last_weekday(year, month, weekday):
    """Last occurrence of  in /, e.g. last Monday of May."""
    next_month = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    d = next_month - timedelta(days=1)
    return d - timedelta(days=(d.weekday() - weekday) % 7)


def _easter(year):
    """Gregorian Easter Sunday (anonymous/Meeus algorithm) -- NYSE closes the Good Friday
    before it, and Good Friday is the one NYSE holiday not on a fixed weekday-of-month rule."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = (h + l - 7 * m + 114) % 31 + 1
    return date(year, month, day)


def _observed(d):
    """NYSE shifts a holiday landing on a weekend to the adjacent weekday: Saturday ->
    preceding Friday, Sunday -> following Monday (e.g. July 4th 2026 falls on a Saturday,
    so NYSE is closed Friday July 3rd instead)."""
    if d.weekday() == 5:
        return d - timedelta(days=1)
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


def nyse_holidays(year):
    """Full-close NYSE holidays for a given year (early-close days like the day after
    Thanksgiving are NOT included here -- those still have a real, if short, trading session)."""
    return {
        _observed(date(year, 1, 1)),      # New Year's Day
        _nth_weekday(year, 1, 0, 3),      # MLK Day (3rd Monday of Jan)
        _nth_weekday(year, 2, 0, 3),      # Presidents Day (3rd Monday of Feb)
        _easter(year) - timedelta(days=2),  # Good Friday
        _last_weekday(year, 5, 0),        # Memorial Day (last Monday of May)
        _observed(date(year, 6, 19)),     # Juneteenth
        _observed(date(year, 7, 4)),      # Independence Day
        _nth_weekday(year, 9, 0, 1),      # Labor Day (1st Monday of Sep)
        _nth_weekday(year, 11, 3, 4),     # Thanksgiving (4th Thursday of Nov)
        _observed(date(year, 12, 25)),    # Christmas
    }


def in_market_hours():
    """13:00-21:00 UTC weekdays, excluding full-close NYSE holidays -- matches the live
    scanner cron window. Checked before EVERY individual config (not just between
    --budget-configs batches): at the observed ~6min/config pace a 50-config batch can run
    5+ hours, so a per-batch-only check could let a run that started just before the open
    plow straight through the trading day. Holiday-awareness added 2026-07-03 (heff's
    direction, after this exact naive weekday/hour check would have paused a run mid-batch
    on July 3rd 2026 -- a real NYSE holiday, since July 4th fell on a Saturday -- for no
    reason, since nothing was actually trading that day to avoid contention with). UTC date
    is used directly (not converted to America/New_York) since 13:00-21:00 UTC never crosses
    a US-Eastern midnight boundary, so the calendar date is the same in both zones here."""
    now = datetime.now(timezone.utc)
    if now.weekday() > 4:
        return False
    if now.date() in nyse_holidays(now.year):
        return False
    return 13 <= now.hour < 21


def fmt(s):
    if s is None:
        return "n/a"
    c = f" corr {s['corr']:+.2f}" if s.get("corr") is not None else ""
    return (f"{s['total_r']:+7.1f}R | {s['n']:>4} tr | {s['per_trade']:+.3f}R/tr | "
            f"Sharpe {s['sharpe']:.2f}{c}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--budget-configs", type=int, default=25)
    a = ap.parse_args()

    syms = wide_universe.load_universe(rebuild_if_stale=False) or mr.fetch_sp100()
    cached = load_cached(syms)
    if not cached:
        raise SystemExit("no cached symbols; run walkforward_search.py --build-cache first")

    champ = load_champion()
    mr_leg, orb_leg = champ["mr"], champ["orb"]

    # establish the date split + champion baseline on search/holdout once
    mr_t = generate_component(mr_leg, cached)
    orb_t = generate_component(orb_leg, cached)
    comp_trades = {component_key(mr_leg): mr_t, component_key(orb_leg): orb_t}
    all_dates = mr_t["date"].tolist() + orb_t["date"].tolist()
    search, holdout = date_split(all_dates)
    base_keys = [component_key(mr_leg), component_key(orb_leg)]
    base_s = score_portfolio(base_keys, comp_trades, search)
    base_h = score_portfolio(base_keys, comp_trades, holdout)

    grid = build_grid()
    tried = load_tried()

    batch = []
    for leg, c in grid:
        h = hashlib.md5(component_key(c).encode()).hexdigest()[:12]
        if h in tried:
            continue
        batch.append((leg, c, h))
        if len(batch) >= a.budget_configs:
            break

    if not batch:
        msg = (f"continuous search: grid exhausted ({len(grid)} configs, all tried). "
               f"Champion unchanged: mr={mr_leg['name']} orb={orb_leg['name']}\n"
               f"search {fmt(base_s)}\nholdout {fmt(base_h)}")
        log.info(msg)
        tg(msg)
        return

    log.info(f"testing {len(batch)} new configs ({len(tried)} already tried, "
             f"{len(grid) - len(tried) - len(batch)} remaining after this run)")

    ledger_rows = []
    promoted = []
    results_lines = []
    stopped_early = False
    for leg, c, h in batch:
        if in_market_hours():
            log.info(f"market hours reached mid-batch -- stopping early after {len(ledger_rows)}/{len(batch)} "
                     f"configs this run (the rest stay untried, picked up next off-hours run)")
            stopped_early = True
            break
        t = generate_component(c, cached)
        comp_trades[component_key(c)] = t
        if leg == "mr":
            keys = [component_key(c), component_key(orb_leg)]
        else:
            keys = [component_key(mr_leg), component_key(c)]
        # Ranked on PER-TRADE R, not total R (changed 2026-07-03, heff's direction). A
        # 2026-07-02 batch promoted orb_vm1.25_mrNone_TrueTrue (no tight-range, 21205 trades,
        # +0.143R/tr) over an almost-identical tight-range sibling in the SAME batch
        # (orb_vm1.25_mr0.0066_TrueTrue, +0.255R/tr, 10686 trades) purely because higher
        # volume gave it more total R -- the same total-R blind spot that once nearly buried
        # tight-range ORB itself (see live_params.json's promotion note). MIN_TRADES below
        # still guards against a thin/lucky sample winning on quality alone.
        s = score_portfolio(keys, comp_trades, search)
        if s["n"] < MIN_TRADES or s["per_trade"] <= 0:
            decision = "dead"
        elif s["per_trade"] > base_s["per_trade"]:
            decision = "search-win"
        else:
            decision = "no-improve"

        h_s = None
        if decision == "search-win":
            h_s = score_portfolio(keys, comp_trades, holdout)
            d_search = s["per_trade"] - base_s["per_trade"]
            d_hold = h_s["per_trade"] - base_h["per_trade"]
            carried = d_search > 0 and d_hold >= 0.5 * d_search
            decision = "CARRIED" if carried else "mirage"
            if carried:
                if leg == "mr":
                    mr_leg = c
                else:
                    orb_leg = c
                base_s, base_h = s, h_s
                promoted.append((leg, c["name"], s, h_s))

        ledger_rows.append(dict(hash=h, leg=leg, name=c["name"], search_total_r=s["total_r"],
                                 search_n=s["n"], search_per_trade=s["per_trade"],
                                 search_sharpe=s["sharpe"], decision=decision))
        results_lines.append(f"  [{leg}] {c['name']:<32} {fmt(s)} -> {decision}")

    if not ledger_rows:
        msg = "continuous search: market hours hit before any config in this batch could start -- nothing tested, will resume next off-hours run."
        log.info(msg)
        tg(msg)
        return

    append_ledger(ledger_rows)
    save_champion({"mr": mr_leg, "orb": orb_leg, "search": base_s, "holdout": base_h})

    n_carried = sum(1 for r in ledger_rows if r["decision"] == "CARRIED")
    n_mirage = sum(1 for r in ledger_rows if r["decision"] == "mirage")
    n_dead = sum(1 for r in ledger_rows if r["decision"] == "dead")
    n_noimp = sum(1 for r in ledger_rows if r["decision"] == "no-improve")
    remaining = len(grid) - len(tried) - len(ledger_rows)

    early_note = " (stopped early -- market hours)" if stopped_early else ""
    header = (f"continuous search: {len(ledger_rows)} new configs tested{early_note} "
              f"({n_carried} carried, {n_mirage} mirage, {n_noimp} no-improve, {n_dead} dead). "
              f"{remaining} configs left in grid.")
    log.info(header)
    for line in results_lines:
        log.info(line)

    if promoted:
        promo_lines = "\n".join(
            f"NEW {leg.upper()} LEG: {name}\n   search {fmt(s)}\n   holdout {fmt(h_s)}"
            for leg, name, s, h_s in promoted)
        msg = f"{header}\n\n{promo_lines}\n\nrunning champion now: mr={mr_leg['name']} orb={orb_leg['name']}"
    else:
        msg = (f"{header}\nrunning champion unchanged: mr={mr_leg['name']} orb={orb_leg['name']}\n"
               f"search {fmt(base_s)}\nholdout {fmt(base_h)}")
    tg(msg)


if __name__ == "__main__":
    main()
