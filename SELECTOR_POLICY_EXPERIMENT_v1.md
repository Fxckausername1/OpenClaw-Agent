# SELECTOR_POLICY_EXPERIMENT_v1

Paper-only. Nothing armed, no cron installed, no live wrapper modified, no
broker state touched, no selector policy changed in production. All numbers
below are development-window data (BT0_CHARTER's window closed 2026-07-30) —
**not promotable, not a live configuration change**, exploratory only.

## 0. Identity — pinned exactly

| | |
|---|---|
| Base dataset | v2.2 replay, 1,804 signals (same as `SELECTOR_REJECTION_AUDIT_v1.md`) |
| git commit | `37ed48d6931a3b3b7d9af3f1169c8f4443fc7fa0` (unchanged since the prior audit) |
| Run | `selector_policy_experiment_v1` run `20260801T180703Z-bbfc8c` |
| Command | `./venv/bin/python run_selector_policy_experiment.py` |
| Wall time | 8,760.9s (2h26m) — 5 independent chronological replays sharing one I/O pass per session |
| Artifacts | `data/thetadata/runs/selector_policy_experiment_v1/20260801T180703Z-bbfc8c/{manifest,report,raw_rows,sweep_reclaim_premium_relief}.json` |
| New code | `thetadata_pipeline/selector_policy_experiment.py`, `thetadata_pipeline/selector_policy_stats.py`, `thetadata_pipeline/tests/test_selector_policy_experiment.py` (23 tests, all passing), `run_selector_policy_experiment.py` |

**bt2_selector.py, bt2_simulator.py, bt2_fills.py, bt2_exits.py — the frozen
baseline — were not edited.** Variant B's debit-cap rule and the delta-
capture needed for Variant D's report are both implemented as external
monkeypatches, restored in a `finally` after every single session call (not
just at the end of the whole run), and proven inert to selection behavior by
`test_selector_policy_experiment.py`'s integration tests (verifies the patch
target is correct — `bt2_simulator.select_contract`, not
`bt2_selector.select_contract`, since `bt2_simulator` imported the name
directly and a same-module-namespace patch would silently not apply).

### Locked train/holdout split (chronological, fixed before any variant ran)

| | Sessions | Signals |
|---|---|---|
| TRAIN | 2025-12-03 .. 2026-06-10 (130 sessions, 80%) | 1,432 |
| HOLDOUT | 2026-06-11 .. 2026-07-28 (32 sessions, 20%) | 372 |

Chronological, not random, so no signal from a later session ever informs a
selection at an earlier one — point-in-time safety is inherited unchanged
from `build_point_in_time_book`'s own `trade_timestamp <= decision_ts`
filter. **All 5 variant policies (A/B/C1/C2/C3) were specified in full by
heff before this run** — none is fit or tuned against any result here, so
"lock policies using training data" is satisfied trivially: nothing is
optimized. Every table below reports TRAIN and HOLDOUT separately so
performance can be judged on the untouched tail without conflating it with
the heavily-mined earlier segment; **holdout numbers are what should
actually inform any judgment**, train numbers are shown for transparency
only.

---

## 1. Read this before the tables — the central interpretive issue

**Exit targets and stops are percentage-of-entry-premium
(`target_return`/`premium_stop_pct` in `ExitConfig`), and quantity is always
1 contract.** That means a $2.00 contract hitting the same % target as a
$0.25 contract pays out 8x more dollars for an identical underlying %
move — bigger dollar P&L from a wider premium band is **partly a bigger bet
size, not automatically a better edge.** This experiment checks that
explicitly rather than reporting raw dollar figures as if they were
apples-to-apples:

| Variant | avg debit/trade | avg % return | median % return | win rate |
|---|---|---|---|---|
| A_baseline | $25.63 | 17.53% | 23.17% | 61.8% |
| B_delta_first_debit_cap | $72.48 | 13.73% | 23.02% | 62.7% |
| C1_premium_0.20_0.50 | $38.80 | 13.66% | 22.50% | 62.0% |
| C2_premium_0.20_1.00 | $74.04 | 12.07% | 23.04% | 62.8% |
| C3_premium_0.20_2.50 | **$146.13** | 10.57% | 22.46% | 61.1% |

Position size scales up to **5.7x baseline (C3)**. Median % return is
essentially flat (~22.5–23.2%) across every variant — the fixed % exit
rule mechanically produces similar median outcomes regardless of which
contract was picked. **The average % return mildly declines** as the band
widens. Splitting this by SWEEP_RECLAIM vs. the other four triggers resolves
why (Section 3): the other four triggers' *win rate* genuinely improves with
a wider band (a real, position-size-independent signal), but SWEEP_RECLAIM's
volume share grows even faster and its win rate sits at a near-coin-flip
48–52% with a negative median return, dragging the pooled average down.
**Read every dollar-P&L number in Section 2 with this in mind — bigger totals
are not, by themselves, evidence of a better edge.**

---

## 2. Full battery, per variant, HOLDOUT (32 sessions, 372 signals) — the numbers that matter

| Metric | A (baseline) | B (delta-first, $100 cap) | C1 ($0.50) | C2 ($1.00) | C3 ($2.50) |
|---|---|---|---|---|---|
| Selectable-contract count / rate | 55 / 14.8% | 358 / 96.2% | 216 / 58.1% | 357 / 96.0% | 360 / 96.8% |
| Fill count / rate | 54 / 14.5% | 345 / 92.7% | 207 / 55.6% | 343 / 92.2% | 347 / 93.3% |
| Sessions with a fill | 21 / 32 | 32 / 32 | 31 / 32 | 32 / 32 | 32 / 32 |
| — mean debit ($/trade) | $25.63 | $72.48 | $38.80 | $74.04 | $146.13 |
| — median debit ($/trade) | $25.00 | $75.00 | $40.00 | $77.00 | $148.00 |
| Actual selected delta (mean\|Δ\|, MAE vs 0.35 target) | 0.200, MAE 0.159 | **0.310, MAE 0.112** | 0.232, MAE 0.140 | 0.312, MAE 0.111 | 0.498, MAE 0.165 |
| Net expectancy/trade (95% bootstrap CI) | $3.53 [−$1.42, $9.53] | **$7.64 [$4.66, $10.55]** | $5.32 [$2.70, $8.06] | $7.80 [$4.74, $10.77] | $11.46 [$6.43, $16.48] |
| Total net P&L (holdout) | $190.60 | $2,636.50 | $1,101.30 | $2,673.70 | $3,978.30 |
| Max drawdown ($) | $75.40 | $180.50 | **$104.50** | $194.90 | $390.00 |
| Win rate | 51.9% | 60.3% | 59.9% | 60.6% | 58.2% |
| **Profit factor** | 2.04 | 2.17 | **2.40** | 2.18 | 1.84 |
| Avg fees/trade | $0.10 | $0.10 | $0.10 | $0.10 | $0.10 |
| Gross (midpoint) vs net expectancy | $4.76 vs $3.53 | $9.22 vs $7.64 | $6.63 vs $5.32 | $9.38 vs $7.80 | $13.61 vs $11.46 |
| Total friction (slippage+fees), holdout | $66.40 | $544.50 | $270.20 | $544.80 | $744.20 |

**A_baseline's own holdout CI crosses zero** ($3.53, CI [−$1.42, $9.53],
n=54, 21 sessions with a fill) — the current live-equivalent policy is not
itself statistically distinguishable from a null edge on this held-out
tail, even though its full-162-session number ($4.37) is positive. Every
other variant's holdout CI is entirely positive.

**C3 has the highest total P&L and the worst risk-adjusted profile**: lowest
profit factor of all five (1.84), by far the largest drawdown (5.2x
baseline), and its actual selected delta (mean 0.50) *overshoots* the 0.35
target further than any other variant — it does not even serve the "delta
target" objective as faithfully as B or C2 do. Treat C3's large total P&L as
a position-size effect, not a demonstrated better edge (Section 1).

**B achieves the closest actual delta to the 0.35 target** (MAE 0.112, vs.
baseline's own MAE 0.159 under its *stated* 0.35 target) while keeping an
explicit, auditable per-trade risk bound — unlike C2/C3, which have no cap
and show escalating drawdown as the premium ceiling rises.

**C1 (the smallest change from current production) has the best profit
factor of all five variants** (2.40) with the most contained drawdown
increase (1.4x baseline vs. B's 2.4x or C3's 5.2x) — see Section 5 for why
this makes it the lowest-risk candidate if a smaller first step is
preferred over B.

Full-162-session (train+holdout combined) figures, for transparency only —
**do not use these to pick a variant**, they include the heavily-mined
training segment:

| | A | B | C1 | C2 | C3 |
|---|---|---|---|---|---|
| n_filled | 406 | 1,636 | 1,209 | 1,611 | 1,603 |
| Net expectancy/trade | $4.37 | $7.95 | $4.85 | $7.94 | $12.76 |

---

## 3. By trigger, holdout, excluding SWEEP_RECLAIM (see Section 4 for that)

Win rate for all four of MSS/BOS/MA_FADE/PULLBACK *improves* (not just
"more trades, same rate") as the premium band widens — win rate is
position-size-independent, so this is real evidence the band was excluding
some genuinely good trades for these triggers, not only inflating dollar
totals via bigger bets:

| Trigger | A win rate | B win rate | C1 win rate | C3 win rate |
|---|---|---|---|---|
| MSS | 77.8% (n=9) | 83.0% (n=47) | 78.1% (n=32) | 80.0% (n=45) |
| BOS | 60.0% (n=5) | 69.4% (n=36) | 68.2% (n=22) | 68.6% (n=35) |
| MA_FADE | 50.0% (n=2) | 71.4% (n=28) | 72.2% (n=18) | 66.7% (n=27) |
| PULLBACK | 44.4% (n=36) | 57.0% (n=172) | 53.3% (n=120) | 56.9% (n=174) |

**PULLBACK is the standout case**: at baseline its holdout expectancy is
*negative* (−$0.88/trade, CI [−$5.38, $2.17]) — the worst of any trigger.
Under every relaxed variant it turns solidly positive: B $4.95 [$1.67,
$8.12], C1 $2.38 [−$0.07, $5.71] (barely still touches zero), C3 $10.17
[$3.43, $16.63]. PULLBACK is by far the highest-volume trigger (838 of
1,804 signals) — its baseline underperformance may have been an artifact of
being forced into whatever cheap, far-OTM scraps were left after the
premium filter, not a genuine lack of edge. Worth a dedicated look before
any promotion decision, independent of this experiment.

Normalized (% return on debit) confirms the win-rate story without the
position-size confound, pooling all four good triggers:

| | avg % return | median % return | win rate |
|---|---|---|---|
| A | 16.23% | 23.60% | 62.8% |
| B | 15.11% | 23.68% | **65.6%** |
| C1 | 13.92% | 23.00% | 63.9% |
| C2 | 13.52% | 23.69% | 65.8% |
| C3 | 11.61% | 23.01% | 63.1% |

---

## 4. SWEEP_RECLAIM — treated separately, not upweighted, not enabled live

**Direct answer to "does its poor fill rate remain after the premium
restriction is removed": no.** Fill rate goes from 2.9% at baseline to
89.9–95.7% under every relaxed variant — the premium band genuinely was the
binding constraint on SWEEP_RECLAIM specifically (confirms
`SELECTOR_REJECTION_AUDIT_v1.md`'s finding that its candidates price further
from the band than every other trigger).

**But at real sample size, its edge does not hold up — it gets weaker, not
stronger:**

| Variant | n filled (holdout) | win rate | expectancy | 95% CI |
|---|---|---|---|---|
| A_baseline | 2 | 50.0% | $61.40 | [−$4.10, $126.90] (2 sessions — meaningless) |
| B_delta_first_debit_cap | 62 | 41.9% | $2.59 | [−$4.92, $10.98] |
| C1_premium_0.20_0.50 | 15 | 46.7% | $13.10 | [−$2.41, $37.57] |
| C2_premium_0.20_1.00 | 63 | 41.3% | $2.00 | [−$5.50, $10.41] |
| C3_premium_0.20_2.50 | 66 | **37.9%** | **−$1.62** | [−$12.18, $8.85] |

Normalized (% return), pooled across all variants, SWEEP_RECLAIM's win rate
sits at **48–52% (a coin flip) with a negative median return (−13% to
−15%)** in every variant except C3 (+11.5% median, still not statistically
distinguishable from zero on the dollar basis above). At n=62–66 fills —
finally a real sample, not the 27-fill sliver from the frozen v2.2 run —
SWEEP_RECLAIM's win rate is now *below* 50% in every relaxed variant, and
C3's point estimate is outright negative. **This is more evidence against a
real edge, not less.** Per heff's instruction, SWEEP_RECLAIM's weight was
not increased and it was not evaluated for a live path in any variant here.
**Recommendation: continue excluding/gating SWEEP_RECLAIM regardless of
which policy (if any) is chosen for the other four triggers.**

---

## 5. $100 risk-cap verification (Variant B) — done as asked, not skipped

`total_debit_dollars(ask, qty, fee) = ask * 100 * qty + fee_per_contract *
qty` — never the quoted ask alone. Concrete boundary case in
`test_selector_policy_experiment.py`
(`test_fee_inclusion_is_the_deciding_factor_at_the_boundary`): an ask of
exactly $1.00 has a raw premium debit of exactly $100.00 (would pass a naive
"ask ≤ 1.00" check) — including the $0.05/contract fee (same regulatory-fee
approximation `bt2_fills.FillConfig` uses everywhere else in this codebase)
pushes it to $100.05, which correctly **fails** the cap. Two more
integration tests exercise the full pipeline (not just the helper function):
a $0.60-ask candidate that variant A rejects on premium band but variant B
accepts (debit $60.05, under cap), and the real $1.84-ask example from
`SELECTOR_REJECTION_AUDIT_v1.md` (debit $184.05), which variant B correctly
still rejects.

**Verified against the real 162-session run, not just synthetic tests**: of
1,731 candidates variant B *selected*, the maximum recorded total debit was
$99.05 — **zero cap violations at selection time.**

**One honest caveat found and worth stating plainly**: the cap is enforced
against the ask observed *at selection*. A handful of *filled* trades'
realized entry price drifted slightly past the target after the selection
decision (fill happens a few seconds to ~20s later, per the entry TTL) — the
maximum realized filled debit across all 1,636 Variant B fills was $117.00,
about 17% over the $100 target in the single worst case. This is a normal
consequence of quoting-to-fill latency, not a defect in the cap's
definition or check — the cap bounds the *decision*, not a guaranteed
execution price, exactly like every other selector rule in this codebase.
Flagged for transparency, not treated as a bug (no regression test was
written for this — it is not a code defect, it is disclosed execution
reality, same category as `bt2_fills`'s own documented slippage model).

---

## 6. Recommendation

**No live configuration change is made by this report.** This is
exploratory, single-pass evidence on a 32-session holdout — a genuine
improvement over how every earlier selector sweep in this codebase was
measured (all prior sweeps used the fully-mined 162-session set with no
holdout at all), but still far short of BT0_CHARTER's own 30-session
promotion bar being met on truly *fresh* data collected after this
experiment, and still just one holdout look, not a validated forward test.

**If heff wants to pursue this further, in order of how much change each
represents:**

1. **Lowest-risk first step — C1 (premium $0.20–$0.50).** Smallest position-
   size increase (1.5x baseline avg debit), the *best* profit factor of any
   variant tested (2.40), a contained drawdown increase (1.4x), and it
   requires touching exactly one existing `SelectorConfig` field
   (`premium_high`) with no new code path. Delta still runs well below the
   0.35 target (mean 0.23) — it does not solve the delta-vs-premium tension,
   it just loosens it a little.

2. **Delta target is structurally the better-supported governing
   constraint, if a bigger change is on the table — B (delta-first, $100
   debit cap), not C2/C3.** B gets closest to the actual 0.35 delta target
   of any variant, carries an explicit and now-verified per-trade risk
   bound, and produces holdout numbers statistically comparable to C2
   without C3's overshoot-the-target-delta and worst-profit-factor problems.
   This is a bigger behavioral change than C1 — selection rate jumps from
   14.8% to 96.2%, meaning the strategy would trade nearly every signal
   instead of roughly one in seven.

3. **Do not adopt C3 (premium $0.20–$2.50) as specified.** Worst profit
   factor, largest drawdown by a wide margin, delta overshoots its own
   target, and its high total-P&L headline is the variant most explained by
   bigger bet sizes rather than a better edge (Section 1).

4. **SWEEP_RECLAIM: do not enable or upweight under any variant.** Section 4
   is more evidence against a real edge than the original 27-fill sample
   was, not less.

5. **PULLBACK's baseline underperformance (Section 3) is worth a focused
   follow-up independent of this experiment** — it is the highest-volume
   trigger and its apparent edge recovery under every relaxed variant is the
   single largest swing in this whole report.

Whichever direction (if any) heff chooses, the honest next step per this
codebase's own established discipline is a **single frozen candidate, tested
once on genuinely fresh forward data** — not a second sweep over this same
162-session set, and not a decision made from Section 2's raw dollar
totals alone.

---

## Appendix — safety posture unchanged

No cron installed, no `--arm` added anywhere, no broker order placed,
cancelled, or modified, no `SelectorConfig` value changed in any file that
governs production selection (`live_heff_smc_selector.py`'s
`LIVE_SELECTOR_CONFIG` and `bt2_selector.SelectorConfig`'s defaults are
untouched). All simulation in this experiment runs through
`thetadata_pipeline/selector_policy_experiment.py`, a new, separate,
read-only module — never through the live or armed path. Verified `git
status` after this experiment shows exactly the new files listed in Section
0 plus the pre-existing dirty tree already disclosed before this session
began. Nothing was committed until after every test passed; nothing was
pushed.
