# VARIANT_B_NO_SWEEP — frozen forward candidate identity

The forward candidate is named **VARIANT_B_NO_SWEEP**. Use this name in
every report, dashboard label and Telegram message. Do not call it
"Variant B" without qualification, and do not call it "delta-first."

## Definition

| property | value |
|---|---|
| selection logic | exact Variant B (`bt2_selector.select_contract`, unmodified) |
| primary ranking key | `spread_pct_mid` ascending |
| tie-break | `abs(abs(delta) − 0.35)` ascending |
| target delta | 0.35 |
| delta floor | `min_abs_delta` 0.15 |
| premium band | disabled (`premium_low=0.0`, `premium_high=inf`) |
| debit gate | `ask × 100 × qty + fees ≤ $100`, a **gate**, not a ranking key |
| quantity | 1 |
| allowed DTE | {0, 1, 2} |
| SWEEP_RECLAIM | **excluded upstream, before a book is ever built** |
| MSS / BOS / MA_FADE / PULLBACK weights | untouched |

Accurate short description: **delta-targeted with a $100 debit cap.**
Behavioral parity with the tested Variant B is proven — 1,492/1,492 eligible
decisions and 1,804/1,804 across all triggers, 0 mismatches
(`VARIANT_B_DIFFERENTIAL_PARITY.json`).

## Why the name matters

Variant B's published totals were computed over **all 1,804** historical
signals, SWEEP_RECLAIM included. VARIANT_B_NO_SWEEP trades **1,492** of
them. Quoting Variant B's combined totals as if they described the forward
candidate would misstate it by construction — a different signal mix, 17.3%
smaller.

## Descriptive reference

Produced by filtering the **already-created** Variant B rows
(`selector_policy_experiment_v1`, run `20260801T180703Z-bbfc8c`,
`raw_rows.json`) to remove SWEEP_RECLAIM. No parameter changed, no sweep
run, nothing re-optimized — a filter and a recount of one existing result.

| candidate | segment | signals | fills | fill % | win % | mean $/trade | 95% CI | total $ | PF |
|---|---|---:|---:|---:|---:|---:|---|---:|---:|
| Variant B (as published) | train | 1432 | 1291 | 90.1% | 63.4% | 8.03 | 6.91 – 9.15 | 10,369.90 | 2.47 |
| **VARIANT_B_NO_SWEEP** | train | 1189 | 1073 | 90.2% | 66.0% | **8.84** | 7.70 – 9.98 | 9,481.70 | 2.79 |
| Variant B (as published) | holdout | 372 | 345 | 92.7% | 60.3% | 7.64 | 5.01 – 10.29 | 2,636.50 | 2.17 |
| **VARIANT_B_NO_SWEEP** | holdout | 303 | 283 | 93.4% | 64.3% | **8.75** | 6.11 – 11.58 | 2,475.70 | 2.52 |
| Variant B (as published) | ALL | 1804 | 1636 | 90.7% | 62.7% | 7.95 | 6.93 – 9.00 | 13,006.40 | 2.40 |
| **VARIANT_B_NO_SWEEP** | ALL | 1492 | 1356 | 90.9% | 65.6% | **8.82** | 7.76 – 9.91 | 11,957.40 | 2.73 |

Trigger composition of VARIANT_B_NO_SWEEP: PULLBACK 838, MSS 319, BOS 168,
MA_FADE 167.

## How to read these numbers — and how not to

**This is descriptive, not validated.** Three caveats, stated plainly:

1. **The exclusion was informed by prior analysis of SWEEP_RECLAIM's poor
   performance** (`selector_policy_stats.py`, `selector_rejection_audit.py`).
   Removing a cohort already known to be weak and then reporting the
   improvement is, unavoidably, selection-affected. The apparent lift
   (mean $7.95 → $8.82, win 62.7% → 65.6%, PF 2.40 → 2.73) should be read as
   *what the remaining population did*, not as an independently earned edge.

2. **The holdout has now been described twice** — once for Variant B, once
   for this subset. That is a re-description of the same rows, not a second
   selection event, but it does mean the holdout is spent for this candidate
   family. A genuinely fresh read requires the forward window.

3. **Train and holdout are never pooled here.** The ALL row is shown only
   because it is the number people will otherwise compute themselves; the
   segment rows are the honest ones.

The forward test exists precisely because none of the above is validation.
Nothing in this table authorizes an expectation for the forward window.

## Frozen — do not tune

Per the standing instruction: the indicator and selector are not to be
tuned during the forward window, and this candidate must not be optimized
against the 162-session dataset, which is now development data. Any change
to the table above makes it a **new candidate** needing its own
pre-registered treatment.
