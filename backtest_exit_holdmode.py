#!/usr/bin/env python3
"""backtest_exit_holdmode.py -- modern rebuild of the old backtest_exit_modes.py idea
(EOD exit vs. hold-across-days-until-TP/SL), 2026-07-08. The old script predates the
2026-06-13 Alpaca migration -- it pulls yfinance directly and self-admits "~60 days,
small sample, directional only" in its own docstring. This version reuses the full
2-year Databento-cached wide_universe + locked 75/25 holdout, same rigor as every other
strategy tested tonight.

METHOD: gen_mean_rev/gen_orb's entry-detection state machine is copied VERBATIM (not
reimplemented) from walkforward_search.py -- the only change is what forward price
window gets handed to sim_forward(). The live/EOD version slices H/L/C from the
CURRENT DAY's own bars only (dd, from df.groupby(df.index.date)); the hold-mode
version instead slices from the FULL underlying df starting at the trigger's exact
timestamp, extended up to MAX_HOLD_BARS (capped, not unbounded -- an uncapped hold
isn't operationally realistic for a manual-confirm strategy, and sim_forward's own
fallback behavior at the cap boundary -- exit at whatever price is there -- models a
forced close at the hold limit, not a magic resolution).

KNOWN RISK being tested (not hidden): holding through an overnight gap can lose MORE
than 1R if the gap jumps past the stop -- sim_forward doesn't model gap-through-stop
explicitly, it just checks each bar's H/L against the stop/target in sequence, so a
bar that opens beyond the stop will still register as a stop-out at the stop's own R
level (i.e., this UNDERSTATES real overnight gap risk, a conservative measurement bias
worth flagging plainly in the read-out, not silently).
"""
import time
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from walkforward_search import (
    load_cached, sim_forward, MR_CAP, ORB_CAP, COST_BPS, MIN_RISK_FRAC,
    wide_universe, date_split,
)
import mean_reversion_scanner as mr

ROOT = Path("/home/heff/.openclaw/workspace")
OUT_LEDGER = ROOT / "data" / "exit_holdmode_wf_ledger.csv"
OUT_RESULT = ROOT / "data" / "exit_holdmode_wf_result.json"

BARS_PER_DAY = 78  # ~6.5h RTH / 5min


def gen_mean_rev_eod_and_hold(df, p, max_hold_bars):
    """Runs the mean-rev entry state machine ONCE, emits BOTH an EOD-exit outcome and a
    hold-mode outcome per trigger (same entry, two different exit simulations) so the
    comparison is apples-to-apples on the identical trigger set, not two separate runs
    that might trigger on slightly different bars due to incidental fp differences."""
    z_t = p["z"]; rsi_os = p["rsi_os"]; rsi_ob = p["rsi_ob"]; vdev = p["vdev"]
    min_rr = p["min_rr"]; buf = p["stop_buf"]; max_price = p["max_price"]; max_watch = p["max_watch"]

    full_H = df["High"].to_numpy(); full_L = df["Low"].to_numpy(); full_C = df["Close"].to_numpy()
    pos_lookup = {ts: k for k, ts in enumerate(df.index)}

    eod_rows, hold_rows = [], []

    for day, dd in df.groupby(df.index.date, sort=True):
        dd = dd.dropna(subset=["sma20", "std20", "rsi", "vwap"])
        if dd.empty:
            continue
        H = dd["High"].to_numpy(); L = dd["Low"].to_numpy(); C = dd["Close"].to_numpy()
        idx = dd.index
        sampled = [i for i, ts in enumerate(idx) if ts.minute % 15 == 0]
        state = {}
        for i in sampled:
            r = dd.iloc[i]
            close = float(r["Close"]); high = float(r["High"]); low = float(r["Low"])
            sma = float(r["sma20"]); std = float(r["std20"]); rs = float(r["rsi"])
            zz = float(r["z"]); vw = float(r["vwap"]); dev = float(r["vwap_dev"])
            if any(np.isnan(v) for v in (close, sma, std, rs, zz, vw, dev)):
                continue
            upper = sma + z_t * std
            lower = sma - z_t * std
            setup_long = (zz <= -z_t and close < lower and rs < rsi_os and dev <= -vdev)
            setup_short = (zz >= z_t and close > upper and rs > rsi_ob and dev >= vdev)
            for side, setup, key in (("SHORT", setup_short, "SHORT"), ("LONG", setup_long, "LONG")):
                st = state.get(key)
                if st is None:
                    if setup and close <= max_price:
                        state[key] = {"stage": "watch", "sl": low, "sh": high,
                                      "eh": high, "el": low, "ck": 0}
                    continue
                if st["stage"] != "watch":
                    continue
                if side == "SHORT":
                    st["eh"] = max(st["eh"], high)
                    if close > upper:
                        st["sl"] = low; st["ck"] += 1
                    elif low < st["sl"]:
                        entry = st["sl"]; stop = st["eh"] * (1 + buf)
                        risk = stop - entry
                        rr = (entry - vw) / risk if risk > 0 else 0
                        if rr >= min_rr and entry > 0 and risk / entry >= MIN_RISK_FRAC:
                            st["stage"] = "triggered"
                            eod_out = sim_forward("SHORT", entry, stop, vw, H[i + 1:], L[i + 1:], C[i + 1:])
                            if eod_out is not None:
                                eod_rows.append((str(day), "SHORT", eod_out, risk / entry))
                            fp = pos_lookup.get(idx[i])
                            if fp is not None:
                                fh = full_H[fp + 1: fp + 1 + max_hold_bars]
                                fl = full_L[fp + 1: fp + 1 + max_hold_bars]
                                fc = full_C[fp + 1: fp + 1 + max_hold_bars]
                                hold_out = sim_forward("SHORT", entry, stop, vw, fh, fl, fc)
                                if hold_out is not None:
                                    hold_rows.append((str(day), "SHORT", hold_out, risk / entry))
                        else:
                            st["stage"] = "expired"
                    else:
                        st["ck"] += 1
                else:
                    st["el"] = min(st["el"], low)
                    if close < lower:
                        st["sh"] = high; st["ck"] += 1
                    elif high > st["sh"]:
                        entry = st["sh"]; stop = st["el"] * (1 - buf)
                        risk = entry - stop
                        rr = (vw - entry) / risk if risk > 0 else 0
                        if rr >= min_rr and entry > 0 and risk / entry >= MIN_RISK_FRAC:
                            st["stage"] = "triggered"
                            eod_out = sim_forward("LONG", entry, stop, vw, H[i + 1:], L[i + 1:], C[i + 1:])
                            if eod_out is not None:
                                eod_rows.append((str(day), "LONG", eod_out, risk / entry))
                            fp = pos_lookup.get(idx[i])
                            if fp is not None:
                                fh = full_H[fp + 1: fp + 1 + max_hold_bars]
                                fl = full_L[fp + 1: fp + 1 + max_hold_bars]
                                fc = full_C[fp + 1: fp + 1 + max_hold_bars]
                                hold_out = sim_forward("LONG", entry, stop, vw, fh, fl, fc)
                                if hold_out is not None:
                                    hold_rows.append((str(day), "LONG", hold_out, risk / entry))
                        else:
                            st["stage"] = "expired"
                    else:
                        st["ck"] += 1
                if state.get(key, {}).get("stage") == "watch" and state[key]["ck"] > max_watch:
                    state[key]["stage"] = "expired"
    return eod_rows, hold_rows


def to_frame(rows):
    out = pd.DataFrame(rows, columns=["date", "side", "r_gross", "risk_frac"])
    if not out.empty:
        out["net_r"] = out["r_gross"] - (COST_BPS / 10000.0) / out["risk_frac"].clip(lower=MIN_RISK_FRAC)
    else:
        out["net_r"] = []
    return out


def sc(df, region):
    t = df[df["date"].isin(region)]
    n = len(t)
    tot = float(t["net_r"].sum())
    per = tot / n if n else 0.0
    d = t.groupby("date")["net_r"].sum()
    sh = float(d.mean() / d.std() * np.sqrt(252)) if len(d) > 1 and d.std() > 0 else 0.0
    return dict(total_r=tot, n=n, per_trade=per, sharpe=sh)


def concentration(df, region, topn=10):
    t = df[df["date"].isin(region)]
    if t.empty:
        return {"n": 0}
    t = t.sort_values("net_r", ascending=False)
    total = t["net_r"].sum()
    topn = min(topn, len(t))
    top_sum = t.head(topn)["net_r"].sum()
    return {
        "win_rate": round(float((t["net_r"] > 0).mean()), 3),
        "median_r": round(float(t["net_r"].median()), 3),
        "mean_r": round(float(t["net_r"].mean()), 3),
        "top10_pct_of_profit": (round(float(top_sum / total * 100), 1) if total != 0 else None),
    }


def fmt(s):
    return "{:+7.1f}R  n={:<5} {:+.4f}R/tr  Sharpe={:+.2f}".format(
        s["total_r"], s["n"], s["per_trade"], s["sharpe"])


def verdict(base_h, hold_h, hold_diag):
    if hold_h["n"] < 30:
        return "INSUFFICIENT SAMPLE (n={})".format(hold_h["n"])
    if hold_h["per_trade"] <= base_h["per_trade"]:
        return "NO IMPROVEMENT -- hold-mode does not beat EOD on holdout ({:+.4f} vs {:+.4f}R/tr)".format(
            hold_h["per_trade"], base_h["per_trade"])
    if hold_diag.get("top10_pct_of_profit") and hold_diag["top10_pct_of_profit"] > 60:
        return "SUSPECT -- {:.0f}% of hold-mode profit from top 10 trades".format(hold_diag["top10_pct_of_profit"])
    return "HOLD-MODE LOOKS REAL -- beats EOD on holdout, not outlier-concentrated (but see the overnight-gap measurement-bias caveat in the module docstring)"


def main():
    syms = wide_universe.load_universe(rebuild_if_stale=False) or mr.fetch_sp100()
    print("universe: {} symbols".format(len(syms)))
    cached = load_cached(syms)
    print("cached: {}/{} symbols".format(len(cached), len(syms)))

    HOLD_VARIANTS = [("hold_2day", 2 * BARS_PER_DAY), ("hold_5day", 5 * BARS_PER_DAY)]

    t0 = time.time()
    eod_rows_all = []
    hold_rows_by_variant = {name: [] for name, _ in HOLD_VARIANTS}
    for i, (sym, df) in enumerate(cached, 1):
        for name, max_bars in HOLD_VARIANTS:
            eod_rows, hold_rows = gen_mean_rev_eod_and_hold(df, MR_CAP["p"], max_bars)
            if name == HOLD_VARIANTS[0][0]:
                eod_rows_all += eod_rows  # EOD baseline is identical regardless of hold variant; only collect once
            hold_rows_by_variant[name] += hold_rows
        if i % 30 == 0:
            print("  {}/{} symbols processed ({:.0f}s)".format(i, len(cached), time.time() - t0))

    eod_df = to_frame(eod_rows_all)
    print("\nEOD baseline: {} trades".format(len(eod_df)))
    all_dates = eod_df["date"].tolist()
    for name, _ in HOLD_VARIANTS:
        all_dates += to_frame(hold_rows_by_variant[name])["date"].tolist()
    search, holdout = date_split(all_dates)
    print("date split: {} search / {} holdout days".format(len(search), len(holdout)))

    base_s, base_h = sc(eod_df, search), sc(eod_df, holdout)
    print("\nEOD (current live behavior): search {} | holdout {}".format(fmt(base_s), fmt(base_h)))

    led = [dict(name="EOD baseline (current live behavior)", search_total_r=base_s["total_r"],
                search_n=base_s["n"], search_per_trade=base_s["per_trade"], search_sharpe=base_s["sharpe"],
                holdout_total_r=base_h["total_r"], holdout_n=base_h["n"],
                holdout_per_trade=base_h["per_trade"], holdout_sharpe=base_h["sharpe"],
                verdict="baseline")]

    for name, max_bars in HOLD_VARIANTS:
        hdf = to_frame(hold_rows_by_variant[name])
        hs, hh = sc(hdf, search), sc(hdf, holdout)
        diag = concentration(hdf, holdout)
        v = verdict(base_h, hh, diag)
        print("\n{} ({} bars ~{}d cap): search {} | holdout {}".format(
            name, max_bars, max_bars // BARS_PER_DAY, fmt(hs), fmt(hh)))
        print("  verdict: {}".format(v))
        print("  diagnostics: {}".format(diag))
        led.append(dict(name=name, search_total_r=hs["total_r"], search_n=hs["n"],
                        search_per_trade=hs["per_trade"], search_sharpe=hs["sharpe"],
                        holdout_total_r=hh["total_r"], holdout_n=hh["n"],
                        holdout_per_trade=hh["per_trade"], holdout_sharpe=hh["sharpe"],
                        verdict=v, **diag))

    pd.DataFrame(led).to_csv(OUT_LEDGER, index=False)
    OUT_RESULT.write_text(__import__("json").dumps(led, indent=2, default=str))
    print("\nwrote {}\nwrote {}".format(OUT_LEDGER, OUT_RESULT))


if __name__ == "__main__":
    main()
