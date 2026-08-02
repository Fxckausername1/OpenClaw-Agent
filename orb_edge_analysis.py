#!/usr/bin/env python3
"""orb_edge_analysis.py — where is the ORB edge, and what's the best return vs R?

Mirrors the validated gen_orb detection + reuses wf.sim_forward on the 2yr cache. For
every ORB breakout it records side, range% (= risk as % of price), relvol, entry time, and
the realized R at EOD PLUS at fixed R-targets (1/1.5/2/2.5/3R). Then, net of 6bp cost:

  1. P(reach T-R): the probability an ORB trade tags each R-multiple intraday — the literal
     "greatest probability of best return vs R" read.
  2. CORRELATION of EOD net R with range% / relvol / time-of-day / side.
  3. WIN% + expectancy bucketed by each feature on the SEARCH region; the single best
     slice is then CONFIRMED on the locked HOLDOUT (so the 'edge' isn't a mined mirage).
  4. TARGET sweep: does exiting at a fixed R-target beat the current EOD exit? Best T found
     on search, confirmed on holdout.

Run after close. ./venv/bin/python orb_edge_analysis.py
"""
import sys
import subprocess

import numpy as np
import pandas as pd

import walkforward_search as wf

TARGETS = [1.0, 1.5, 2.0, 2.5, 3.0]
COST = wf.COST_BPS / 10000.0
TG_TARGET = "7590346809"


def tg(msg):
    try:
        subprocess.run(["/usr/bin/openclaw", "message", "send", "--channel", "telegram",
                        "--target", TG_TARGET, "--message", msg], timeout=40, check=False)
    except Exception:
        pass


def collect():
    p = wf.comp_orb("o")["p"]
    or_end = p["or_end"]; vol_mult = p["vol_mult"]; use_vwap = p["use_vwap"]
    use_vol = p["use_vol"]; max_price = p["max_price"]
    rows = []
    for sym, df in wf.load_cached(wf.mr.fetch_sp100()):
        for day, dd in df.groupby(df.index.date, sort=True):
            times = np.array([t.time() for t in dd.index])
            orb = dd[times < or_end]; post = dd[times >= or_end]
            if len(orb) < 1 or len(post) < 2:
                continue
            orh = float(orb["High"].max()); orl = float(orb["Low"].min()); rng = orh - orl
            if rng <= 0 or orh > max_price:
                continue
            pH = post["High"].to_numpy(); pL = post["Low"].to_numpy(); pC = post["Close"].to_numpy()
            pO = post["Open"].to_numpy(); pV = post["vwap"].to_numpy(); pA = post["avgvol"].to_numpy()
            pVol = post["Volume"].to_numpy(); pidx = post.index
            for j in range(len(post)):
                up = pH[j] >= orh; dn = pL[j] <= orl
                if not (up or dn):
                    continue
                side = ("LONG" if pC[j] >= pO[j] else "SHORT") if (up and dn) else ("LONG" if up else "SHORT")
                entry = orh if side == "LONG" else orl
                stop = orl if side == "LONG" else orh
                if entry <= 0 or rng / entry < wf.MIN_RISK_FRAC:
                    break
                relvol = pVol[j] / pA[j] if pA[j] > 0 else 0
                take_vwap = (pC[j] > pV[j]) if side == "LONG" else (pC[j] < pV[j])
                if use_vwap and not take_vwap:
                    break
                if use_vol and relvol < vol_mult:
                    break
                r_eod = wf.sim_forward(side, entry, stop, None, pH[j:], pL[j:], pC[j:])
                if r_eod is None:
                    break
                rec = dict(date=str(day), side=side, rf=rng / entry, relvol=relvol,
                           emin=pidx[j].hour * 60 + pidx[j].minute, r_eod=r_eod)
                for T in TARGETS:
                    tgt = entry + T * rng if side == "LONG" else entry - T * rng
                    rec[f"g_{T}"] = wf.sim_forward(side, entry, stop, tgt, pH[j:], pL[j:], pC[j:])
                rows.append(rec)
                break
    t = pd.DataFrame(rows)
    cpr = COST / t["rf"].clip(lower=wf.MIN_RISK_FRAC)        # round-trip cost in R units
    t["r_eod_net"] = t["r_eod"] - cpr
    for T in TARGETS:
        t[f"net_{T}"] = t[f"g_{T}"] - cpr
        t[f"hit_{T}"] = t[f"g_{T}"] >= (T - 1e-6)            # tagged the T-R target intraday
    return t


def wr(s):
    return (s > 0).mean() * 100 if len(s) else 0.0


def main():
    t = collect()
    search, holdout = wf.date_split(t["date"].tolist())
    ts = t[t["date"].isin(search)]; th = t[t["date"].isin(holdout)]
    L = []
    L.append(f"📊 ORB edge analysis — {len(t)} trades ({len(ts)} search / {len(th)} holdout), net 6bp")
    L.append(f"EOD baseline: SEARCH {wr(ts['r_eod_net']):.0f}% win / {ts['r_eod_net'].mean():+.3f}R | "
             f"HOLDOUT {wr(th['r_eod_net']):.0f}% / {th['r_eod_net'].mean():+.3f}R")

    L.append("\n— P(reach T·R) intraday (full sample) + expectancy if exited there —")
    for T in TARGETS:
        L.append(f"  {T:>3}R: P(hit)={t[f'hit_{T}'].mean()*100:4.1f}%  "
                 f"exit-at-{T}R net exp SEARCH {ts[f'net_{T}'].mean():+.3f}R / HOLDOUT {th[f'net_{T}'].mean():+.3f}R")

    L.append("\n— correlation of EOD net R with setup features (search) —")
    for col, name in [("rf", "range%(risk)"), ("relvol", "relvol"), ("emin", "time-of-day")]:
        c = np.corrcoef(ts[col], ts["r_eod_net"])[0, 1]
        L.append(f"  {name:<14} r={c:+.3f}")
    L.append(f"  side: LONG {wr(ts[ts.side=='LONG']['r_eod_net']):.0f}%/{ts[ts.side=='LONG']['r_eod_net'].mean():+.3f}R "
             f"| SHORT {wr(ts[ts.side=='SHORT']['r_eod_net']):.0f}%/{ts[ts.side=='SHORT']['r_eod_net'].mean():+.3f}R")

    # bucketed win%/exp on SEARCH; find the best slice, confirm on HOLDOUT
    L.append("\n— EOD expectancy by feature bucket (SEARCH) —")
    slices = {}
    # side
    for s in ("LONG", "SHORT"):
        slices[f"side={s}"] = (ts.side == s, th.side == s)
    # range% terciles
    qs = ts["rf"].quantile([1/3, 2/3]).values
    slices["range%≤t1"] = (ts.rf <= qs[0], th.rf <= qs[0])
    slices["range% mid"] = ((ts.rf > qs[0]) & (ts.rf <= qs[1]), (th.rf > qs[0]) & (th.rf <= qs[1]))
    slices["range%>t2"] = (ts.rf > qs[1], th.rf > qs[1])
    # relvol buckets
    slices["relvol≥2"] = (ts.relvol >= 2, th.relvol >= 2)
    slices["relvol≥3"] = (ts.relvol >= 3, th.relvol >= 3)
    # time windows
    slices["09:45-11:00"] = ((ts.emin >= 585) & (ts.emin < 660), (th.emin >= 585) & (th.emin < 660))
    slices["14:30-16:00"] = (ts.emin >= 870, th.emin >= 870)
    best = None
    for name, (ms, mh) in slices.items():
        n = int(ms.sum()); exp = ts.loc[ms, "r_eod_net"].mean() if n else 0
        L.append(f"  {name:<14} {n:>5} tr | {wr(ts.loc[ms,'r_eod_net']):3.0f}% | {exp:+.3f}R")
        if n >= wf.MIN_TRADES and (best is None or exp > best[1]):
            best = (name, exp, mh)
    if best:
        name, exp_s, mh = best
        exp_h = th.loc[mh, "r_eod_net"].mean() if mh.sum() else 0
        carried = mh.sum() >= wf.MIN_TRADES and exp_h > th["r_eod_net"].mean()
        L.append(f"  ➤ BEST search slice: {name} ({exp_s:+.3f}R) → HOLDOUT {int(mh.sum())} tr / {exp_h:+.3f}R "
                 f"vs all-ORB {th['r_eod_net'].mean():+.3f}R → {'✅ carried' if carried else '⚠️ did NOT carry'}")

    # best fixed target vs EOD, search→holdout
    L.append("\n— best fixed R-target vs EOD exit —")
    cand = [("EOD", "r_eod_net")] + [(f"{T}R", f"net_{T}") for T in TARGETS]
    bs = max(cand, key=lambda c: ts[c[1]].mean())
    L.append(f"  best on SEARCH = {bs[0]} ({ts[bs[1]].mean():+.3f}R)  |  HOLDOUT {bs[0]} {th[bs[1]].mean():+.3f}R "
             f"vs EOD {th['r_eod_net'].mean():+.3f}R → {'✅ beats EOD' if th[bs[1]].mean() > th['r_eod_net'].mean() else '⚠️ no better than EOD'}")

    msg = "\n".join(L)
    print(msg)
    tg(msg)


if __name__ == "__main__":
    main()
