# GREEKS BACKTEST SPEC v1 (2026-06-24)

Greeks-based backtests over the Databento OPRA parquet pull. Greeks are **computed**
(Black-Scholes), not bought — so daily OHLCV + definitions + open interest is sufficient.
Mapped to "Single-Stock Options Quant Research" §1 (vol/skew) and §2 (gamma/vanna/charm).

## DATA FOUNDATION
Per name, in `data/options/`:
- `{SYM}_defs.parquet`  — raw_symbol ↔ strike, expiration, instrument_class (C/P)
- `{SYM}_ohlcv1d.parquet` — daily OHLC + volume per contract (close = LAST TRADE, not mid)
- `{SYM}_stats.parquet` — daily statistics incl. **open interest** per contract (for GEX)
- underlying daily bars — FREE via Alpaca (spot S)

**Compute layer — `greeks.py`** (per contract per day):
- mid proxy = close; **restrict to NTM monthly + drop zero-volume / zero-OI** (last-trade is noisy
  on illiquid strikes — this is the honest mitigation for not having NBBO mid).
- solve implied vol σ from BS given S, K, T, r (constant 3m T-bill proxy), call/put.
- analytic Greeks: delta, gamma, vega, theta, **vanna (∂Δ/∂σ), charm (∂Δ/∂t)**.
- per-name daily IV surface: ATM IV, term structure (front vs back), 25Δ skew (put IV − call IV).

---

## BACKTEST 1 — GEX REGIME FILTER (Gamma)  ·  doc §2  ·  HIGHEST LEVERAGE
Overlay on the **already-validated** MR/ORB equity edges — not a new standalone edge.

- **Net GEX** per name/day = Σ_strikes [ gamma_i × OI_i × 100 × S² × sign_i ], standard dealer-sign
  convention (calls +, puts −). Document the assumption + the doc's **T-1 OI lag** caveat explicitly.
- **Gamma-flip** = spot level where cumulative GEX crosses zero. Regime = NEG-gamma (below flip) vs
  POS-gamma (above).
- **Hypothesis (from doc):** NEG-gamma → pro-cyclical hedging → momentum days → **ORB breakouts** do
  better; POS-gamma → pinning/range → **mean-reversion** does better.
- **Test (matches your method):** on the **LOCKED 75/25 holdout**, re-run existing MR & ORB stats
  *conditioned on regime*. Metric = does regime-gating lift R/trade + Sharpe vs unconditional?
  **KILL if it doesn't carry** — same bar that killed risk-off / regime-ORB / VIX.
- Validate as an **equity-signal filter first** (cheap, plugs into `walkforward_search.py`); only if it
  carries do we express it in options.

## BACKTEST 2 — VARIANCE / VOL RISK PREMIUM: delta-hedged straddle (Vega+Gamma)  ·  doc §1
- **Signal:** VRP = ATM_IV(t) − realized_vol(t→expiry), per name/day.
- **Construct:** at ~30-DTE monthly, short ATM straddle, delta-hedge daily via computed delta.
  **$1000-tradeable form = iron fly** (short ATM straddle + long wings at ±1×EM) → defined risk.
- **P&L attribution:** theta − gamma (realized variance) ± vega. **R = max loss** (fly width − credit).
  Report mean R, Sharpe, hit-rate per name + pooled.
- **Cross-section:** rank names by VRP; short-vol top-quantile vs long-vol bottom (doc §1 "high
  ex-ante earnings risk premium" selection).

## BACKTEST 3 — SKEW + TERM STRUCTURE (Tier 2, same compute)  ·  doc §1
- **Skew:** risk-reversal / put-spread conditioned on 25Δ-skew z-score (skewness risk premium harvest).
- **Term:** calendar (front vs back IV) when term structure inverts into an event.

---

## METHOD DISCIPLINE (your rules)
- **LOCKED 75/25 holdout; KILL mirages.** Forward paper tournament stays the live proving ground.
- **Cost model:** no NBBO → apply a conservative **fill-vs-mid haircut by moneyness/liquidity bucket**,
  calibrated to the live indicative spreads from `options_probe` (liquid ATM ~tight, OTM/illiquid wide).
- Daily close = last trade → NTM-monthly only, drop zero-vol/zero-OI contracts.

## BUILD ORDER
1. `greeks.py` — IV solve + Greeks + per-name IV surface; unit-test vs a known BS reference value.
2. `gex.py` — net GEX, flip level, regime tag (needs `{SYM}_stats` OI).
3. `backtest_greeks.py` — BT1 regime overlay on existing MR/ORB; BT2 VRP straddle; over parquet.
4. Fold survivors into the tournament slate + `night_report.py` leaderboard.

---

## PLAYBOOK ALIGNMENT (2026-06-25) — BT1 locked to THE GAMMA PLAYBOOK
heff's `GAMMA_PLAYBOOK.md` (+ source PDF `docs/The_Gamma_Playbook.pdf`) is the canonical framework
for the GEX work. BT1 above is now implemented in **`gex.py`** to match it exactly:
- **Formula:** `Σ gamma × OI × 100 × S² × 0.01`, dealer sign **calls +, puts −** (the playbook's
  ×0.01 per-1%-move factor is now included — earlier BT1 line omitted it).
- **Flip:** spot level where net GEX(S) crosses zero, recomputed on a ±15% spot grid (sticky-strike,
  solved IV held fixed); linear-interp the zero nearest spot.
- **Walls:** call wall = largest gamma×OI strike above spot; put wall = largest below.
- **Regime → strategy:** POSITIVE = suppressive → favor **mean-reversion**; NEGATIVE = amplifying →
  favor **ORB/momentum** (== the BT1 hypothesis; this is what the holdout test must confirm/KILL).
- **Honesty (playbook §4/§6, enforced):** every row reports `n_oi`/`m_contracts`/`coverage`;
  thin chain (<6 OI strikes) → regime `none` ("no clean read"); greeks tagged `BS_modeled`;
  **T-1 OI lag** = no-lookahead (predict day t from OI known at t's open). Verify flip/walls vs a free
  reference (FlashAlpha/GEXStream) before any LIVE use.
