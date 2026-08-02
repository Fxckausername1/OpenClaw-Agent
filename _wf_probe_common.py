"""_wf_probe_common.py -- shared harness for tonight's 5 remaining untested-generator
probes (vwap_rev, vol_reversion, overnight_gap, microstructure, earnings_reversion).
Factored out of backtest_close_drift.py's pattern so all 5 use IDENTICAL diagnostic
discipline (concentration + quarterly-stability, the exact checks that caught
volprofile's mirage) rather than 5 copy-pasted, potentially-drifting copies.

Same locked 75/25 holdout as every other strategy validated in this codebase.
"""
import hashlib
import json
import time
import pandas as pd
import walkforward_search as wf


def fmt(s):
    corr = " corr={:+.2f}".format(s["corr"]) if s.get("corr") is not None else ""
    return "{:+8.1f}R  n={:>5}  {:+.3f}R/tr  Sharpe={:+.2f}{}".format(
        s["total_r"], s["n"], s["per_trade"], s["sharpe"], corr)


def concentration_diagnostics(comp, holdout_dates, topn=10):
    h = hashlib.md5(wf.component_key(comp).encode()).hexdigest()[:12]
    cf = wf.COMP_DIR / "{}_{}.parquet".format(comp["base"], h)
    if not cf.exists():
        return None
    t = pd.read_parquet(cf).sort_values("date")
    hold = t[t["date"].isin(holdout_dates)].sort_values("net_r", ascending=False)
    if hold.empty:
        return {"holdout_n": 0}
    hold_total = hold["net_r"].sum()
    topn = min(topn, len(hold))
    top_sum = hold.head(topn)["net_r"].sum()
    dates = sorted(t["date"].unique())
    n = len(dates)
    quarters = []
    for i in range(4):
        lo = dates[int(n * i / 4)]
        hi = dates[int(n * (i + 1) / 4) - 1] if i < 3 else dates[-1]
        sub = t[(t["date"] >= lo) & (t["date"] <= hi)]
        per = sub["net_r"].sum() / len(sub) if len(sub) else 0.0
        quarters.append(round(per, 3))
    return {
        "holdout_n": len(hold),
        "holdout_win_rate": round(float((hold["net_r"] > 0).mean()), 3),
        "holdout_median_r": round(float(hold["net_r"].median()), 3),
        "holdout_mean_r": round(float(hold["net_r"].mean()), 3),
        "top{}_pct_of_profit".format(topn): (
            round(float(top_sum / hold_total * 100), 1) if hold_total != 0 else None),
        "per_trade_by_quarter": quarters,
    }


def verdict(h, diag):
    if h["n"] < wf.MIN_TRADES:
        return "INSUFFICIENT SAMPLE ({} < {} min trades)".format(h["n"], wf.MIN_TRADES)
    if h["per_trade"] <= 0:
        return "MIRAGE -- negative expectancy on holdout ({:+.3f}R/tr)".format(h["per_trade"])
    top_key = next((k for k in (diag or {}) if k.startswith("top") and k.endswith("_pct_of_profit")), None)
    if top_key and diag.get(top_key) and diag[top_key] > 50:
        return "SUSPECT -- holdout positive ({:+.3f}R/tr) but {:.0f}% of profit from top trades".format(
            h["per_trade"], diag[top_key])
    if diag and diag.get("holdout_median_r", 0) < 0:
        return "SUSPECT -- holdout mean positive but median trade is a loser ({:+.3f}R) -- outlier-driven".format(
            diag["holdout_median_r"])
    quarters = (diag or {}).get("per_trade_by_quarter") or []
    if len(quarters) == 4 and quarters[-1] <= 0 and sum(1 for q in quarters if q > 0) <= 1:
        return "SUSPECT -- edge concentrated in old data, fading/negative in recent quarters {}".format(quarters)
    return "LOOKS REAL -- positive on holdout, not outlier-concentrated, median trade wins"


def run_backtest(strategy_label, candidates, cached, out_ledger, out_result):
    comp_trades = {}
    uniq = {}
    for cand in candidates:
        for c in cand["comps"]:
            uniq.setdefault(wf.component_key(c), c)

    t0 = time.time()
    print("generating components for {} ({} unique)...".format(strategy_label, len(uniq)))
    for i, (k, c) in enumerate(uniq.items(), 1):
        t = wf.generate_component(c, cached)
        comp_trades[k] = t
        n = len(t)
        tot = float(t["net_r"].sum()) if n else 0.0
        per = tot / n if n else 0.0
        print("  [{}/{}] {:<30} {:>5} tr | net {:+7.1f}R | {:+.3f}R/tr ({:.0f}s)".format(
            i, len(uniq), c["name"], n, tot, per, time.time() - t0))

    all_dates = []
    for t in comp_trades.values():
        all_dates += t["date"].tolist()
    if not all_dates:
        print("\nZERO trades from any candidate for {}. Not proceeding.".format(strategy_label))
        return
    search, holdout = wf.date_split(all_dates)
    print("\ndate split: {} search days / {} locked holdout days ({:.0%}/{:.0%})".format(
        len(search), len(holdout), wf.SEARCH_FRAC, 1 - wf.SEARCH_FRAC))

    keys = lambda cand: [wf.component_key(c) for c in cand["comps"]]
    results = {}
    for cand in candidates:
        s = wf.score_portfolio(keys(cand), comp_trades, search)
        h = wf.score_portfolio(keys(cand), comp_trades, holdout)
        results[cand["name"]] = (s, h)

    print("\n" + "=" * 92)
    print("SEARCH region (in-sample -- orientation only, NOT the verdict):")
    for name, (s, h) in results.items():
        print("  {:<38} {}".format(name, fmt(s)))

    print("\nLOCKED HOLDOUT (untouched until now):")
    for name, (s, h) in results.items():
        print("  {:<38} {}".format(name, fmt(h)))

    print("\nCONCENTRATION / STABILITY DIAGNOSTICS:")
    led = []
    for cand in candidates:
        s, h = results[cand["name"]]
        diag = concentration_diagnostics(cand["comps"][0], holdout)
        v = verdict(h, diag)
        print("\n  {}:".format(cand["name"]))
        print("    {}".format(v))
        if diag:
            print("    diagnostics: {}".format(json.dumps(diag)))
        diag = dict(diag or {})
        diag.pop("holdout_n", None)
        led.append(dict(name=cand["name"], verdict=v, search_total_r=s["total_r"], search_n=s["n"],
                        search_per_trade=s["per_trade"], search_sharpe=s["sharpe"],
                        holdout_total_r=h["total_r"], holdout_n=h["n"],
                        holdout_per_trade=h["per_trade"], holdout_sharpe=h["sharpe"], **diag))

    pd.DataFrame(led).to_csv(out_ledger, index=False)
    out_result.write_text(json.dumps(led, indent=2, default=str))
    print("\nwrote {}\nwrote {}".format(out_ledger, out_result))
