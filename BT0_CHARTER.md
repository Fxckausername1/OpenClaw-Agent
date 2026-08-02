# BT-0 CHARTER — Options Backtesting Program (2026-07-26)

Per `BOT_NEXUS_Options_Strategy_Backtesting_Roadmap.pdf` (BT-OPT) Section 18: BT-0's
deliverable is "Questions, schemas, metrics, success criteria and test split," gated
on being **signed/frozen before any test-period access**. This document is that
freeze attempt. Sections marked **PROPOSED** are Claude's draft, not yet
heff-approved — everything else restates BT-OPT's own already-fixed rules. Nothing
in this document authorizes writing backtest code yet; it exists to be argued with
and signed off on FIRST.

**Why now, and why scoped down from BT-OPT's full ambition:** BT-OPT's own document
assumes a mature research program (regime-by-regime grids, multi-year history,
hundreds of trades). Real data reality as of this writing: 28 real trading days of
SPY/QQQ tick-level ThetaData history (TD-5, backfilled 2026-07-26), plus 11 more days
of a single tracked weekly expiration for Ghost Wall confirmation specifically. That
is nowhere near enough for BT-OPT's full scope — its own data-sufficiency ladder
would call this "Exploratory" at best. This charter deliberately freezes a SMALL,
honest v1 rather than pretending to freeze the whole roadmap, so it can actually be
followed instead of quietly ignored once real numbers arrive.

## 1. Primary research question (frozen, per BT-OPT Section 1)

Can a post-10:00-ET, Catalyst-Brief-informed, HEFF-SMC-indicator-triggered manual
long-options entry produce positive expectancy after realistic execution costs
(real NBBO fills, not midpoint; real fees; real spread)? This is the SAME question
BT-OPT Section 1 poses — nothing scoped down here, since it doesn't depend on
sample size to state.

## 2. Questions this v1 charter actually commits to answering (PROPOSED, scoped)

BT-OPT Section 2's full question inventory (premium-band optimality, DTE studies,
entry-time studies, regime-by-regime splits, cross-symbol generalization, etc.) all
require far more sessions than exist today. **This charter commits to only the
smallest useful slice**, expanded later as real history accrues:

- Does the full process (Section 9's "B6 Full process") show ANY net expectancy
  signal at all, even a noisy one — not "is it good," just "is it not obviously
  broken"?
- What fraction of signals produce a contract that even passes the viability gate
  (CB-V4 Section 8 PASS/WAIT/FAIL) — i.e., how often is there simply NO TRADE
  available, which BT-OPT Section 1 explicitly requires treating as a real outcome,
  not a discarded signal?
- Ghost Wall next-day-OI confirmation and the Control Map's `volatility_lean`
  threshold: continue the TD-5 calibration exercise as more real days accrue,
  reporting Exploratory/Preliminary/Research-ready honestly each time (already
  found: volatility_lean shows no proven edge past the window's own base rate;
  Ghost Wall confirmation needed a backfill-scope fix and now has an initial small
  sample).

**Explicitly deferred, not asked yet:** premium-band optimization, DTE comparison,
entry-time-of-day study, regime-conditional splits, cross-symbol generalization
beyond SPY/QQQ. Asking these now, on ~30-40 days of history, would produce numbers
with no real statistical footing — BT-OPT Section 15 itself warns against exactly
this ("a result selected from hundreds of variants requires stronger evidence than
a single preregistered test").

## 3. Canonical schemas (frozen, adapted from BT-OPT Section 4 to this codebase's real fields)

### Strategy specification
```
strategy_id, version, symbols (SPY|QQQ only for v1), session_window (10:00-15:30 ET),
direction_gate (control_map_verdict: CALL WATCH | PUT WATCH only -- TWO-SIDED/WAIT/NO
  TRADE all mean no signal, not a coin-flip direction),
contract_gate (CB-V4 Section 8: PASS only for v1 -- WAIT/FAIL trades excluded from the
  "traded" ledger but logged as no-fill outcomes per BT-OPT's own rule),
fill_model, position_size, exit_rules, event_blackouts, parameter_set_id,
created_before_test_date
```

### Simulated/shadow trade ledger
```
trade_id, session, symbol, signal_ts, decision_ts, contract_id,
entry_quote_ts, entry_bid, entry_ask, entry_fill, quantity,
context_snapshot_id (points at the exact catalyst_brief report_id + contract_viability_card
  read that produced this trade -- BT-OPT Section 17's "identical information sets" rule),
target, stop, invalidation, exit_ts, exit_bid, exit_ask, exit_fill, exit_reason,
gross_pnl, fees, slippage, net_pnl, mae, mfe, rule_flags, data_quality, experiment_id
```

Reuses BT-OPT's own field names verbatim where they map directly (the doc's schema was
already well-designed); `context_snapshot_id` and the `contract_gate` field are the two
real additions tying this to code that actually exists today
(`catalyst_brief.py`'s `report_id`, `contract_quote.py`'s gate vocabulary).

## 4. Success metrics (per BT-OPT Section 14, unchanged — these don't need scoping down)

Net expectancy per trade, profit factor, average win/loss payoff ratio, maximum
drawdown, fill rate (PASS-gate rate, effectively, for v1), friction share of gross
target. Session-level (not trade-level) bootstrap confidence intervals, since intraday
trades on the same day are not independent draws.

## 5. Success criteria (PROPOSED — needs explicit sign-off, this is the real freeze)

No promotion decision (BT-7) without ALL of:
- **Minimum sample:** at least 30 independent trading sessions with a PASS-gated
  signal (not just 30 calendar days — days with no signal don't count toward this).
- **Net expectancy:** positive after modeled costs (spread-paid fill, not midpoint;
  real per-contract fees).
- **Session-bootstrap 95% CI lower bound > 0** on net expectancy.
- **No single session or short cluster of sessions accounts for all the edge** —
  BT-OPT Section 15's "remove the best five sessions" robustness check must still
  show a positive (even if smaller) result.

Below this bar: the honest label is "Continue research," not "Reject" and not
"Approve" — BT-OPT Section 19 explicitly allows for this, and given the real
sample sizes involved for the next several weeks, "Continue research" is the
EXPECTED outcome for a while, not a disappointing one.

## 6. Test split (PROPOSED, and deliberately provisional)

BT-OPT Section 13's chronological development/validation/test structure requires
real volume this charter doesn't have yet. Rather than freezing arbitrary day-counts
today:
- **Development window:** every real session from here forward, used to build and
  debug the simulator (BT-1/BT-2) and run the earliest baseline experiments (BT-3).
- **Validation window / final test window:** NOT dated yet. This charter commits to
  freezing an exact date range for these ONLY once the development window reaches a
  real, pre-agreed size (proposed: 60 real PASS-gated trading sessions — open to
  heff's own number instead). Once frozen, the final test window is never used for
  parameter selection, full stop, per BT-OPT Section 13's own nested-selection rule.
- Until that freeze happens, no result from this backtesting program should be
  described as "validated" or "out-of-sample" — everything before the freeze is
  development-stage exploration, however good it looks.

## 7. What BT-0 does NOT authorize

Per BT-OPT's own acceptance gate ("signed/frozen before final-period access"): this
document does not authorize starting BT-1 (the actual data/simulator build) until
Section 5 and Section 6's PROPOSED items get explicit sign-off. If any number here
gets changed after seeing real results, BT-OPT Section 13's own rule applies: the
old "final test" becomes development data, and a new, real, untouched period is
required before anything can be called validated again.

## Addendum — Sections 5 & 6 FROZEN (2026-07-31, signed off by heff)

**Trigger:** Section 6's own proposed threshold (freeze a validation/test window once the
development window reaches 60 real PASS-gated sessions) has been cleared. Real counts as of
2026-07-31: 85 sessions-with-fill (indicator-only baseline), rising to 94/120/128/153 across
the various selector configs tested since — all well past 60.

### Section 5 (success criteria): ADOPT AS WRITTEN, no changes proposed

- Minimum sample: ≥30 independent PASS-gated sessions
- Net expectancy positive after modeled costs (real spread-paid fill, not midpoint)
- Session-bootstrap 95% CI lower bound > 0
- Robust to excluding the best 5 sessions (still positive)

Nothing found since the 2026-07-26 draft argues for changing any of these four bars.
Recommend adopting verbatim, no edits.

### Section 6 (test split): the honest problem, and the actual freeze

The original Section 6 language reads as if a good validation/test window could be carved
out of the EXISTING history once it got big enough. **That's no longer possible, and
pretending otherwise would violate this same document's own Section 13 nested-selection
rule.**

Why: every session from 2025-10-01 through today has already been used, repeatedly, for
parameter selection — B0 random control, B1 indicator-only (162 sessions), a 30-variant
indicator param sweep, and two rounds of selector sweeps (moderate_combo/wider_delta/
wider_premium/wider_quality, then wide_premium_020_100, promoted live 2026-07-31). None of
that history can retroactively become an "untouched" holdout no matter how it's sliced —
it's development data, permanently, regardless of how good the numbers look.

**Actual freeze, proposed:**

- **Development window (closed):** every session through 2026-07-30 inclusive. No further
  parameter selection — indicator OR selector — may be tuned against this window and then
  described as "validated."
- **Validation/test window (opens 2026-07-31, forward-only):** every session from today
  forward, real live-shadow/paper-fill data only, collected without being used to pick any
  parameter. This window can only accumulate going forward — no backtest re-run against
  historical data can ever populate it retroactively.
- **Validation threshold:** once the validation window itself reaches ≥30 independent
  PASS-gated sessions (mirroring Section 5's own minimum-sample bar), a result computed
  ONLY on that window — never blended with development-window data — can be called
  validated/out-of-sample for the first time.
- **Standing rule (restates Section 7, unchanged):** any parameter change made after seeing
  validation-window results — indicator or selector — invalidates that window from that
  point forward. It becomes development data, and a new, untouched validation window starts
  from the date of the change.

**Practical consequence for what's live right now:** the 2026-07-31 selector promotion
(wide_premium_020_100) was selected using 100% development-window data. Today is therefore
the correct start date for the first real validation window under this rule. Any further
selector or indicator change resets the clock again — so changes should be made
deliberately, not casually, once this is signed off.

---
*Signed off by heff 2026-07-31. Sections 5 and 6 of the body above are superseded by
this addendum; the body text is left unedited for history, per this charter's own
Section 7 convention (a changed number means a new frozen statement, not a silent
rewrite of the old one).*
