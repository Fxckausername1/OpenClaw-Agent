# OPTIONS TOURNAMENT — STRATEGY SPEC v1 (2026-06-24)

Daily forward **paper** tournament of 10 options strategies on Alpaca. Rank by realized
**R** and **P&L** as the sample grows; surface the top performer. Reuses the autonomous
equity infra (`alpaca_executor` / `alpaca_recon` / `portfolio_gate` / `guardrails` /
`night_report`).

## ACCESS — VERIFIED 2026-06-24 (`options_probe.py`)
- Paper account **options level 3** → all spreads / condors / calendars / straddles / naked OK.
- Live chains/quotes/trades **free** on the **indicative** feed (200).
- **Alpaca historical options BLOCKED** — `403 "OPRA agreement is not signed"` (paid).
- Friction seen live: liquid ATM SPY ~4¢ wide; deep-ITM/0DTE $5+ wide → bias to liquid,
  near-the-money, defined-risk; recon scores fills vs mid.

## ACCOUNT SIZING — $1000 (2026-06-24)
- Trades on a **$1000** account. Binding constraint = **strike spacing × account size**:
  $500+ underlyings have $5-10 strikes → min spread risk $500+ = untradeable; lower-priced
  $15-130 names ($0.50-1 strikes) → $50-100 risk = ideal, long premium affordable.
- **Per-trade max loss ≤ ~$100 (5-10% of book). DEFINED-RISK ONLY.**
- **Tradeable universe (lower-priced, liquid):** F · BAC · INTC · PFE · CSCO · MU · AMD ·
  PLTR · XLF · GDX · IWM · SPY (last two index spreads/harvest).

## BACKTEST DATA — Databento OPRA (research source; Alpaca = execution only)
- Box has a Databento key (`credentials/databento.key`) seeing **OPRA.PILLAR back to 2013**.
  So a real **locked-holdout backtest IS possible** (unlike via Alpaca).
- Pay-per-data; ~$113 free credit, **~$74 authorized**. Definitions cheap+cached; **daily bars =
  cost driver; NBBO quotes unaffordable (52GB/$97 per name-month — never pull).**
- Cost levers: **monthly-only expiries + ±10% NTM band** → ~$6/name (AAPL 6mo) vs $17 un-narrowed.
- Fetcher = **`databento_options.py`** (budget-gated, defs-cached, batched; `--plan/--defs-only/--arm`).
- v1 pull: 6mo window (2025-12→2026-06), monthly expiries, ±10% band, the 12-name universe above.

---

## CROSS-CUTTING FRAMEWORK

### Defining "R" for options (risk = MAX DEFINED LOSS at entry)
| Structure | R (max loss, per unit) |
|---|---|
| Long option (debit) | net debit × 100 × contracts |
| Debit vertical / calendar | net debit × 100 × contracts |
| Credit vertical / iron condor | (width − credit) × 100 × contracts |
| Long straddle/strangle | total debit × 100 × contracts |

**Realized R per trade = realized P&L ÷ R.** Strategy score = mean R per trade + cumulative
P&L over the growing sample (report both; rank primarily by mean R, tiebreak P&L, show N).

### Strike selection — FEED-INDEPENDENT (no Greeks needed)
Free indicative feed may not carry Greeks, so select strikes by **expected move**, not delta:
- **Expected move (EM)** = ATM straddle mid (nearest expiry) ≈ 1σ move. Robust, feed-only.
- Short strikes placed at EM multiples / % OTM bands; long wings = narrowest *liquid* width.
- Liquidity filter per leg: quoted spread ≤ ~8% of mid (skip illiquid/0DTE-deep-ITM), require
  a two-sided quote. If a leg fails the filter, skip the trade (log "no-fill-candidate").

### Sizing (tournament-measurement vs graduation)
- **Measurement phase:** each strategy trades **1 minimum defined-risk unit** per signal
  (1 contract / narrowest liquid spread). Purpose is *discovery*; R normalizes P&L so unequal
  notionals stay comparable. (The $1000 book can't hold 10 concurrent option structures at
  ~$100/unit — forcing it would starve the sample.)
- **Graduation:** only the winning strategy gets sized into the real ~$1000 book later, gated by
  `alpaca_recon` real-fill proof — same bar as the equity edges.
- Per-trade max-loss cap (config, default ≤ ~$100/unit = 5-10% of the $1000 book); lower-priced
  liquid underlyings + narrow ($0.50-1-wide) spreads keep within it.

### Risk controls (extend `guardrails.py` + `portfolio_gate.py` to options)
- **DEFINED-RISK ONLY** — no naked/short-premium without long wings, *despite* level 3.
- Options daily-loss halt (config, default −$30 across the options book) + per-strategy daily cap.
- Max concurrent options positions cap; **kill-switch covers the options bot** (master).
- EOD: no new opens after 15:45 ET; let evaluator mark expiries; flatten anything flagged.

### Attribution / orchestration
- Tag every order `OTT-<stratID>-<YYYYMMDD>` via `client_order_id` → fills attribute to strategy.
- **Daily orchestrator:** read live chains → build each structure → submit paper multi-leg orders.
- **Evaluator:** at exit/expiry, compute realized R + P&L per tag (reads fills + activities).
- **Ranker:** extend `night_report.py` with an options leaderboard (mean R, P&L, N, win%).

---

## THE 10 STRATEGIES

### Group A — express the PROVEN equity edges (defined-risk directional)
**1. MR-oversold → Bull put spread**
- Signal: `mean_reversion_scanner` long trigger (z ≤ −1.5, two-stage, `regime_ok`).
- Structure: sell put ~1×EM below spot / buy put one width lower. DTE 7–14.
- Exit: 50% credit capture · signal invalidation · or 2 DTE; stop 1.5–2× credit. R=(width−credit).

**2. MR-oversold → Long call (debit convexity)**
- Signal: same MR long trigger.
- Structure: buy ~ATM-to-0.5×EM-ITM call, DTE 7–14.
- Exit: scale +50%/+100% · stop −50% · time stop 2 DTE. R = debit.

**3. ORB-short tight-range → Bear call spread**
- Signal: `orb_scanner` short trigger (tight-range ≤0.66%, VWAP+volume).
- Structure: sell call ~1×EM above / buy one width higher. DTE 0–7 (weekly).
- Exit: 50% credit · EOD flatten (ORB is intraday) · stop 1.5–2× credit. R=(width−credit).

**4. MR-overbought → Bear call spread**
- Signal: `mean_reversion_scanner` short trigger (z ≥ +1.5).
- Structure/exit: as #3 but DTE 7–14. R=(width−credit).

### Group B — event-driven vol (the retail-feasible domain in the research doc)
**5. Earnings IV-crush short → Iron condor into the print**
- Signal: confirmed earnings ≤1 trading day out + high IV-rank (event vol overpriced vs realized).
- Structure: short strangle wrapped to iron condor; short legs ≈ ±1×EM; wings one width out.
  Nearest expiry *after* earnings.
- Exit: close the morning after earnings (capture crush); stop 2× credit. R=(width−credit).

**6. Earnings calendar spread** *(doc's primary event structure)*
- Signal: confirmed earnings; front expiry IV >> back expiry IV.
- Structure: sell front-week (event) ATM / buy next-week ATM, same strike.
- Exit: day after earnings (front crushes faster than back). R = net debit.

**7. Pre-earnings long straddle (cheap event vol)**
- Signal: earnings approaching AND extracted event-vol < historical realized jump (event-variance
  decomposition, doc §1).
- Structure: long ATM straddle, DTE spans earnings.
- Exit: close at/just-before earnings open, or day-after on the move; time stop −40%. R = debit.

**8. PEAD fade → debit vertical**
- Signal: 1 day post-earnings, large surprise/gap on a *liquid* name (overcrowded reaction).
- Structure: debit vertical fading the gap direction. DTE 5–10.
- Exit: +50% · reversal exhausts · stop −50%. R = debit.

### Group C — volatility-premium harvest (retail-safe skew / neutral control)
**9. Systematic OTM put-spread sell on liquid ETF (skew-premium harvest)**
- Signal: always-on baseline on SPY/IWM (retail-safe slice of the skewness risk premium, doc §1).
- Structure: sell ~0.7–1×EM OTM put spread, DTE 7–14.
- Exit: 50% credit · 21-DTE roll · stop 2× credit. R=(width−credit).

**10. Short-dated iron condor on liquid high-IV-rank name — NEUTRAL CONTROL/BENCHMARK**
- Signal: high IV-rank, no directional signal (range day).
- Structure: short legs ≈ ±1×EM both sides, defined wings, DTE 7–14.
- Exit: 50% credit · stop 2× credit. R=(width−credit).
- Role: the signal-free benchmark — if signal-driven strats don't beat this, the signals add nothing.

**Coverage:** 4 signal-directional · 4 event-vol · 2 vol-premium (one is the neutral control).
**Explicitly EXCLUDED** (institutional-only / data-hard, per research-doc reality filter):
dispersion, hard-to-borrow reversals, dividend arb, VPIN/PFOF internalization, GEX/gamma
front-running (parked as a possible *filter*, not a strategy).

---

## BUILD PLAN (phased, reuses existing infra)
1. **`options_lib.py`** — chain pull (indicative feed), EM/ATM-straddle calc, strike picker,
   liquidity filter, multi-leg order builder (`/v2/orders` `class:mleg` or `class:multileg`),
   R calculator. (foundation)
2. **`options_orchestrator.py`** — for each of 10: check signal → build structure → size 1 unit →
   guardrails+PM gate → submit tagged paper order. Cron during RTH; respect 15:45 cutoff.
3. **`options_eval.py`** — mark realized R + P&L per `OTT-` tag at exit/expiry (fills + activities);
   own the options paper CSV (never hand-write — same rule as equity).
4. **Guardrails/PM-gate extension** — defined-risk-only, options daily-loss, concurrent cap,
   kill-switch coverage.
5. **`night_report.py` leaderboard** — mean R, P&L, N, win% per strategy; surface daily top.
6. **`options_recon.py`** (later) — fills vs mid slippage = the go-live gate for the winner,
   mirroring `alpaca_recon`.

**Order:** 1 → smoke-test chain/EM/strike picker on SPY+1 single-name → 2 (paper, 1 strat first)
→ 3 → 4/5. Smoke-test each before wiring cron. No heavy compute during market hours.
