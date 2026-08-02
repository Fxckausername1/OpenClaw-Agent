#!/usr/bin/env python3
"""backtest_close_drift.py -- locked 75/25 walkforward validation of gen_close_drift
(the MOC-imbalance/closing-hour edge, built orthogonal-by-design to ORB/mean-rev),
never run before 2026-07-08. Mirrors backtest_volprofile.py's discipline exactly,
including the concentration/quarterly-stability diagnostics FROM THE START (volprofile's
first pass skipped these and had to be re-run once it turned out to be a top-10-trade
mirage -- applying that lesson immediately here instead of repeating the mistake).

Uses the standard 5-min intraday cache (load_cached / wide_universe), same as
mean_rev/orb -- close_drift needs intraday VWAP + bar-level swing extremes, not daily
bars, so no daily-cache build step is needed (unlike volprofile).
"""
import hashlib, json, time
from pathlib import Path
from datetime import time as dtime
import pandas as pd
import walkforward_search as wf

ROOT = Path("/home/heff/.openclaw/workspace")
OUT_LEDGER = ROOT / "data" / "close_drift_wf_ledger.csv"
OUT_RESULT = ROOT / "data" / "close_drift_wf_result.json"


def fmt(s):
    corr = f" corr={s['corr']:+.2f}" if s.get("corr") is not None else ""
    return f"{s['total_r']:+8.1f}R  n={s['n']:>5}  {s['per_trade']:+.3f}R/tr  Sharpe={s['sharpe']:+.2f}{corr}"


def concentration_diagnostics(comp, cached, holdout_dates, topn=10):
    h = hashlib.md5(wf.component_key(comp).encode()).hexdigest()[:12]
    cf = wf.COMP_DIR / f"{comp['base']}_{h}.parquet"
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
        f"top{topn}_pct_of_profit": (round(float(top_sum / hold_total * 100), 1)
                                     if hold_total != 0 else None),
        "per_trade_by_quarter": quarters,
    }


def verdict(h, diag):
    if h["n"] < wf.MIN_TRADES:
        return f"INSUFFICIENT SAMPLE ({h['n']} < {wf.MIN_TRADES} min trades)"
    if h["per_trade"] <= 0:
        return f"MIRAGE -- negative expectancy on holdout ({h['per_trade']:+.3f}R/tr)"
    if diag and diag.get("top10_pct_of_profit") and diag["top10_pct_of_profit"] > 50:
        return (f"SUSPECT -- holdout positive ({h['per_trade']:+.3f}R/tr) but "
                f"{diag['top10_pct_of_profit']:.0f}% of profit from top 10 trades")
    if diag and diag.get("holdout_median_r", 0) < 0:
        return (f"SUSPECT -- holdout mean positive but median trade is a loser "
                f"({diag['holdout_median_r']:+.3f}R) -- outlier-driven, not broad-based")
    return "LOOKS REAL -- positive on holdout, not outlier-concentrated, median trade wins"


def main():
    syms = wf.wide_universe.load_universe(rebuild_if_stale=False) or []
    print(f"universe: {len(syms)} symbols")
    t0 = time.time()
    cached = wf.load_cached(syms)
    print(f"loaded {len(cached)}/{len(syms)} intraday-cached symbols ({time.time()-t0:.0f}s)")
    if not cached:
        raise SystemExit("no cached symbols")

    CANDIDATES = [
        {"name": "close_drift: MOM @15:00 thresh0.003",  "comps": [wf.comp_cld("cld_mom_1500", mode="mom", decide_time=dtime(15, 0), thresh=0.003)]},
        {"name": "close_drift: REV @15:00 thresh0.003",  "comps": [wf.comp_cld("cld_rev_1500", mode="rev", decide_time=dtime(15, 0), thresh=0.003)]},
        {"name": "close_drift: MOM @14:30 thresh0.003",  "comps": [wf.comp_cld("cld_mom_1430", mode="mom", decide_time=dtime(14, 30), thresh=0.003)]},
        {"name": "close_drift: MOM @15:00 thresh0.005",  "comps": [wf.comp_cld("cld_mom_1500_t5", mode="mom", decide_time=dtime(15, 0), thresh=0.005)]},
        {"name": "close_drift: MOM @15:30 thresh0.003",  "comps": [wf.comp_cld("cld_mom_1530", mode="mom", decide_time=dtime(15, 30), thresh=0.003)]},
    ]

    uniq = {}
    for cand in CANDIDATES:
        for c in cand["comps"]:
            uniq.setdefault(wf.component_key(c), c)

    comp_trades = {}
    t0 = time.time()
    print("generating components...")
    for i, (k, c) in enumerate(uniq.items(), 1):
        t = wf.generate_component(c, cached)
        comp_trades[k] = t
        n = len(t)
        tot = float(t["net_r"].sum()) if n else 0.0
        per = tot / n if n else 0.0
        print(f"  [{i}/{len(uniq)}] {c['name']:<28} {n:>5} tr | net {tot:+7.1f}R | {per:+.3f}R/tr ({time.time()-t0:.0f}s)")

    all_dates = []
    for t in comp_trades.values():
        all_dates += t["date"].tolist()
    if not all_dates:
        print("\nZERO trades from any candidate. Not proceeding.")
        return
    search, holdout = wf.date_split(all_dates)
    print(f"\ndate split: {len(search)} search days / {len(holdout)} locked holdout days ({wf.SEARCH_FRAC:.0%}/{1-wf.SEARCH_FRAC:.0%})")

    keys = lambda cand: [wf.component_key(c) for c in cand["comps"]]
    results = {}
    for cand in CANDIDATES:
        s = wf.score_portfolio(keys(cand), comp_trades, search)
        h = wf.score_portfolio(keys(cand), comp_trades, holdout)
        results[cand["name"]] = (s, h)

    print("\n" + "=" * 92)
    print("SEARCH region (in-sample -- orientation only, NOT the verdict):")
    for name, (s, h) in results.items():
        print(f"  {name:<38} {fmt(s)}")

    print("\nLOCKED HOLDOUT (untouched until now):")
    for name, (s, h) in results.items():
        print(f"  {name:<38} {fmt(h)}")

    print("\nCONCENTRATION / STABILITY DIAGNOSTICS (applied from the start, per the volprofile lesson):")
    led = []
    for cand in CANDIDATES:
        s, h = results[cand["name"]]
        diag = concentration_diagnostics(cand["comps"][0], cached, holdout)
        v = verdict(h, diag)
        print(f"\n  {cand['name']}:")
        print(f"    {v}")
        if diag:
            print(f"    diagnostics: {json.dumps(diag)}")
        diag = diag or {}
        diag.pop("holdout_n", None)
        led.append(dict(name=cand["name"], verdict=v, search_total_r=s["total_r"], search_n=s["n"],
                        search_per_trade=s["per_trade"], search_sharpe=s["sharpe"],
                        holdout_total_r=h["total_r"], holdout_n=h["n"],
                        holdout_per_trade=h["per_trade"], holdout_sharpe=h["sharpe"],
                        **diag))

    pd.DataFrame(led).to_csv(OUT_LEDGER, index=False)
    OUT_RESULT.write_text(json.dumps(led, indent=2, default=str))
    print(f"\nwrote {OUT_LEDGER}\nwrote {OUT_RESULT}")


if __name__ == "__main__":
    main()
