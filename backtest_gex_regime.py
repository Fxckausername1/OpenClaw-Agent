#!/usr/bin/env python3
"""backtest_gex_regime.py -- first-ever historical test of gating MR/ORB by REAL net_gex
regime (not a proxy), 2026-07-08. Uses the 6 names with already-paid-for daily options
history in data/options/{SYM}_gex.parquet (AMD/BAC/CSCO/F/INTC/PFE, 2025-12-01 to
2026-05-29, ~123 trading days) -- this sidesteps the usual "GEX backtest data is expensive"
constraint because this data already exists on disk from the nightly options pipeline.

Theory (and today's live observation: MR shorted KLAC/NVDA/HPE/HPQ into a GEX-confirmed
negative-gamma breakout day and got stopped on all 4):
  - MR (fade) should work BEST in POSITIVE gamma (dealers dampen/pin -> fades get help).
  - ORB (breakout) should work BEST in NEGATIVE gamma (dealers amplify -> breakouts run).
Two opposite-direction, theory-motivated gates tested against each other.

HONEST SCOPE CAVEAT: only 6 symbols x ~123 days, vs the 200-symbol/2yr universe used
everywhere else in this codebase. This is a real, non-proxy signal check, but a much
smaller sample -- treat as an early/exploratory result, not the same statistical power as
the flagship mean_rev/orb walkforward. Analysis window is restricted to the known-GEX
date range for BOTH baseline and gated variants (apples-to-apples), even though the
intraday cache itself goes back to 2024.

Same locked-holdout + concentration/quarterly diagnostics discipline as backtest_volprofile.py
and backtest_close_drift.py.
"""
import json, time
from pathlib import Path
import pandas as pd
import numpy as np
import walkforward_search as wf

ROOT = Path("/home/heff/.openclaw/workspace")
OUT_LEDGER = ROOT / "data" / "gex_regime_wf_ledger.csv"
OUT_RESULT = ROOT / "data" / "gex_regime_wf_result.json"

GEX_SYMS = ["AMD", "BAC", "CSCO", "F", "INTC", "PFE"]
COST = wf.COST_BPS / 10000.0
MRF = wf.MIN_RISK_FRAC


def load_gex_regime(sym):
    """date_str -> regime ('positive'/'negative') for one symbol, from its real
    historical daily GEX file. Returns {} if missing."""
    p = ROOT / "data" / "options" / f"{sym}_gex.parquet"
    if not p.exists():
        return {}
    g = pd.read_parquet(p)
    g["date_str"] = pd.to_datetime(g["date"]).dt.strftime("%Y-%m-%d")
    return dict(zip(g["date_str"], g["regime"]))


def to_frame(rows):
    out = pd.DataFrame(rows, columns=["date", "side", "r_gross", "risk_frac", "sym", "regime"])
    if not out.empty:
        out["net_r"] = out["r_gross"] - COST / out["risk_frac"].clip(lower=MRF)
    else:
        out["net_r"] = []
    return out


def gen_tagged(gen_fn, params, cached, gex_lookup):
    """Runs a gen_ function per-symbol (not the anonymizing batch generate_component)
    so each trade can be tagged with its own symbol + that symbol's real GEX regime
    on that trade's date."""
    rows = []
    for sym, df in cached:
        lut = gex_lookup.get(sym, {})
        pp = dict(params, _ticker=sym)
        for r in gen_fn(df, pp):
            date, side, r_gross, risk_frac = r[0], r[1], r[2], r[3]
            regime = lut.get(date)
            rows.append((date, side, r_gross, risk_frac, sym, regime))
    return to_frame(rows)


def sc(df, region, regime_filter=None):
    t = df[df["date"].isin(region)]
    if regime_filter is not None:
        t = t[t["regime"] == regime_filter]
    n = len(t)
    tot = float(t["net_r"].sum())
    per = tot / n if n else 0.0
    d = t.groupby("date")["net_r"].sum()
    sh = float(d.mean() / d.std() * np.sqrt(252)) if len(d) > 1 and d.std() > 0 else 0.0
    return dict(total_r=tot, n=n, per_trade=per, sharpe=sh, corr=None)


def concentration(df, region, regime_filter=None, topn=10):
    t = df[df["date"].isin(region)]
    if regime_filter is not None:
        t = t[t["regime"] == regime_filter]
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
    return "{:+7.1f}R  n={:<4}  {:+.4f}R/tr  Sharpe={:+.2f}".format(
        s["total_r"], s["n"], s["per_trade"], s["sharpe"])


def verdict(h, diag):
    if h["n"] < 15:  # relaxed floor given the 6-symbol scope; flagged explicitly, not hidden
        return "INSUFFICIENT SAMPLE (n={}, scope is only 6 symbols/~123 days)".format(h["n"])
    if h["per_trade"] <= 0:
        return "MIRAGE/NO-EDGE -- non-positive holdout expectancy ({:+.4f}R/tr)".format(h["per_trade"])
    if diag.get("top10_pct_of_profit") and diag["top10_pct_of_profit"] > 60:
        return "SUSPECT -- {:.0f}% of profit from top 10 trades".format(diag["top10_pct_of_profit"])
    return "REAL DIRECTION, SMALL SAMPLE -- positive holdout, not outlier-concentrated"


def main():
    print("scope: {} (only symbols with real historical GEX data)".format(GEX_SYMS))
    cached_all = wf.load_cached(GEX_SYMS)
    cached = [(s, d) for s, d in cached_all if s in GEX_SYMS]
    gex_lookup = {s: load_gex_regime(s) for s in GEX_SYMS}
    for s in GEX_SYMS:
        n = len(gex_lookup[s])
        print("  {}: {} days of real GEX history".format(s, n))
    known_dates = set()
    for lut in gex_lookup.values():
        known_dates |= set(lut.keys())
    if known_dates:
        print("union of known-GEX dates: {} days ({} -> {})".format(
            len(known_dates), min(known_dates), max(known_dates)))
    else:
        print("NO GEX DATES FOUND")

    mr_p = dict(wf.comp_mr("_")["p"])          # live champion MR params (z=1.5)
    orb_p = dict(wf.comp_orb("_")["p"])        # live champion ORB params

    t0 = time.time()
    mr_all = gen_tagged(wf.gen_mean_rev, mr_p, cached, gex_lookup)
    orb_all = gen_tagged(wf.gen_orb, orb_p, cached, gex_lookup)
    print("MR: {} raw trades | ORB: {} raw trades ({:.0f}s)".format(
        len(mr_all), len(orb_all), time.time() - t0))

    # restrict to the known-GEX window for a fair baseline-vs-gated comparison
    mr_kw = mr_all[mr_all["date"].isin(known_dates)]
    orb_kw = orb_all[orb_all["date"].isin(known_dates)]
    print("within known-GEX window: MR {} trades | ORB {} trades".format(len(mr_kw), len(orb_kw)))
    print("  MR regime tag coverage: {:.0%} | ORB regime tag coverage: {:.0%}".format(
        mr_kw["regime"].notna().mean() if len(mr_kw) else 0.0,
        orb_kw["regime"].notna().mean() if len(orb_kw) else 0.0))

    all_dates = sorted(known_dates)
    search, holdout = wf.date_split(all_dates)
    print("date split (within known-GEX window only): {} search / {} holdout days".format(
        len(search), len(holdout)))

    results = {}
    results["MR baseline (all regimes, in-window)"] = (sc(mr_kw, search), sc(mr_kw, holdout))
    results["MR gated: POSITIVE gamma only"] = (sc(mr_kw, search, "positive"), sc(mr_kw, holdout, "positive"))
    results["MR gated: NEGATIVE gamma only"] = (sc(mr_kw, search, "negative"), sc(mr_kw, holdout, "negative"))
    results["ORB baseline (all regimes, in-window)"] = (sc(orb_kw, search), sc(orb_kw, holdout))
    results["ORB gated: NEGATIVE gamma only"] = (sc(orb_kw, search, "negative"), sc(orb_kw, holdout, "negative"))
    results["ORB gated: POSITIVE gamma only"] = (sc(orb_kw, search, "positive"), sc(orb_kw, holdout, "positive"))

    print("\n" + "=" * 88)
    print("SEARCH (in-sample, orientation only):")
    for name, (s, h) in results.items():
        print("  {:<38} {}".format(name, fmt(s)))
    print("\nLOCKED HOLDOUT:")
    for name, (s, h) in results.items():
        print("  {:<38} {}".format(name, fmt(h)))

    print("\nDIAGNOSTICS + VERDICT (n<15 flagged, not hidden -- this is a 6-symbol exploratory scope):")
    led = []
    df_map = {"MR": mr_kw, "ORB": orb_kw}
    filt_map = {
        "MR baseline (all regimes, in-window)": ("MR", None),
        "MR gated: POSITIVE gamma only": ("MR", "positive"),
        "MR gated: NEGATIVE gamma only": ("MR", "negative"),
        "ORB baseline (all regimes, in-window)": ("ORB", None),
        "ORB gated: NEGATIVE gamma only": ("ORB", "negative"),
        "ORB gated: POSITIVE gamma only": ("ORB", "positive"),
    }
    for name, (s, h) in results.items():
        leg, rf = filt_map[name]
        diag = concentration(df_map[leg], holdout, rf)
        v = verdict(h, diag)
        print("\n  {}: {}".format(name, v))
        print("    diagnostics: {}".format(json.dumps(diag)))
        led.append(dict(name=name, verdict=v, search_total_r=s["total_r"], search_n=s["n"],
                        search_per_trade=s["per_trade"], search_sharpe=s["sharpe"],
                        holdout_total_r=h["total_r"], holdout_n=h["n"],
                        holdout_per_trade=h["per_trade"], holdout_sharpe=h["sharpe"], **diag))

    pd.DataFrame(led).to_csv(OUT_LEDGER, index=False)
    OUT_RESULT.write_text(json.dumps(led, indent=2, default=str))
    print("\nwrote {}\nwrote {}".format(OUT_LEDGER, OUT_RESULT))


if __name__ == "__main__":
    main()
