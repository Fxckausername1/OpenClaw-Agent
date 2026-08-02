# OPTIONS AGENT BLUEPRINT — autonomous single-stock options agent (heff research, 2026-06-25)
Canonical architecture for the options "brain." Implemented in `options_lib.py` (primitives) →
orchestrator/gate/recon (wiring). Philosophy: **paper edge is an illusion until it survives real
fills.** A Bayesian survival engine, not a latency/arb play. Maps onto existing infra (below).

## §0 Global constants (match the live equity stack exactly)
```
MAX_CONCURRENT_POSITIONS = 3      # == portfolio_gate.py
MAX_POSITIONS_PER_SIDE   = 2      # == portfolio_gate.py (de-correlation)
EXECUTION_CYCLE_SECONDS  = 120    # == alpaca_executor.py 2-min cron
EXECUTION_POLL_TICKS     = 4      # 30s ticks, t=0..4
NAV_ES_LIMIT             = 0.05   # ES_99% must stay <5% of NAV
MAB_SUCCESS_THRESHOLD    = 0.5    # R-multiple that counts as a 'success'
PSR_PROMOTE_THRESH       = 0.95
PSR_KILL_THRESH          = 0.50
MIN_TRADES_FOR_KILL      = 20
SLIPPAGE_EMA_ALPHA       = 0.10
```

## §1 Translation Engine — signal → optimal defined-risk spread
- **Breeden-Litzenberger RND:** extract market-implied risk-neutral density from the IV smile,
  `f(K) = e^{rT} ∂²C/∂K²`. Pipeline: IVs → 4th-deg smoothing spline → BS call prices → central
  finite diff `(C(K-h)-2C(K)+C(K+h))/h²` → clip≥0, normalize. Captures skew/kurtosis/tails the
  flat-σ Black-Scholes can't. (Invariant: risk-neutral `E[S_T] = S·e^{rT}` — used as the selftest.)
- **Discrete strike optimizer (NOT scipy.minimize — strikes are discrete):** vectorized numpy grid
  over valid `(K_short, K_long)` pairs ≤ max width. Per pair: `credit`, `max_loss=width-credit`,
  `PoP=∫f up to breakeven`, `R=reward/risk`, `EV=∫payoff·f − cost`. Maximize
  `U = ω1·(EV/risk) + ω2·ln(R) + ω3·PoP` s.t. `PoP≥0.65`, `R≥0.33`, width≤max, **REV>0**.
- **Skew→strategy map:** `skew_slope=(IV_10Δ−IV_50Δ)/IV_50Δ`. Steep put smirk → bull put credit
  (sell inflated puts); flat → iron condor/straddle; inverted/upside → bear call credit; penalize
  debit spreads when skew is >1.5σ rich (vol-crush risk).

## §2 Microstructure / Friction Layer — execution gatekeeper
- **Pre-trade veto:** `S_penalty = ½·Δspread + λ·σ·√(Q/V)` (half-spread + square-root market impact).
  If `EV − S_penalty ≤ 0` → VETO (signal may be perfect but the spread is too illiquid).
- **Exponential limit pacing over 120s:** `L(t)=P_mid−(P_mid−P_nat)·(e^{κ(t/N)}−1)/(e^κ−1)`, κ=2.
  t=0 posts at mid (capture edge); t=N crosses at nat (guarantee fill before signal decays).
  Native multi-leg orders (no legging risk). Partial fill at t=N → IOC the remainder at P_nat.

## §3 Forward Tournament — dynamic ranking of ~10 sub-strategies
- **Thompson sampling (Bayesian MAB):** each strategy ~ `Beta(α,β)`; success (R≥0.5) → α+1, else β+1.
  Draw `θ̂~Beta(α,β)`, rank by θ̂ (exploit winners, still explore newcomers). Exponential time-decay
  on (α,β) for non-stationary regimes.
- **Deflated Sharpe Ratio guard (Bailey/López de Prado):** corrects multiple-testing + non-normality.
  `SR* ≈ √V[SR]·((1−γ)Z⁻¹(1−1/N) + γ·Z⁻¹(1−1/(Ne)))`; `PSR(SR*)=Φ((SR−SR*)√(T−1)/√(1−skew·SR+(kurt−1)/4·SR²))`.
- **Lifecycle:** PSR>0.95 AND top-3 over rolling 5d → **PROMOTE**; 0.5≤PSR≤0.95 → keep PAPER;
  PSR(0)<0.5 after ≥20 trades → **KILL**. ⚠️ *See reconciliation: PROMOTE = surface-for-confirmation,
  not auto-live (real money stays behind confirm-every-trade).*

## §4 Portfolio Risk & Dynamic Greeks
- **MILP knapsack** (`scipy.optimize.milp`, binary x): `max Σ x_i·REV_i` s.t. `Σx≤3`, `Σ_bull x≤2`,
  `Σ_bear x≤2`, optional per-correlation-group cap (categorical row). EV computed at **pessimistic**
  prices (bid for short / ask for long), not mid. (Replaces portfolio_gate's planned_rr ranking.)
- **Expected Shortfall gap guard:** shock each underlying by `±3×ATR_30`, mark spreads to the shocked
  nodes, `ES_99% = E[L | L>VaR_99%]`. If `ES_99% > 5% NAV` → block new + deleverage the highest-|β_SPY|
  position before close.

## §5 Reconciliation — theoretical vs realized alpha (the closed loop)
- **Slippage drag:** `δ=(P_realized−P_mid)·D`, D=−1 credit / +1 debit (positive δ = cost).
- **Per-ticker EMA:** `δ̄_j = η·δ + (1−η)·δ̄_j`, η=0.1. Inject forward: `REV = EV_theoretical − δ̄_j`.
- **Cybernetic consequences:** toxic-microstructure tickers self-veto (δ̄→ REV<0); spread-crossing
  strategies decay → PSR drops → KILL. Survival by isolating where positive EV survives real friction.

## RECONCILIATION WITH EXISTING INFRA (heff's rules win)
1. **Real-money boundary:** §3 "Promote to LIVE" ⇒ in our system = **promoted/surfaced for
   confirm-every-trade**, NOT auto-executed real orders. Paper tournament stays autonomous (== the
   existing Alpaca paper bot); real money stays gated. This overrides the blueprint's auto-live.
2. **Data:** live Translation Engine uses Alpaca's FREE indicative chain (bid/ask/IV, verified in
   `options_probe.py`). The BACKTEST Translation Engine uses the **computed IV surface from
   `greeks.py`** (we have Databento daily close=last-trade + solved IV, not historical NBBO) — RND
   from modeled IV, flagged modeled (same honesty bar as the Gamma Playbook GEX work).
3. **Gate:** §4 MILP supersedes `portfolio_gate.py`'s rr-ranking but keeps its 3/2 constraints.
4. **Recon:** §5 extends `alpaca_recon.py` (already the go-live gate) to per-ticker slippage EMA.
5. **GEX confluence:** the Gamma Playbook regime (`gex.py`) feeds the skew/strategy selection in §1
   (POS-gamma → favor mean-rev-expressed credit puts; NEG-gamma → favor ORB-expressed bear calls).
6. **Discipline unchanged:** locked 75/25 holdout for any backtest; KILL mirages; no heavy compute
   during market hours; no hand-written CSVs; back up before overwrite.

## BUILD ORDER
1. `options_lib.py` — RND · discrete strike optimizer · pacing ladder · MILP selector · PSR/DSR ·
   slippage EMA · ES shock. Each with a synthetic `--selftest` ($0, market-safe). ← FIRST
2. `options_orchestrator.py` — wire signal→optimize→friction-veto→gate(MILP)→pace→submit (1 strat first).
3. `options_eval.py` — owns the options trades CSV (never hand-written) + per-strategy Beta/returns state.
4. Tournament loop (Thompson + DSR lifecycle) + `night_report.py` leaderboard.
5. `options_recon.py` — realized-vs-theoretical drag, REV feedback → go-live gate.
