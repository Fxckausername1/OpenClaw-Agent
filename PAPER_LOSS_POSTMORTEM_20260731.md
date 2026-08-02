# Paper-loss postmortem — 2026-07-31

Read-only operational attribution of the 10 Alpaca PAPER entries placed on
2026-07-31. **Not an optimization input.** Nothing in this document tunes
Variant B, and no parameter was changed as a result of it.

Sources: Alpaca PAPER order history (authoritative for fills),
`phase2_shadow_decisions.jsonl` (selected contract, delta, quotes at
decision), `phase3_entered.json`, `triggers.jsonl`, and the executor /
exit-manager logs.

---

## Headline

**The realized SMC options total was −$114 gross of fees, not −$101.** The
−$101 figure in circulation appears to predate the final position being
closed. Reconciled against Alpaca: 10 buys totalling $716, 10 sells
totalling $602.

Equity symbols in the same paper account that day (APO, CHRW, CSGP, FTNT,
NDAQ, SBUX, VRT, XEL) belong to other strategies and are excluded
throughout.

**The selector that placed these trades was not Variant B.** During the
session `LIVE_SELECTOR_CONFIG` was `wide_premium_020_100` (premium
$0.20–$1.00); it was reverted to `moderate_combo` (`min_abs_delta=0.10`,
premium $0.15–$0.40) only later that day as part of the P0 containment.
Neither is Variant B (`min_abs_delta=0.15`, `target_delta=0.35`, no premium
ceiling, $100 debit cap). Six of the ten fills carry |delta| below 0.25 and
one is 0.1333, which **Variant B's 0.15 delta floor would have rejected
outright**.

Consequently: **these ten trades say nothing about VARIANT_B_NO_SWEEP's
expected performance.** They are an execution-path postmortem, not evidence
about the frozen candidate.

---

## Attribution summary

| classification | trades | net |
|---|---:|---:|
| Normal strategy win (worked as designed) | 3 | **+$109** |
| Normal strategy loss (stop worked as designed) | 1 | **−$8** |
| Execution / slippage problem | 2 | **−$56** |
| State-machine defect | 4 | **−$159** |
| Data-quality problem | 0 | $0 |
| Unknown | 0 | $0 |
| **total** | **10** | **−$114** |

**The strategy's own decisions were net positive (+$101 across the four
trades whose lifecycle behaved correctly). Defects cost −$215.** The loss
is an execution and state-management story, not a selection story.

---

## Per-trade detail

Delta and quotes are as recorded at the selection instant. Entry slippage is
signed relative to the submitted limit (negative = filled better than
limit). All contracts were 0DTE.

### 1. QQQ260731C00700000 — MSS long — **−$19** — execution/slippage
- Signal `2026-07-31:1950:long`, bar 09:30:00 ET, score n/a
- Selected: strike 700 C, **delta 0.2014**, bid/ask 0.70/0.71, quote age 0.0s
- Entry: limit 0.71 → **filled 0.64** (−0.07, favorable) at 13:36:47Z
- Exit: STOP at 13:38:34Z, `entry=0.64 bid=0.44` — **−31% at detection**
- Exit fill 0.45. Intended exit fired: yes. Reconciliation: correct. No duplicate/stale/late order.
- **Why classified execution:** the −20% stop was detected only once price
  had already fallen 31%, 107s after entry. The stop level itself was never
  the problem; the 2-minute exit-manager cadence was.

### 2. QQQ260731C00695000 — SWEEP_RECLAIM long — **−$35** — state-machine defect
- Selected: strike 695 C, **delta 0.1547**, bid/ask 0.60/0.61
- Entry: limit 0.61 → **filled 0.53** (−0.08) at 13:55:02Z
- Exit: STOP first detected 14:04:52Z at `bid=0.25` — **−53%, 590s after entry**
- Then **six** exit attempts in 10 minutes, each re-priced at the new lower bid:
  0.25 → 0.22 → 0.19 → 0.18 → 0.19 → 0.16, five cancelled on the "did not
  fill within 2.0s buffer, left OPEN for next tick" path
- Final fill 0.18. Realized **−66% against a −20% stop.**
- **Why classified state-machine:** the deferral loop is the defect. A sell
  limit resting at the displayed bid did not fill within 2s because the bid
  was a lagging indicative quote; the position was then handed to the next
  tick ~2 minutes later and re-priced *down*, repeatedly. The mechanism
  chases the market by construction.

### 3. QQQ260731C00693000 — SWEEP_RECLAIM long — **−$32** — state-machine defect
- Selected: strike 693 C, **delta 0.2316**, bid/ask 0.97/0.99
- Entry: limit 0.99 → **filled 0.72** (−0.27, a 27¢ gap) at 14:01:34Z
- Exit: STOP at 14:04:58Z, `entry=0.72 bid=0.39` — **−46% at detection**
- Exit fill 0.40
- **Note the entry gap:** the limit was set 27¢ above where the contract
  actually traded, i.e. priced off a quote already stale at submission. In
  PAPER this was harmless (filled better); live it is an invitation to pay
  up to the limit.

### 4. QQQ260731C00692000 — SWEEP_RECLAIM long — **−$8** — normal strategy loss
- Selected: strike 692 C, **delta 0.1333** *(would fail Variant B's 0.15 floor)*
- Entry: limit 0.52 → filled 0.39 (−0.13) at 14:09:47Z
- Exit: STOP at 14:16:44Z, `entry=0.39 bid=0.31` — **−21%, essentially the intended −20%**
- Exit fill 0.31, submitted and filled in the same second
- **The only trade where the whole lifecycle behaved as designed on a loss.**

### 5. QQQ260731P00675000 — PULLBACK short — **−$37** — execution/slippage
- Selected: strike 675 P, **delta −0.186**, bid/ask 0.90/0.91
- Entry: limit 0.91 → filled 0.89 at 14:24:59Z
- Exit: STOP at 14:28:31Z `bid=0.66` (−26%); first attempt cancelled on the
  2.0s buffer; second at 14:31:00Z `bid=0.51` filled 0.52
- **The single deferral cost 14¢ (−26% → −42%).**

### 6. QQQ260731P00681000 — PULLBACK short — **+$47** — normal strategy win
- Selected: strike 681 P, **delta −0.2239**
- Entry: limit 0.92 → filled 0.91 at 15:02:06Z (66s to fill)
- Exit: TARGET at 15:12:39Z `bid=1.38`, filled 1.38. Clean.

### 7. QQQ260731C00688000 — MSS long — **+$30** — normal strategy win
- Selected: strike 688 C, **delta 0.2515**
- Entry: limit 0.73 → filled 0.72, same second
- Exit: TARGET at 16:24:41Z `bid=1.01`, filled 1.02 in 1s. Clean.

### 8–10. QQQ260731C00690000 ×3 — three signals, one contract, net −$60

**These three trades do not share one classification.** Trade #8 is a normal
strategy win; trades #9 and #10 are state-machine defects. Splitting them
this way is what the attribution table above already counts: three normal
wins totalling **+$109** (which includes #8's +$32) and four state-machine
defects totalling **−$159** (which includes #9 and #10).

Three separate signals bought the **same contract**, and the tracking record
could not represent them.

| # | signal | trigger | delta | entry | exit | P&L | classification |
|---|---|---|---:|---|---|---:|---|
| 8 | `2175:long` | BOS | 0.2965 | 0.66 @17:21:40 | TARGET 0.98 @17:24:51 | **+$32** | **normal strategy win** |
| 9 | `2200:long` | PULLBACK | **0.4110** | 0.98 @17:45:48 | 0.45 @18:12:46 | **−$53** | **state-machine defect** |
| 10 | `2206:long` | PULLBACK | 0.3372 | 0.72 @17:59:03 | 0.33 @19:06:48 | **−$39** | **state-machine defect** |

**Trade #8's realized lifecycle was correct and profitable.** It entered at
0.66, hit its target, and closed at 0.98 in 191 seconds — before the second
signal on this contract existed. The OCC collision corrupted the *shared
tracking context* spanning trades #8–#10, but it did not turn #8's own
outcome into a loss, and #8 must not be counted as a defect. The defect
damage is confined to #9 and #10, which were entered into a tracking record
that could no longer distinguish them.

Evidence of the record collapse, directly from the exit-manager log:

- 18:12:45 — `STOP triggered (entry=0.72 bid=0.43)`. By FIFO this sell closed
  the **0.98** lot, not the 0.72 lot. The manager was reasoning about the
  wrong entry price.
- 19:10:28 — `STOP triggered (entry=0.85 bid=0.20)`. **0.85 is the arithmetic
  mean of 0.98 and 0.72.** Two distinct positions had been merged into one
  averaged record.
- The guard `SKIP entry: ... already has an open SMC_TRIANGLE position` did
  not fire until **19:10:01**, roughly 108 minutes after the second entry.
- Trade 10 also took **437 seconds to fill** (submitted 17:51:42, filled
  17:59:03) — a stale resting limit, not a marketable one.
- Trade 10's realized exit at 19:06:48 preceded the 19:10:28 stop signal,
  i.e. the position was closed outside the manager's own control loop.

Root cause is the one already recorded in the executor wrapper:
`open_positions.json` was keyed by OCC, so a second signal on the same
contract overwrote the first's tracking record.

---

## Cross-cutting findings

**1. Exit detection latency dominates the losses.** Stops were detected at
−21%, −26%, −31%, −46%, −53% against a −20% rule. The rule was never wrong;
the observation interval was. Every one of these is a direct argument for
Phase 4's stream-driven exits.

**2. The 2.0s-buffer deferral is actively harmful.** Six of the exit orders
were cancelled for missing a 2-second fill window and re-priced ~2 minutes
later at a worse bid. This converted two stops into −66% and −42%
realizations. It should not survive into the new runner in any form.

**3. Limits were priced off lagging indicative quotes.** Two entries filled
27¢ and 13¢ below their own limit. Paper flattered this; a live venue would
not.

**4. Telegram blocked the execution path.** Four `telegram notify failed …
timed out after 150 seconds` entries sit *inline* in the executor log
between order submissions. Notification was synchronous and could stall the
trading loop for 2.5 minutes per message. This is concrete evidence for the
Phase 5 requirement that dashboard/Telegram must be asynchronous.

**5. Three of ten entries were SWEEP_RECLAIM** (−$35, −$32, −$8 = −$75).
VARIANT_B_NO_SWEEP excludes that trigger upstream, so this cohort would not
be taken by the frozen candidate.

**6. No data-quality failure was found — and the staleness was a handoff
problem, not a feed problem.** Every one of the ten selections recorded
`quote_age_seconds = 0.0` and a valid two-sided quote **at the selector's
decision timestamp**. The quotes were fresh when the decision was made.

They became stale *afterwards*, because the executor did not submit for
roughly 170 seconds — a separate `*/3` cron stage between the decision and
the order. That is **scheduler and handoff staleness**, introduced by the
architecture between the two stages, not a feed that supplied bad data.

Two clarifications so this is not misread later:

- This is **not** evidence that ThetaData supplied stale quotes at the
  decision timestamp. ThetaData was **not the quote source for these trades
  at all** — the 07-31 path read quotes through
  `options_orchestrator` (Alpaca). The ThetaData streaming path did not
  exist yet.
- The visible symptom of that 170-second gap is the two entries that filled
  27¢ and 13¢ *below* their own limit (finding 3): the limit was computed
  from a quote that was accurate when computed and out of date by the time
  it reached the venue.

The fix is therefore to remove the gap, not to distrust the feed.

---

## Threshold decision (heff, 2026-08-01)

**The −20% stop, the profit target and the time-exit stay unchanged for
VARIANT_B_NO_SWEEP's initial forward window.**

The evidence in this document supports that directly: the one stop that was
observed and acted on promptly realized **−21%**, essentially exactly the
rule. Every worse outcome (−26%, −31%, −46%, −53%, and the −66% deferral
case) is explained by observation cadence and the deferral loop, not by the
threshold being wrong.

Changing thresholds now would mix strategy tuning with execution repair and
make the forward result uninterpretable — there would be no way to tell
whether a changed outcome came from the new execution path or the new
numbers. The execution path is what changes; the strategy is held fixed.

Target and stop percentages are held constant for the full 30-session
window.

## Signal latency measured from these trades

Bar close → order submission, per signal:

| stage | observed | mechanism |
|---|---|---|
| bar close → detector emitted | 41–227 s (median ~160 s) | `*/3` detector cron |
| detector → selector decision | 25–115 s (median ~73 s) | `*/3` selector cron |
| selector decision → order submitted | ~170 s typical | `*/3` executor cron |
| **bar close → order submitted** | **~6–7 minutes** | three chained 3-minute crons |

Against the target of **p95 < 1 s from confirmed bar to internal signal**,
the current architecture is roughly two orders of magnitude off, and none of
it is bar-formation time — it is scheduler dwell.
