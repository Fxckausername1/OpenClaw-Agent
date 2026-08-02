# OPTIONS ORCHESTRATOR SPEC — options_orchestrator.py (heff research, 2026-06-25)
Wiring layer on top of `options_lib.py`. Source: two research PDFs in `docs/`
(`Orchestrator_Architecture_Blueprint.pdf`, `Options_Trading_Orchestrator_Architecture.pdf`).
**The math is adopted in full; the deployment is adapted to our cron/box reality (see ⚠️).**

## 1. Signal→density tilt — ESSCHER TRANSFORM (✅ built in options_lib.py)
The static chain is no-arb → EV≈0; the edge comes ONLY from the equity signal. Map signal→tilt
via **Entropy Pooling**: minimize KL-divergence to the BL risk-neutral density subject to ONE
expected-return view `μ_signal = α·z` (α calibrated per signal type). The closed-form solution IS
the **Esscher transform** `p*_i ∝ p_i·e^{θ·S_i}`, reduced to a single monotonic root
`g(θ)=Σp_iS_ie^{θS_i}/Σp_ie^{θS_i} − S0(1+μ)=0` solved by Brent in sub-ms. `esscher_tilt()` done
+ selftested (hits target mean, θ>0 for bullish). Tilted density → `optimize_spread()` → positive-EV
strikes/structures. **This formalizes the no-arb insight from the agent blueprint.**

## 2. Async event loop & API rate limiting  ⚠️ ADAPTED TO CRON
- **Idealized design (PDFs):** always-on asyncio daemon — SIP websockets (Thread 1), 120s
  signal/MILP loop (Thread 2), execution pacer (Thread 3); CPU-bound math in a
  `ProcessPoolExecutor` so the MILP/Esscher don't block the event loop; `asyncio.PriorityQueue`
  (P1 cancel/replace · P2 mleg submit · P3 reconcile · P4 chain poll); Token Bucket (cap 190,
  refill ~3.16/s) under Alpaca's 200 req/min; hosted AWS us-east-1 for single-digit-ms latency.
- **⚠️ OUR REALITY:** the live stack is **cron-driven** (`alpaca_executor.py` every 2 min, `*/2 13-20`)
  on a **1.9GB DigitalOcean box** — NOT an always-on AWS daemon. The **2-min cron tick already IS
  the blueprint's temporal aggregation window.** So we **port the math, not the daemon**: the
  orchestrator runs once per tick (poll signals → tilt → optimize → MILP → pace → submit →
  reconcile), no websocket service, no ProcessPoolExecutor (our MILP is ≤~10 vars = microseconds
  inline), no colo (2-min/paper cadence is latency-insensitive). **`TokenBucket` (cap 190) + the
  P1>P2>P3>P4 ordering ARE kept** (built in options_lib.py) for ticks where many signals fire.

## 3. Concurrency & MILP knapsack
Batch the tick's signals → Esscher-tilt each → optimize spreads → collate EV array → `scipy.milp`:
`max Σ EV_j·y_j` s.t. `Σy+open≤3`, `Σ_bull+longs≤2`, `Σ_bear+shorts≤2`, **`A_margin·y ≤ buying_power`**.
`select_portfolio_milp()` has the 3/2 caps ✅; **TODO: add the margin/BP row** (max_loss·y ≤ free
capital, sized to the **$1000 book**). EV uses pessimistic prices (bid short / ask long) ✅.

## 4. Multi-leg FSM & fault recovery (→ options_orchestrator.py, next phase)
`INIT→PENDING_NEW→WORKING→REPLACING→PENDING_REPLACE→FILLED|CANCELED|RECOVERY_HEDGE`
(states stubbed in options_lib.py). Critical, ADOPT verbatim:
- **Legging risk killed by the `mleg` class** — exchange fills the whole package atomically or
  rejects; partial fills apply to the **ratio** (e.g., 3 of 10 iron condors = 3 of every leg), never a
  naked leg.
- **Synthetic IOC** (the real Alpaca constraint): mleg supports **TIF=`day` only**, so at `t_max`
  (120s) submit the aggressive limit at the natural price, wait ~2000ms, then force-cancel the
  remainder. No native IOC for mleg.
- **Fault recovery on websocket/timeout:** halt new signals → **REST `GET /v2/orders`+`/v2/positions`
  reconcile to ground truth** → rebuild FSM state → idempotent cancel if past `t_max` → re-subscribe.
  (Our cron model leans on this REST-reconcile each tick anyway — a natural fit.)

## 5. Adaptive calibration & LLM oversight  ⚠️ POST-CLOSE ONLY
- **Bayesian param tuning:** a GP surrogate (`scikit-optimize`) tunes hyper-params (tilt α/θ, pacing
  κ/t_max, MILP risk weights) to maximize realized Sharpe − EV-tracking-error; hot-swap via
  `config/adaptive_params.json`. ⚠️ Runs **post-close/weekends** (matches no-heavy-compute-during-
  market-hours + the existing post-close cron pattern). Don't tune on the live tick.
- **LLM math-revision loop:** if 14-day EV-tracking error breaches 2σ, package telemetry+code →
  LLM proposes a model change (e.g., Esscher→Student-t heavy tail under high VIX) → push thesis +
  git diff → **merge ONLY on explicit human APPROVE.** Aligns with heff's confirm-every-change culture.

## 6. Interfaces (DTOs + Alpaca payload)
- `EquitySignal(symbol, timestamp_ms, signal_type∈{MR_OVERSOLD,MR_OVERBOUGHT,ORB_BREAKOUT}, z_score)`
  → feeds `μ_signal=α·z`. `TiltedOptionEdge(symbol, legs, tilted_ev, capital_required, pop)`.
  `PortfolioState(total_positions, long_count, short_count, available_buying_power)`.
  (options_lib.py has ProposedSpread/SpreadLeg/StrategyState; align/extend when wiring.)
- **Alpaca mleg payload:** `{"class":"mleg","time_in_force":"day","legs":[{symbol,ratio_qty,side}...],
  "type":"limit","price":<net debit + / credit ->signed>}`.

## RECONCILIATION (heff's rules win — same as the agent blueprint)
1. **Real money stays confirm-every-trade.** Orchestrator is autonomous in **PAPER only**; promotion
   surfaces for confirmation, never auto-fires real orders.
2. **Cron, not daemon; DO box, not AWS colo.** Port math; skip the always-on/websocket/colo layer.
3. **Heavy calibration (GP/LLM) = post-close**, never on the market-hours tick (1.9GB box, scanners).
4. **$1000 book** sizing; defined-risk, per-trade max-loss ≤ ~$100; add the MILP margin row.
5. Backtest path still uses greeks.py computed IV surface (modeled); live uses Alpaca free chain.

## BUILD ORDER (unchanged, now detailed)
1. ✅ `options_lib.py` primitives (RND, **Esscher tilt**, optimizer, friction/pacing, Thompson/DSR,
   MILP 3/2, ES, slippage EMA, **TokenBucket**, FSM states). Selftest 28/28.
2. `options_orchestrator.py` — per-tick: poll signals → tilt → optimize → MILP(+margin) → FSM pace
   (synthetic-IOC) → submit mleg → REST-reconcile. ONE strategy first, paper, behind the existing
   guardrails/kill-switch. Smoke-test on the indicative chain before any cron.
3. `options_eval.py` (owns options CSV + Beta/returns state) → tournament loop (Thompson+DSR) →
   night_report leaderboard → `options_recon.py` (realized drag → REV feedback → go-live gate) →
   post-close `adaptive_calibration.py` (GP) + the APPROVE-gated LLM loop.
