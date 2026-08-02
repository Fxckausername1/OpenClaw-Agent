#!/usr/bin/env python3
"""Verbose re-run of gen_volprofile's inner logic for one symbol, printing entry/stop/
target/MA/HVN/LVN levels for every fired signal -- to eyeball that the mechanics (not
just the aggregate R) make sense before trusting the smoke-test numbers."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import numpy as np
import pandas as pd
import walkforward_search as wf

SYM = sys.argv[1] if len(sys.argv) > 1 else "SPY"
df = wf.load_daily_symbol(SYM)
p = wf.comp_vp("dbg")["p"]

ma_fast, ma_slow = p["ma_fast"], p["ma_slow"]
close, high, low, open_ = df["Close"], df["High"], df["Low"], df["Open"]
ma_f = close.rolling(ma_fast).mean().shift(1)
ma_s = close.rolling(ma_slow).mean().shift(1)
prev_close = close.shift(1)
tr = pd.concat([(high - low), (high - close.shift(1)).abs(), (low - close.shift(1)).abs()], axis=1).max(axis=1)
atr = tr.rolling(p["atr_period"]).mean().shift(1)
body = (close - open_).abs()
body_avg = body.rolling(p["body_lookback"]).mean().shift(1)

H = high.to_numpy(); L = low.to_numpy(); C = close.to_numpy()
PC = prev_close.to_numpy(); MAF = ma_f.to_numpy(); MAS = ma_s.to_numpy()
ATR = atr.to_numpy(); BODY = body.to_numpy(); BAVG = body_avg.to_numpy()
idx = df.index
need = max(ma_slow, p["vp_lookback"], p["body_lookback"], p["atr_period"]) + 1

fired = 0
for t in range(need, len(df) - 1):
    if np.isnan(MAF[t]) or np.isnan(MAS[t]) or np.isnan(ATR[t]) or np.isnan(BAVG[t]):
        continue
    price_ref = PC[t]
    if price_ref <= 0:
        continue
    cands = []
    for which, lvl, other in (("fast", MAF[t], MAS[t]), ("slow", MAS[t], MAF[t])):
        band = p["prox_pct"] * lvl
        if abs(price_ref - lvl) <= band * p["in_play_mult"]:
            cands.append((which, lvl, band, other))
    if not cands:
        continue
    window = df.iloc[max(0, t - p["vp_lookback"]):t]
    centers, vol = wf.compute_volume_profile(window, bins=p["vp_bins"])
    hvn, lvn = wf.find_hvn_lvn(centers, vol, p["vp_sep_pct"], p["vp_smooth_sigma"], p["vp_prominence_frac"])

    for which, lvl, band, other in cands:
        touched = (L[t] <= lvl + band) and (H[t] >= lvl - band)
        if not touched:
            continue
        approach_from_below = price_ref < lvl
        pierced_above = H[t] > lvl + band
        pierced_below = L[t] < lvl - band
        closed_above = C[t] > lvl
        closed_below = C[t] < lvl
        big_body = BODY[t] >= p["body_mult"] * BAVG[t]
        side = None
        if approach_from_below:
            if pierced_above and closed_below: side = "SHORT"
            elif closed_above and big_body: side = "LONG"
        else:
            if pierced_below and closed_above: side = "LONG"
            elif closed_below and big_body: side = "SHORT"
        if side is None:
            continue

        entry = float(C[t])
        buf = p["stop_buf"] * lvl
        if side == "LONG":
            raw_stop = lvl - buf
            behind = lvn[lvn <= raw_stop]
            stop = float(behind.max()) - buf if len(behind) else raw_stop
        else:
            raw_stop = lvl + buf
            behind = lvn[lvn >= raw_stop]
            stop = float(behind.min()) + buf if len(behind) else raw_stop

        cap = entry * 1.25 if side == "LONG" else entry * 0.75
        ma_target = other if ((side == "LONG" and other > entry) or (side == "SHORT" and other < entry)) else None
        if side == "LONG":
            ahead = hvn[(hvn > entry) & (hvn <= cap)]
            hvn_target = float(ahead.min()) if len(ahead) else None
        else:
            ahead = hvn[(hvn < entry) & (hvn >= cap)]
            hvn_target = float(ahead.max()) if len(ahead) else None
        targets = [x for x in (ma_target, hvn_target) if x is not None]
        if not targets:
            print(f"{idx[t].date()} {SYM} {which.upper()}-MA {side} entry={entry:.2f} "
                  f"ma_lvl={lvl:.2f} other_ma={other:.2f} -- NO TARGET (ma_target={ma_target}, hvn_target={hvn_target}), skipped")
            break
        target = min(targets) if side == "LONG" else max(targets)
        risk = abs(entry - stop); reward = abs(target - entry)
        rr = reward / risk if risk > 0 else float("nan")
        fired += 1
        print(f"{idx[t].date()} {SYM} {which.upper()}-MA {side}: price_ref(T-1 close)={price_ref:.2f} "
              f"ma_lvl={lvl:.2f} other_ma={other:.2f} | T bar H/L/C={H[t]:.2f}/{L[t]:.2f}/{C[t]:.2f} "
              f"body={BODY[t]:.2f} vs body_avg*{p['body_mult']}={BAVG[t]*p['body_mult']:.2f}")
        print(f"    entry={entry:.2f} stop={stop:.2f} (raw={raw_stop:.2f}, lvn_used={len(behind)>0}) "
              f"target={target:.2f} (ma_target={ma_target}, hvn_target={hvn_target}) risk={risk:.2f} reward={reward:.2f} RR={rr:.2f} "
              f"min_rr_ok={rr>=p['min_rr']}")
        print(f"    hvn levels (n={len(hvn)}): {np.round(hvn,2).tolist()}")
        print(f"    lvn levels (n={len(lvn)}): {np.round(lvn,2).tolist()}")
        break
print(f"\ntotal candidate signals examined: {fired}")
