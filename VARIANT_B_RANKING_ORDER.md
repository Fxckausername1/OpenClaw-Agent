# Variant B — actual contract ranking order

## Differential parity result (the gate)

`smc/selector_variant_b.py` was run against the exact historical inputs of
`selector_policy_experiment_v1` Variant B — same signals, same
point-in-time book construction, one shared book object handed to both
implementations per decision.

| measure | value |
|---|---|
| sessions replayed | 162 (**0 skipped**) |
| total decisions, all triggers | 1,804 |
| eligible decisions (SWEEP_RECLAIM removed) | 1,492 |
| **exact matches, eligible** | **1,492 / 1,492** |
| mismatches, eligible | **0** |
| exact matches, ALL triggers incl. SWEEP_RECLAIM | **1,804 / 1,804** |
| selection-vs-rejection mismatches | 0 |
| OCC mismatches | 0 |
| rejection-reason mismatches | 0 |
| contract-field mismatches (strike/expiry/right/delta/bid/ask/spread/debit/DTE/quote-age/ask-size) | 0 |
| candidates-checked mismatches | 0 |
| monkeypatch leaks | 0 |
| configs byte-identical | yes |

**PARITY_PASS: true.** No mismatch category has a first example, because no
category has any members. Full record:
`VARIANT_B_DIFFERENTIAL_PARITY.json`.

This is the tested Variant B, not a new B2. It is cleared to connect to
Alpaca PAPER.

### One scope difference that parity does NOT cover

The live module excludes SWEEP_RECLAIM before selection; **the original
Variant B experiment did not.** SWEEP_RECLAIM was only analyzed *post hoc*
(`selector_policy_stats.py`), never excluded from the run that produced
Variant B's numbers.

So: the *decision logic* is proven identical (and identical on
SWEEP_RECLAIM signals too — all 1,804 match). What differs is the
**population** the live selector will act on: 1,492 of 1,804 historical
decisions, 17.3% fewer. Variant B's historical performance figures were
computed over the full 1,804. Any forward expectation carried over from
that backtest is an expectation for a *different signal mix* than the one
that will actually trade.

That is a real caveat about what the historical numbers mean — not a
defect in the implementation, and not something the parity run can settle.


Status: authoritative description of what the frozen forward candidate
actually does. Written 2026-08-01 in response to heff's correction that
calling Variant B "delta-first" overstates the role delta plays.

## The correct name

**"Delta-targeted with a $100 debit cap."**

NOT "delta-first contract ranking." Delta does not rank candidates first.
It is the *tie-break* on a ranking whose primary key is spread percentage.

The existing internal identifier `B_delta_first_debit_cap` (in
`thetadata_pipeline/selector_policy_experiment.py`) is therefore a
misleading name for the policy it selects. That identifier is left
UNCHANGED on purpose — it is the key under which the historical result was
produced and reported, and renaming it would break the link between the
frozen candidate and its evidence. The name is wrong; the behavior it
names is what was tested, and the behavior is what we freeze.

## The actual order

Selection is two distinct phases. Conflating them is what produces the
"delta-first" misreading: delta appears in *both* phases, but only as a
floor in the first and only as a tie-break in the second.

### Phase 1 — pass/fail gates (`bt2_selector.evaluate_candidate`)

A candidate must clear **all** of these. They are not ordered relative to
each other; every failing reason is collected, never short-circuited after
the first.

1. **Quote validity** — a genuine short-circuit, evaluated before anything
   else, returning immediately: bid and ask both present, both > 0, and
   bid < ask. A missing, zero, crossed or locked quote produces
   `no valid two-sided quote (missing/zero/crossed)` and no other reason,
   because no other rule can be meaningfully evaluated without a quote.
2. **DTE** in `{0, 1, 2}`.
3. **Premium band** — `premium_low <= ask <= premium_high`. **Disabled in
   Variant B** (`premium_low=0.0`, `premium_high=inf`), so it never
   rejects. This is the single change Variant B makes to the baseline
   config.
4. **Total debit cap** — `ask * 100 * qty + fee * qty <= $100.00`,
   fees included. This is Variant B's *added* gate, applied by wrapping
   `evaluate_candidate`. It is a **gate, not a ranking key** — it decides
   admissibility only and never orders one admissible candidate above
   another.
5. **Absolute spread** — `spread <= $0.05`.
6. **Relative spread** — `spread / mid <= 15%`.
7. **Delta floor** — `|delta| >= 0.15`. Note this is a *floor*, not a
   target: it rejects far-OTM contracts. Being nearer 0.35 buys a
   candidate nothing here.
8. **Quote age** — `<= 10s`.
9. **Ask size** — `>= 5`.

### Phase 2 — ranking of the survivors (`bt2_selector._quality_key`)

```python
def _quality_key(row):
    return (row["spread_pct_mid"], row["_delta_distance"])
```

`min()` over that tuple. Lower is better. So:

1. **`spread_pct_mid` ascending — PRIMARY.** The tightest relative spread
   wins outright.
2. **`|abs(delta) - 0.35|` ascending — TIE-BREAK ONLY.** Consulted *only*
   when two or more survivors have an identical `spread_pct_mid`.

`bt2_selector`'s own docstring already states this plainly: *"Spread-pct
primary … then closeness to target_delta as the documented tie-break."*
The behavior and its documentation were always consistent. The
`delta_first` label was the outlier.

### Consequence worth internalizing

Two candidates with the same *dollar* spread can have different
`spread_pct_mid` if their mids differ, and the cheaper one wins on
percentage regardless of which is nearer 0.35 delta. Delta target only
breaks an exact percentage tie. In practice this means the selected
contract is chosen predominantly on execution quality, with delta acting
as a coarse admissibility band (the 0.15 floor) plus a rarely-invoked
tie-break — not as the selection objective.

## How often the tie-break actually decides anything

Measured directly by `variant_b_differential_parity.py`, which recomputes
the ranked set per decision (`select_contract` pops `_delta_distance` off
the winner and never exposes the ranking, so this had to be reconstructed).

Across the full 162-session frozen dataset:

| measure | value |
|---|---|
| decisions with at least one passing candidate | 1,731 |
| decisions where the delta tie-break decided the winner | **2** |
| share of decisions delta actually decided | **0.12%** |
| decisions with a full-tuple tie (order-dependent) | **0** |

**In 1,729 of 1,731 decisions the contract was chosen on spread percentage
alone.** Delta closeness altered the outcome twice in the entire history.

This is the concrete reason "delta-first" is the wrong name: under that
label a reader would reasonably expect delta to be the dominant selection
criterion, when it is in practice a near-inert tie-break. The 0.15 |delta|
floor in Phase 1 does real work (it excludes far-OTM contracts); the 0.35
target in Phase 2 almost never does.

The zero full-tuple ties is a separate, useful result: `min()` returns the
first minimum it encounters, so had any decision produced two candidates
identical on *both* keys, the winner would have depended on book row order
— a hidden nondeterminism. None exist in 1,731 decisions, so selection is
order-independent on this dataset. That is an empirical observation about
this data, not a proof about future data; a live book could in principle
produce one.

## What was NOT done, deliberately

heff's explicit instruction: *"Do not create and evaluate a new true-
delta-first ordering against the viewed holdout data during this build.
Preserve the exact tested B implementation for the frozen forward
candidate."*

Accordingly:

- `_quality_key` is **unchanged**. No reordering was made, considered-and-
  applied, or silently "fixed."
- No alternative ordering was constructed, scored, or compared against the
  holdout split. The holdout has been viewed once, for Variant B as
  tested; evaluating a new ordering against it would spend that budget a
  second time and turn a locked comparison into a fishing expedition.
- If a true delta-first ordering is ever wanted, it is a **new candidate**
  requiring its own pre-registered train/holdout treatment — not an edit
  to this one.
