# SELECTOR_REJECTION_AUDIT_v1

Read-only audit. Nothing armed, no cron installed, no selector policy changed,
no broker state touched. One demonstrated implementation defect was fixed
(diagnostic-only, documented in Section 7). All numbers below are development-
window data — BT0_CHARTER.md's development window closed 2026-07-30 — and are
explicitly **not promotable and not out-of-sample**.

## 0. Identity — what this audit describes, pinned exactly

| | |
|---|---|
| Frozen run being audited | `bt3_b1_heff_smc_indicator_only` run `20260801T043948Z-a51b7f` (the 406-fill / $4.37 point-in-time+admission definition — **not** the historical 278-fill / $6.19 definition) |
| git commit | `37ed48d6931a3b3b7d9af3f1169c8f4443fc7fa0` |
| Indicator | v2.2, sha256 `360aeadf18f6c3247307fe54d154994d84f8dbd05c487a9ceeebc73c4f04fb76` |
| Events file | `data/thetadata/heff_smc_replay/triangle_events_v2.2.json` (1,804 events) |
| This audit's run | `selector_rejection_audit_v1` run `20260801T143930Z-88601a` |
| Command | `./venv/bin/python run_selector_audit.py` |
| Code hashes | Identical to the frozen run's manifest for all 6 CODE_FILES (bt2_simulator/bt2_fills/bt2_exits/bt2_selector/heff_smc_engine/heff_smc_replay) — checked programmatically before any output was trusted, see `assert_frozen_inputs_unchanged()` |
| **Parity** | **CONFIRMED** — reproducing the run through instrumented code gave the exact same n_total_signals/n_no_contract/n_admission_rejected/n_contract_pass_no_fill/n_filled/n_sessions_with_signal/n_sessions_with_fill/expectancy/profit_factor/win_rate as the frozen `report.json` (tolerance 1e-6), plus total_net_pnl within 1c. See `parity.json`. |

Tooling (new, reusable, tested):

- [`thetadata_pipeline/selector_rejection_audit.py`](../thetadata_pipeline/selector_rejection_audit.py) — the analysis module (44 unit tests, `thetadata_pipeline/tests/test_selector_rejection_audit.py`)
- `run_selector_audit.py` — runner, produces an isolated run directory via `bt_run.new_run` (never appends to an existing ledger)
- `trace_v22_touched_signals.py` — one-off join script for Section 4

Run artifacts (`data/thetadata/runs/selector_rejection_audit_v1/20260801T143930Z-88601a/`):
`manifest.json`, `parity.json`, `funnel.json`, `per_signal.json` (1,804 rows), `candidates_index.json` (every candidate evaluated for every none-pass signal, 33MB), `counterfactual_gate_sensitivity.json`, `trigger_comparison_with_ci.json`, `v22_diff_detail.json`, `v22_touched_signals_traced.json`.

**How the per-signal detail was recovered.** The production ledger only ever
records a NO_CONTRACT signal's *coarse* outcome
(`data_quality = "no_candidates_in_book"` or `"no_candidate_passed_all_rules"`).
`bt2_selector.evaluate_candidate` already computes every failing rule for
every candidate and never short-circuits — it just was not persisted. This
audit monkeypatches `bt2_selector.evaluate_candidate` to a wrapper that calls
the real function **unchanged** (so every selection/fill/admission/PnL number
is byte-identical to the frozen run — see parity above) and additionally
stashes a copy of each candidate's full reasons list. Selector logic itself
was never edited for this purpose.

---

## 1. Headline funnel — all 1,804 signals

| Outcome | n | % of total |
|---|---|---|
| FILLED | 406 | 22.5% |
| NO_CONTRACT | 1,385 | 76.8% |
| ADMISSION_REJECT | 13 | 0.7% |
| NO_FILL (contract passed, never filled) | 0 | 0.0% |

### First rejection reason, grouped (of the 1,398 non-filled signals)

| Group | n | % of non-filled | % of all 1,804 |
|---|---|---|---|
| **Premium limit** (ask outside $0.20–$0.30 band) | **1,326** | **94.8%** | **73.5%** |
| Missing chain data (no candidates in book at all) | 33 | 2.4% | 1.8% |
| Stale/incomplete quotes (no valid two-sided quote) | 18 | 1.3% | 1.0% |
| Admission/risk policy | 13 | 0.9% | 0.7% |
| Strike/delta failure | 7 | 0.5% | 0.4% |
| Liquidity (ask size) | 1 | 0.1% | 0.1% |
| Spread ($ or %) | 0 | 0.0% | 0.0% |
| Expiration (DTE) | 0 | 0.0% | 0.0% |

**The premium band dwarfs every other rejection cause combined by roughly
20:1.** Spread and DTE never reject a single candidate in this dataset.

### Concrete illustration (2025-12-04 09:31:00, PUT WATCH, SWEEP_RECLAIM)

24 candidates were checked. The best one (0DTE, strike 624 vs underlying
623.53, delta −0.54, spread 1.6%, ask size 61, quote age 0.28s — passes
*every other rule cleanly*) failed on exactly one: `ask $1.84 outside
preferred premium band $0.20-$0.30`. This is the modal case, not an outlier
(see Section 3).

---

## 2. Category taxonomy — separating data issues, policy, and defects

| Category | Root cause | Verdict |
|---|---|---|
| **Premium limit** (1,326) | `SelectorConfig.premium_low=0.20 / premium_high=0.30`. Its own docstring: *"an initial research judgment call, not yet calibrated... Revisit once real trades exist to study."* | **Intentional policy threshold**, not a bug — see Section 3 for why it dominates so completely |
| **Missing chain data** (33) | **All 33, without exception, fire at exactly 09:30:00** — the literal opening bar. `build_point_in_time_book` is strictly point-in-time (`trade_timestamp <= decision_ts`); at the instant of the opening bell, no options trade can yet exist in the historical record. | **Genuinely untradable, not a defect.** Structural consequence of a no-lookahead book at the first bar of the session, already documented in `build_point_in_time_book`'s own docstring. |
| **Stale/incomplete quotes** (18) | 13/18 in the final 15 minutes of session (15:45–15:59 ET), the rest scattered in the last 90 minutes. Real end-of-day thinning in specific OTM 0–2DTE contracts. | **Genuinely untradable, not a defect.** Time clustering is consistent with real late-session illiquidity, not a parsing bug. |
| **Admission/risk policy** (13) | All 13 are `SAME_CONTRACT_ALREADY_OPEN` — the canonical `AdmissionPolicy`/`OpenBook` dedup already exercised and tested elsewhere in this codebase. | **Intentional risk policy**, working as designed |
| **Strike/delta failure** (7) | Real candidates with valid quotes, delta below the 0.15 floor or unresolvable. | **Genuinely untradable** (n=7, too small to investigate further) |
| **Liquidity/ask size** (1) | Single occurrence. | **Genuinely untradable** (n=1) |
| **Implementation errors found** | One — see Section 7 | **Fixed, with a regression test** |

No demonstrated defect was found in `bt2_selector.py`, `bt2_simulator.py`,
`bt2_fills.py`, or the point-in-time book construction. The one real defect
found was in a different, diagnostic-only module (Section 7).

---

## 3. Why premium band dominates so completely

For every premium-band-rejected signal, the audit re-derived the actual ask
of the *least-bad* candidate (fewest failing rules, same tie-break
`bt2_selector._quality_key` uses) — not just that it failed, but by how much
and in which direction.

| | n | median ask | below $0.20 | **above $0.30** |
|---|---|---|---|---|
| SWEEP_RECLAIM | 281 | **$2.38** | 0.4% | **99.6%** |
| All other triggers | 1,045 | **$1.82** | 2.2% | **97.8%** |

**Rejections are overwhelmingly because premiums are too *high*, not too
low** — by roughly 6–8x the $0.30 ceiling, not a narrow miss. This is the
mechanical explanation: `SelectorConfig` simultaneously wants `target_delta
= 0.35` (moderately-close-to-the-money) and `allowed_dte = {0,1,2}`, but QQQ
trades at ≈$600–625 through this sample. A 0–2DTE, ~0.35-delta option on an
underlying at that price level costs low single-digit dollars in the modal
case — the $0.20–$0.30 band can only be hit by contracts far enough OTM to
also usually be below the 0.15 delta floor. **The premium band and the
delta target are close to mutually exclusive at QQQ's current price level
and DTE range**, which is exactly what the 98.0% counterfactual-gate number
in Section 5 measures.

This is stated as a mechanical observation, not a recommendation — Section 8
turns it into a single bounded, preregistered hypothesis rather than a
selector change made here.

---

## 4. The 25/32/30/6 signals v2.2 touched, traced individually

**Correction to the prior framing.** The frozen run's manifest delta says
`n_signals: +25` and the handoff describes "32 new v2.2 signals, all landed
in SWEEP_RECLAIM, none filled." Both are directionally right but imprecise.
A key-exact diff (`session, bar_index, side, time`) against v2.1's own event
file resolves it exactly:

| | n | Detail |
|---|---|---|
| Genuinely new (session/bar/time never fired in v2.1 at all) | **30** | All 30 are SWEEP_RECLAIM |
| Reclassified (same session/bar/time, different trigger label) | **6** | 3× MA_FADE→SWEEP_RECLAIM, 3× PULLBACK→SWEEP_RECLAIM |
| Removed (fired in v2.1, gone entirely from v2.2) | **5** | 4× SWEEP_RECLAIM, 1× PULLBACK |

Net signal-count delta: 30 new − 5 removed = **+25** (matches the manifest
exactly). Net SWEEP_RECLAIM-trigger delta: 30 new + 6 reclassified-in − 4
removed = **+32** (matches the handoff's "32" exactly — that number was the
trigger-count delta, not a literal set of 32 new events). 280 → 312
SWEEP_RECLAIM, 842 → 838 PULLBACK, 170 → 167 MA_FADE reconcile exactly
against this decomposition.

**Traced outcome for every one of the 36 touched events** (0 lookup misses
against `per_signal.json`):

- **All 30 genuinely-new signals: NO_CONTRACT, primary_category =
  `premium_band`.** Confirms the handoff's "none filled" claim for this
  subset.
- **5 of 6 reclassified signals: NO_CONTRACT, `premium_band`.**
- **1 of 6 reclassified signals actually FILLED** — 2026-01-06 09:42:00,
  QQQ, long, score 8.0, **net_pnl = $10.90**. This is the one correction to
  "none filled": a signal that was PULLBACK in v2.1 and became SWEEP_RECLAIM
  in v2.2 did get a contract and a real fill under its new label. It is
  already counted inside the 27 SWEEP_RECLAIM fills reported everywhere
  else in this audit — nothing double-counts.
- The 5 truly-removed events (4 SWEEP_RECLAIM, 1 PULLBACK) no longer exist
  as signals in v2.2 at all — not part of the 1,804-signal set, so they have
  no selector record. Listed for completeness, not evaluated further.

---

## 5. SWEEP_RECLAIM vs. every other trigger

| Trigger | n signals | n filled | fill rate | win rate (Wilson 95% CI) | expectancy (session-bootstrap 95% CI) |
|---|---|---|---|---|---|
| MSS | 319 | 94 | 29.5% | 81.9% [72.9%, 88.4%] | $7.00 [$4.98, $9.23] |
| BOS | 168 | 35 | 20.8% | 71.4% [55.0%, 83.7%] | $6.41 [$3.17, $9.93] |
| MA_FADE | 167 | 40 | 24.0% | 67.5% [52.0%, 79.9%] | $8.38 [$3.40, $14.59] |
| PULLBACK | 838 | 210 | 25.1% | 51.9% [45.2%, 58.6%] | $1.59 [$0.26, $2.92] |
| **SWEEP_RECLAIM** | **312** | **27** | **8.7%** | **48.2% [30.7%, 66.0%]** | **$8.34 [−$1.41, $21.79]** |

**SWEEP_RECLAIM is the only trigger whose 95% expectancy CI crosses zero.**
Every other trigger's interval is entirely positive; SWEEP_RECLAIM's
lower bound is negative. The attractive $8.34 point estimate is built on 27
trades and is not statistically distinguishable from a losing or breakeven
edge yet — exactly the "not conclusive" framing the task asked to confirm,
now with a number behind it.

**Why the fill rate is so much lower than its peers, specifically:**
premium-band rejects 90.1% of *all* SWEEP_RECLAIM signals (281/312) vs.
61.4%–73.7% of all signals for the other four triggers — SWEEP_RECLAIM's
own candidates run structurally more expensive (median $2.38 vs $1.82,
Section 3) than the other triggers' candidates. This audit does not
establish *why* SWEEP_RECLAIM's setups price further from the band — that
would require reasoning about the indicator's own structural logic, out of
scope here — only that they measurably do.

Rejection-group breakdown by trigger (non-filled signals only), for
completeness:

| Trigger | premium_limit | missing_chain_data | stale_quotes | admission | delta | ask_size |
|---|---|---|---|---|---|---|
| MSS | 196 | 19 | 7 | 2 | 1 | 0 |
| BOS | 118 | 8 | 1 | 1 | 5 | 0 |
| MA_FADE | 123 | 2 | 1 | 1 | 0 | 0 |
| PULLBACK | 608 | 4 | 6 | 8 | 1 | 1 |
| SWEEP_RECLAIM | 281 | 0 | 3 | 1 | 0 | 0 |

---

## 6. One-rule-at-a-time counterfactual sensitivity (exploratory, non-promotable)

**Scope, stated plainly.** This answers "would at least one already-checked
candidate clear contract *selection*" for the 1,352 NO_CONTRACT signals
where candidates were actually evaluated (excludes the 33
missing-chain-data signals — there was nothing to relax). It does **not**
simulate the resulting admission/fill/exit cascade or PnL; that would need
one full 162-session chronological re-simulation *per rule* (new OpenBook
state, new entry/exit fill checks), i.e. 7 more full replays. This
single-core, RAM-constrained box was not asked to do that in one sitting —
a disclosed limitation, not a hidden one. Never combines relaxations; each
rule tested independently against the same fixed candidate set.

| Rule relaxed alone | Signals that would clear selection | % of 1,352 eligible |
|---|---|---|
| **Premium band** | **1,325** | **98.0%** |
| Delta floor | 875 | 64.7% |
| Quote age | 28 | 2.1% |
| Ask size | 7 | 0.5% |
| DTE | 0 | 0.0% |
| Spread ($) | 0 | 0.0% |
| Spread (%) | 0 | 0.0% |

Premium band and delta floor overlap heavily (many candidates fail both
simultaneously, consistent with Section 3's mechanical explanation) — this
is why relaxing delta alone (64.7%) recovers less than premium alone
(98.0%), not an inconsistency.

---

## 7. Implementation defect found and fixed

**File:** `live_heff_smc_selector.py`, `_near_miss_diagnostics` (the live
shadow selector's own near-miss classifier, the exact pattern this audit's
categorization follows per the original task brief).

**Defect:** the reason-classification `if/elif` chain has no branch for
`evaluate_candidate`'s `"no valid two-sided quote (missing/zero/crossed)"`
reason, so it silently fell into the generic `"other"` bucket —
indistinguishable from a genuinely unclassified future rule, defeating the
function's own stated purpose ("*so a 'no_candidate_passed_all_rules' day is
diagnosable ... instead of a black box*"). **Diagnostic-only** — it cannot
affect any PASS/FAIL selection, fill, or trading decision; `select_contract`
itself was never touched.

Not yet observed in the live shadow log (16 decisions logged as of
2026-08-01, none hit this path) — demonstrated directly against the
function, not inferred from production behavior.

**Regression test** (`test_live_heff_smc_selector.py`, new file):
3 tests. `test_no_two_sided_quote_gets_its_own_bucket_not_other` and
`test_crossed_quote_also_classified_correctly` both **failed** against the
pre-fix code (`AssertionError: None != 1`); a third,
`test_genuinely_unclassified_reasons_still_fall_to_other`, confirms the
catch-all bucket still exists for text the classifier truly doesn't
recognize. All 3 pass after the fix.

**Fix:** one 3-line branch added at the top of the existing `if/elif` chain,
functionally identical in shape to this audit's own `classify_reason()`.
Backed up to `live_heff_smc_selector.py.bak_20260801_nearmiss_othercategory`
before editing.

**Verified no regression:** full suites re-run after the fix —
`thetadata_pipeline/tests`: **425/425 passing** (381 baseline + 44 new from
this audit's own module). `smc/tests`: **112/112 passing**, unchanged.

No other implementation defect was found. Everything else in Sections 2–6
is either intentional policy (premium band, admission dedup) or genuine
market microstructure (open-bell/end-of-day data timing).

---

## 8. Preregistered hypotheses for one-time forward testing

Per BT0_CHARTER.md, the 162-session development window closed 2026-07-30 —
these 162 sessions have already been mined by B0, B1, a 30-variant indicator
sweep, and two selector sweeps. Testing either hypothesis again on this same
history would repeat the nested-selection error that got the
`$0.20–$1.00` selector promotion withdrawn. **Both must be tested exactly
once, on untouched forward data collected after this audit, with the
criteria below locked in before that data is viewed.** Neither is
implemented or run here.

### Hypothesis 1 — premium band is miscalibrated against the delta target, not just "narrow"

**Rule change to test:** raise `premium_high` (exact new value to be chosen
by heff, informed by Section 3's $1.82–$2.38 median — e.g. $2.50 or $3.00,
not swept) while leaving `target_delta`, `min_abs_delta`, and every other
`SelectorConfig` field untouched. One frozen candidate value, not a grid.

**Success criterion (fixed before viewing forward data):** on the forward
window, fill rate for signals with a same-side candidate available rises
materially (pre-specify: ≥1.5x the current ~22.5% aggregate fill rate) AND
the session-bootstrap 95% CI on net expectancy for the raised-band variant
does not drop its lower bound below the current variant's lower bound by
more than friction/spread would predict.

**Failure criterion:** fill rate does not materially improve (premium was
not actually the binding constraint out-of-sample), OR expectancy's lower
bound goes negative (the wider band is admitting genuinely worse trades, not
just more of the same edge).

### Hypothesis 2 — SWEEP_RECLAIM's expectancy is not yet distinguishable from zero

**No rule change** — this is a data-collection hypothesis, not a selector
change. Continue running SWEEP_RECLAIM signals through the existing,
unmodified selector on forward data.

**Success criterion (fixed before viewing forward data):** once SWEEP_RECLAIM
accumulates n≥30 forward fills (BT0_CHARTER's own minimum-sample bar), its
session-bootstrap 95% CI lower bound on expectancy is positive.

**Failure criterion:** at n≥30 forward fills, the CI lower bound is still
≤$0. SWEEP_RECLAIM should not be treated as a validated edge — reweighting
or promoting it further would be acting on Section 5's $8.34 point estimate
without the statistical support this section shows it currently lacks.

Both hypotheses are exploratory only. Neither result, whichever direction it
comes back, authorizes a selector or cron change on its own — that remains
heff's decision, same as every other promotion gate in this codebase.

---

## Appendix — safety posture unchanged

No cron installed, no `--arm` added anywhere, no broker order placed,
cancelled, or modified, no selector rule value changed. The one code change
in this audit (Section 7) is a diagnostic-classification fix in a shadow-only
module that never places orders; it does not touch `bt2_selector.py`,
`select_contract`, admission policy, or any armed/armable path. Verified
`git status` after this audit shows exactly one modified tracked file
(`live_heff_smc_selector.py`, the Section 7 fix), this audit's own new files
(`thetadata_pipeline/selector_rejection_audit.py`,
`thetadata_pipeline/tests/test_selector_rejection_audit.py`,
`run_selector_audit.py`, `trace_v22_touched_signals.py`,
`test_live_heff_smc_selector.py`), a gitignored backup
(`live_heff_smc_selector.py.bak_20260801_nearmiss_othercategory`, matched by
the repo's existing `*.bak_*` ignore rule), plus the pre-existing unrelated
dirty tree already present and disclosed in `HANDOFF_2026-08-01.md` before
this audit began — nothing else was touched. Nothing was committed or
pushed.
