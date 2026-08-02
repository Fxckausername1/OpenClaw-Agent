# SMC repair audit — 2026-07-31

## Operational status

- Both scheduled legacy SMC order paths are disarmed. Their wrappers invoke the Python programs without `--arm`.
- The consolidated `smc_run.py` runner also defaults to dry-run and is not scheduled or armed.
- Final read-only broker reconciliation: zero QQQ option positions and zero QQQ open orders.
- No broker order was submitted, cancelled, or modified during this repair.
- Non-SMC positions and orders were left untouched.

The crontab descriptions still contain stale `ARMED` wording. The executable wrapper source is authoritative and is dry-run. Do not re-arm from the comments.

## Repairs completed

### Live execution and lifecycle

- Reconciliation mismatch positions remain under protective supervision and block new entries.
- Startup and per-pass reconciliation failures fail closed; detection/submission does not continue against unreadable or ambiguous state.
- Working entry and exit orders are recovered by durable broker evidence after restarts or transport timeouts.
- A replacement is never submitted until cancellation of the prior order is confirmed terminal.
- Partial fills and realized P&L are reconstructed from durable fills; the system no longer fabricates a zero fill price.
- Orphan QQQ positions and working orders halt entries for manual attribution.
- Telegram/reporting failures cannot unwind committed state or block protective exits.
- Entry submission requires a fresh, valid, two-sided OPRA quote and reprices from the OPRA ask. Indicative quotes cannot reach submission.
- Protective price decisions reject stale quotes.

### Backtest/live parity

- Net P&L and slippage are consistently expressed in account dollars, including quantity and the 100x option multiplier.
- Headline B1 selection no longer uses an end-of-day Greek at an intraday decision time. Delta is derived from the decision-time underlying price and latest eligible option midpoint.
- The entry model now uses the live-matching 3-second reaction latency and 20-second order TTL.
- Signals are replayed chronologically through the shared admission policy: same-contract duplication, concurrency, and correlated exposure are enforced before fill evaluation.
- Admission rejection is recorded separately from no-contract and no-fill outcomes.
- Reruns atomically replace the target ledger instead of appending duplicates.
- Low-memory aborts fail before overwriting a complete ledger.

The model uses 4:00 p.m. ET as the expiry/settlement clock. QQQ ETF options may remain tradable until 4:15 p.m., but Cboe states that OCC closing/settlement marks and in/out-of-the-money determination use the 4:00 p.m. NBBO and underlying close: <https://www.cboe.com/document/tech-spec/document/technical-specifications/equity-options-extended-trading-hours-faq>.

## Corrected headline replay

Run: `smc_point_in_time_live_parity_20260731`

- Signals: 1,779 across 162 sessions
- No contract: 1,360
- Admission rejected: 13
- Selected but not filled: 0
- Filled: 406 (22.8% of all signals)
- Net expectancy per filled trade: $4.37
- Session-cluster bootstrap 95% CI: $3.03 to $5.88 across 134 filled sessions
- Profit factor: 2.97
- Win rate: 61.8%
- Net expectancy excluding the best five sessions: $3.16
- Charter-window-only expectancy: $2.51
- Average modeled friction: 19.0% of planned gross target

These are research results, not approval to trade. The 162 sessions were already used for development and parameter exploration, so this is not untouched forward validation. Historical option observations are sparse trade-associated NBBO rather than a continuous quote stream; queue priority, unobserved partial fills, and cancel/fill races cannot be reconstructed. The 3-second latency is a frozen assumption, not a measured broker-acknowledgement distribution. Legacy trailing, reclaim, selector-sweep, and holdout reports are non-promotable until rerun under the same point-in-time and chronological admission rules.

## Verification

- Backtest/pipeline suite: 363 passed, 0 failed.
- SMC lifecycle/safety suite: 99 passed, 0 failed.
- Total: 462 passed, with `ResourceWarning` treated as an error.
- Compile-all passed for `smc`, `thetadata_pipeline`, and the live selector.
- Scoped `git diff --check` passed.

## Re-arm gate

Do not re-arm yet. The next authorized step is one full market session of the consolidated runner in shadow mode, with measured signal-to-quote, quote-to-submit-decision, and broker-acknowledgement latency. Only after a clean shadow session should a separately approved one-contract paper canary be considered. Any `RECON_MISMATCH`, unattributed QQQ exposure/order, stale/non-OPRA entry quote, or unconfirmed cancel is an immediate stop condition.
