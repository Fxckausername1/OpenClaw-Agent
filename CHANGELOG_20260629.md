# Changelog — 2026-06-29

Structural fixes deployed to the OpenClaw trading stack on 2026-06-29. **All changes are PAPER-only**;
real money remains gated behind confirm-every-trade. Each entry lists the problem, root cause, the fix,
files touched, and how it was verified. A timestamped backup of every edited file was saved alongside it
(`<file>.bak_20260629_*`).

---

## 1. Universe Purification — top-down S&P 500 pivot (fixes the R-mirage)

**Problem.** The continuous-search champion numbers on the new wide universe were implausible — Sharpe ~15
and per-trade R ~0.50 vs the validated curated-99 baseline of ~0.10–0.13 R / Sharpe ~2.5. The optimizer
was at risk of curve-fitting "universal" parameters to a handful of distorted names.

**Root cause.** Bottom-up filtering (price + ADV, then a $2B market-cap floor, then a realized-vol attempt)
each surfaced a *new* category of contamination — 2× leveraged/inverse single-stock ETFs, then AI/quantum/
crypto-mining/space hype micro-caps, then backfilled miners and recent volatile IPOs. None of those proxies
cleanly separates "stable, liquid equity" from "speculative." A per-symbol breakdown showed the top ~25 of
181 names accounting for ~45% of total backtest R.

**Fix.** Pivoted `wide_universe.py` to **top-down S&P 500 index membership** (`fetch_sp500_tickers()` via
Wikipedia + a browser User-Agent to dodge the 403; `build_sp500_universe()`). Apply only the $5 price floor
(no ceiling — that's a downstream scanner cap), then keep the top 180 by IEX volume rank. Result: **180
clean names, zero overlap with any previously-flagged offender.**

**Bonus bug found + documented.** Alpaca's free "ADV" (`fetch_daily_volume_batch`) is **IEX-feed-only**
(~2–3% of consolidated volume) — median IEX ADV across the *entire* S&P 500 came back ~166K, below even the
old 250K floor. An absolute ADV threshold from this feed is meaningless for liquid mega-caps; it is safe
**only as a relative ranking signal**, never an absolute cutoff. (This same IEX limitation reappears in #2.)

**Artifacts.** Archived (not deleted) every prior round's ledger/champion/component cache with dated
suffixes. `continuous_search_ledger.csv` / `continuous_champion.json` reset to a clean slate for the cutover.
`promote_champion.py` is commented out of the nightly wrapper until the new universe earns a track record.

**Verified.** 180-symbol per-symbol R breakdown — residual elevation now traces to *real* S&P 500
constituents in volatile sectors (AI-capex semis, biotech, crypto-adjacent fintech, airlines), not junk.
The literal goal (zero leveraged ETFs / micro-caps / crypto-miners) is structurally met and re-verifiable.

---

## 2. ORB Staleness Guard — 15-minute latency cap (prevents stale entries)

**Problem.** On 2026-06-29, ORB breakouts that occurred at 09:45 and 10:05 ET were not detected/acted upon
until ~11:34 ET — a **~2-hour lag**, entering breakouts long after the move.

**Root cause analysis (and a correction).** A 9-day audit showed this is **not chronic**: 8 of 9 days had a
healthy 5–6 min detection lag (5-min bar close + 2-min scan cadence). Only 2026-06-29 was an outlier. The
scanner ran on schedule the whole morning but kept reporting "no triggers" right after the breaks, then
"found" them ~2 hrs later — consistent with the **free IEX feed's bars backfilling late** (same limitation
as #1), not compute or universe size. An earlier "chronic IEX sparsity" hypothesis was walked back once the
9-day data contradicted it.

**Fix.** Added a **staleness guard** to `orb_scanner.py`: a break detected more than `STALE_MIN` (15) minutes
after it occurred is **skipped** — a lagged day now produces *fewer* trades, never *stale* ones. Normal ~6-min
lags are never affected. Also added **lag instrumentation**: every fired trigger now stamps `detected_at` +
`lag_min`, so future lag is measured directly instead of inferred from file timestamps.

**Related tuning (separate decision).** `use_vol` was set to `false` (price-only ORB break, `live_params.json`)
for timelier signals / momentum style. Note: the 9-day data showed the volume filter was *not* the cause of
the lag, so this is a style choice, not a lag fix — flagged for forward-testing (cohort split by date).

**Verified.** Guard simulated against the day's real numbers: 6-min lag → FIRES, 87/107-min lags → SKIP.

---

## 3. Options P&L Accounting Fix — multi-leg sign error + the −$625 phantom loss

**Problem.** A closed S3 trade (FE) booked **−$625** on a defined-risk spread whose max loss was **$425** —
an impossible −1.47 R. A second (FAST) booked −$550 on a $475-max spread. The combined ledger read −$1,175
vs the true −$625, distorting portfolio capacity and risk gates.

**Root cause.** Alpaca reports multi-leg `filled_avg_price` with a **buys − sells** sign convention, so a
spread closed *for a net credit* comes back **negative** (FE closed for a +$0.40 credit → reported −0.40).
The code fed that −0.40 straight into the realized formula as the exit price: `(−0.40 − 0.85) × 500 = −625`
instead of the correct `(+0.40 − 0.85) × 500 = −225`. **Verdict: logic bug, not a stale row or CSV issue.**

**Fix.**
- `options_orchestrator.py` `process_exits`: `exec_px = abs(float(...))` — take the magnitude of the
  multi-leg fill (correct for both debit and credit closes).
- `options_eval.py` `reconcile_expirations` (latent twin): a worthless OTM expiry now signs by structure —
  a **credit** spread keeps the premium (+, max profit), a **debit** spread loses the debit (−, max loss).
  Would have mis-booked the first debit spread held to expiry.
- Corrected the two already-corrupted rows from their real fills: **FAST −550 → −400** (r −0.84),
  **FE −625 → −225** (r −0.53). Combined CLOSED P&L now **−$625** (true), both R-multiples back in `[−1, 0]`.

**Verified.** Confirmed the other realized paths are clean (OPASN books `−risk` deliberately; the OPEN→CLOSED
reconcile delegates). Both files' selftests pass; ledger + DB backed up before the row edits.

---

## 4. Options Exit Engine — entry-fill reconcile + close-path crash

**Problem.** Two open option spreads sat **unmanaged** (no working stop), one already past its stop-loss; the
exit sweep reported "0 open trades to evaluate" despite live positions.

**Root cause (two stacked bugs).**
1. The tournament wrapper ran the entry (`bridge`) and the exit sweep (`--exits`) but **never the
   `--reconcile` step in between** — the one that transitions `PENDING → OPEN` on fill. Trades stayed stuck
   at `PENDING` forever, and `process_exits` only manages `OPEN`, so it silently ignored them.
2. The close path itself crashed with a `NameError` (`cost` / `exec_cost` — undefined; the real vars are
   `metric` / `exec_px`), so even when a stop fired the close order never submitted.

**Fix.** Wired `options_eval.py --reconcile` into `scripts/orb_options_tournament_wrapper.sh` between entry
and exit. Fixed both `NameError`s in `process_exits`. The engine now adopts fills each tick and manages
TP/SL correctly (verified end-to-end — FE stopped out at its rule once adopted).

---

## 5. Options Flatten Fix — cancel-then-close (clears the zombies)

**Problem.** Equity positions (DXCM, KMI, earlier KHC) sat open and **naked for days** — the EOD flatten ran
but failed silently with a 403 (`insufficient qty available ... held_for_orders`). They clogged concurrency
slots and blocked fresh signals.

**Root cause.** The flatten issued a position close while the protective **stop order still reserved the
shares**, so Alpaca refused it.

**Fix.** `alpaca_executor.py` flatten now **cancels open orders first, then closes** (new `cancel_orders_for()`
helper), and operates on **equity positions only** — option legs are left to the tournament's own exit/expiry
engine. Verified by dry-run (targets the equity short, skips the option legs). The two stale zombies were
manually flattened (+~$9 locked, naked risk removed).

---

## 6. Post-Close Telegram Spam Fix — delete stale message files

**Problem.** The same scanner alert was re-sent every cron tick for ~2 hours after the close.

**Root cause.** The three scanner wrappers (`orb`, `mr_watch`, `mean_reversion`) re-`cat` and re-send their
message file every tick and **never delete it**. When the market closes the scanner returns early without
clearing the file, so the last intraday alert got re-blasted until the cron window ended.

**Fix.** All three wrappers now **delete the message file after a successful send** (`&& rm -f "$MSG"`) — each
alert goes out **exactly once**. Deletion is gated on send success, so a transient failure still retries next
tick (no dropped alerts). Also removed the stale file that was mid-loop. Verified: syntax OK, structure
(mr_watch's paper-eval call) preserved.

---

## 7. Options Sizing — defense-in-depth $500 premium cap

**Problem / context.** Enforce a strict ≤ $500 premium (cost/risk) per options trade, scaling quantity down
to fit or rejecting if a single contract exceeds it.

**Finding.** The cap was already enforced for the live path: `size_qty()` scales `qty` so
`max_loss × 100 × qty ≤ $500` and returns 0 if one contract exceeds it. For debit spreads (S3, all live
trades) `max_loss = the debit paid = the premium`, so it was already a premium cap.

**Fix (hardening).** Added an **explicit hard-cap re-validation at the order-submission boundary** in
`run_tournament` — re-checks the final `qty` and **fails safe (no trade)** if it would exceed $500, so no
upstream change (MILP / DTS / recipe edit) can ever route an oversized order to the broker. Added 5 selftest
assertions (scale → 5 contracts; reject single > $500; exact-$500 boundary; just-over reject; total-cost
invariant) — all pass.

**Verified.** Dry-run tournament scaled BAC S1 to **qty 13 = $468** (14 would be $504), payload valid, flow
intact.

---

## 8. Options New-Trade Telegram Alert (new capability)

**Added.** A `notify_telegram()` helper in `options_orchestrator.py`, fired on a **successful armed fill**.
Message contains: **symbol, spread type** (e.g. "Call Debit Spread"), **legs, quantity, net entry (debit/
credit), and total risk**. Implemented as **fire-and-forget** (detached `Popen`, all stdio to `DEVNULL`,
fully try/excepted) so it returns instantly, never hangs the execution loop, and completes in the background
even when the single-core box is busy (e.g. the 22:05 nightly pipeline starved a blocking send). The order is
submitted + ledger-recorded *before* the alert, so a dropped notification is harmless.

---

## 9. "Prove-It-Or-Lose-It" replacement logic (earlier today)

**Added.** `evaluate_replacement()` in `alpaca_executor.py`: when the book is at the 3-position concurrency
cap and a trigger is bumped, it ranks open positions by live `unrealized_plpc` and either **cuts the worst
loser** (if beyond −0.5%, to seat the new trigger) or **chokes the smallest winner** (moves its stop to
breakeven via `Alpaca.replace_order`), else holds. All Alpaca calls individually try/excepted; dry-run
previews without mutating. Verified against a mocked client across all branches.

---

## Still open / next session

- **Watch the new S&P 500 universe's first live cohort** (zero track record yet); review a few nightly
  `continuous_search` runs before re-enabling `promote_champion.py`.
- **Watch ORB `lag_min`** tomorrow — back to ~6 min ⇒ 2026-06-29 was a one-off; creeping up ⇒ escalate
  (yfinance cross-check / liquid-volatile watchlist / paid SIP feed).
- **`use_vol=false`** is live (price-only ORB) — forward-test timely-vs-confirmed.
- **Priority paid upgrade when revenue allows:** Alpaca Algo Trader Plus ($99/mo) — full SIP feed fixes the
  ORB lag *and* unlocks OPRA options data; integration is a one-line `feed=iex → sip` change.
- Recurring/periodic open-book Telegram summary (visibility) — not yet built.
```
