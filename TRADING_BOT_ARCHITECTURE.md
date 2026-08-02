# Trading bot architecture — snapshot 2026-06-29

Living doc. Reflects exactly what's deployed as of this date; update when the stack changes materially.

## Agent count: 6 trading agents + 1 governance layer + 1 research feed + 1 reporting layer + 2 staged-not-active

### The operational pipeline (signal -> execute -> record)
1. **Mean-reversion signal agent** — `mean_reversion_scanner.py`. Two-stage watch->trigger on 5-min bars, z-score vs 20-period mean + RSI + VWAP deviation. Cron: full scan `*/5`, fast watch-only loop `*/1`, both 13-21 UTC weekdays.
2. **ORB signal agent** — `orb_scanner.py`. Opening-range breakout, tight-range filter (<=0.66% of price, holdout-confirmed edge). Cron: `*/2` 13-21 UTC weekdays.
3. **Equity execution agent** — `alpaca_executor.py`. Consumes both scanners' trigger files, runs them through guardrails + the portfolio gate (max 3 concurrent, max 2 per side), sizes to the real account, submits PAPER brackets/OTOs to Alpaca. **`evaluate_replacement()` added 2026-06-29 ("Prove-It-Or-Lose-It")**: when a trigger is rejected purely for the concurrency cap being full, ranks the real open positions by live `unrealized_plpc` and either market-closes the worst loser (if beyond -0.5%, beyond normal spread friction) to seat the new trigger, or tightens the smallest winner's stop to breakeven if every position is profitable (new `Alpaca.replace_order()` PATCH call), or holds if the worst position's loss sits inside the -0.5%-to-0% buffer. Cron: `*/2` 13-20 UTC.
4. **Options tournament agent** — `options_orchestrator.py` + `options_strategies.py` (S1-S10 recipe slate) + `options_eval.py` (SQLite ledger, Discounted Thompson Sampling, DSR) + `orb_tournament_bridge.py` (wires live ORB triggers into it). Per trigger: builds every matching arm -> EV filter -> MILP portfolio gate ($500/trade cap, 50% of the $1000 book; 3 total/2 per side) -> Thompson-sample pick -> PAPER submit + ledger record. Cron: `*/2` 13-21 UTC (entries + exit sweep), EOD expiry reconcile at 22:35 UTC. **Unchanged this session.**

### The self-improvement loop (discovers, then closes the loop back into #1/#2)
5. **Strategy discovery agent** — `continuous_search.py`. Wide/loosened parameter grid (319 configs) tested against the locked 75/25 holdout every night — the only thing never loosened. Incremental, ledger-tracked, never re-tests a config. **`data/continuous_search_ledger.csv`/`data/continuous_champion.json` do NOT currently exist (2026-06-29)** — archived for the universe cutover below; the next nightly run starts completely fresh on the new 180-symbol universe.
6. **Champion promotion agent** — `promote_champion.py`. Diffs the discovery agent's holdout-confirmed champion against `data/live_params.json` (which both scanners read at startup) and pushes any real change live. **DISABLED 2026-06-29** (commented out in `scripts/continuous_search_wrapper.sh`, dated rationale left in the script) — auto-pushing a "carried" win to live params with zero human review is too risky on a brand-new, zero-track-record universe. Re-enable once a few nightly runs on the new universe have been reviewed. Zero real promotion cycles to date (`data/live_params.json` still shows the original seed values, `data/promotion_history.jsonl` doesn't exist).

### Cuts across everything — not a pipeline stage
- **Governance/safety layer** — `guardrails.py` + the kill-switch flag file. Every execution agent (#3, #4) checks this before acting. Real money stays outside all of this entirely — confirm-every-trade is a human-in-the-loop boundary, not a bug to fix.
- **Logging — unified under loguru.** Shared `log_setup.py` (`get_logger(name)`) gives every pipeline script a rotating (daily)/retained (7-day) file sink at `logs/<name>.log`, layered on top of loguru's default stderr sink.

### Feeds data in, doesn't trade (yet)
- **Options research pipeline** — `databento_options.py` -> `greeks.py` -> `gex.py`, nightly (22:05 UTC). Computes per-symbol GEX regime (positive/negative gamma) for the options-pull universe. Currently informational only. **Unchanged this session.**

### Staged, not yet active (added 2026-06-29, Phase 3 — write-only per heff's instruction)
- **`market_regime.py`** — Kaufman's Efficiency Ratio on SPY daily closes -> TRENDING vs MEAN_REVERTING classification, VIX pulled for context only. `ER_THRESHOLD=0.30` is an explicit unvalidated placeholder. Compiles clean, `--selftest` (synthetic data) passes. **Not wired into any scanner; never run against real market data.**
- **`pairs_screener.py`** — Engle-Granger cointegration screening (statsmodels) with a correlation pre-filter, the real out-of-sample pair-selection fix this project's memory flagged as missing back on 2026-06-13. Compiles clean, `--selftest` (synthetic cointegrated series) passes. **The real `--screen` mode requires an explicit flag and has never been invoked; no candidate pairs exist yet.**

### Reporting — informational, no trading action
- `morning_report.py` (~9:50 ET), `night_report.py` (16:05 ET) — Telegram digests of account/track-record state. **Unchanged this session.**

## Data stores

| File | Owned by | Contents |
|---|---|---|
| `data/mr_triggers_<date>.jsonl`, `data/orb_triggers_<date>.jsonl` | scanners | every signal fired, tagged `universe: core99\|wide500k\|sp500_index` |
| `data/paper_trades.csv`, `data/orb_paper_trades.csv` | paper_eval.py | idealized-fill equity track record |
| `data/alpaca_orders.jsonl` | alpaca_executor.py | real paper order log (idempotency + audit) |
| `data/options_eval.db` | options_eval.py | SQLite ledger: trades, Thompson Sampling state, DSR scores |
| `data/continuous_search_ledger.csv` | continuous_search.py | **does not exist as of 2026-06-29** — archived for the universe cutover, next run starts fresh |
| `data/continuous_champion.json` | continuous_search.py | **does not exist as of 2026-06-29** — same reason |
| `data/live_params.json` | promote_champion.py | what's actually live right now — still the original seed values, zero real promotions to date |
| `data/promotion_history.jsonl` | promote_champion.py | **does not exist** — zero promotions have ever fired |
| `data/wide_universe.json` | wide_universe.py | **rebuilt 2026-06-29: top-down S&P 500 index membership** (`fetch_sp500_tickers()`/`build_sp500_universe()`), $5 price floor only (no ceiling), top 180 by IEX-volume rank if more survive. Replaces the bottom-up wide500k build (price+ADV+market-cap+blocklist), kept in the file for reference but no longer on the `load_universe()` rebuild path. See `EXCLUDED_SYMBOLS` (34 names: leveraged single-stock ETFs + 2 rounds of confirmed hype-stock offenders) — belt-and-suspenders, not currently load-bearing since no S&P 500 member is on it. |
| `data/wf_cache/*.parquet` | walkforward_search.py | offline backtest cache, float32/uint32. **367 files** as of 2026-06-29 — accumulated across every universe round (core99 Databento-sourced + multiple wide-universe Alpaca-sourced batches); harmless leftover, `load_cached()` only loads what's actually requested |
| `data/pairs_screener_ledger.csv`, `data/pairs_candidates.csv` | pairs_screener.py | **do not exist** — staged, never run against real data |
| `logs/<agent>.log` | log_setup.py | loguru rotating (daily)/retained (7-day) per-agent logs |

## Live status as of 2026-06-29 (overnight pre-open)

- Kill-switch: clear. Crontab: active except `promote_champion.py`'s line, commented out (dated rationale in `scripts/continuous_search_wrapper.sh`).
- Box: single-core, confirmed stable throughout this session — load stayed 0.02-0.18, 551Mi-1.2Gi free/available, no two heavy jobs ever run concurrently.
- **Equity universe is now its THIRD generation: core99 (curated, holdout-validated) -> wide500k (ADV-bottom-up, retired) -> sp500_index (top-down, 180 names, as of tonight).** Zero live track record on the new universe yet — z=1.5/tight-ORB were proven on curated-99 only; whether they carry here is genuinely unknown.
- `alpaca_executor.py` got `evaluate_replacement()` ("Prove-It-Or-Lose-It") this session — tested against a mocked Alpaca client (6 branches: cut/choke/hold/dry-run/no-stop-leg/malformed-data), all pass. Live and armed, zero real cut/choke events fired yet.
- `continuous_search.py`'s ledger/champion are both archived and gone — completely fresh start on the new universe, `promote_champion.py` disabled until a few nightly runs are reviewed.
- Options tournament: unchanged, still 0 real trades recorded (correctly declining, not stuck).
- Real money: still 100% outside this system. Nothing here has ever placed a non-paper order.

## Universe construction history (running log — append, don't overwrite)

1. **2026-06-26**: unfiltered wide universe (~5,600 names, $5-$266) — caused a real CPU-starvation incident (3 compounding bugs), retired.
2. **2026-06-28**: wide500k — ADV>500K bottom-up filter, 196 names, deployed unvalidated.
3. **2026-06-29**: 3 rounds of further bottom-up purification (market-cap floor, 2 blocklist rounds, a realized-vol attempt that failed a sanity check) each found NEW contamination categories and didn't converge -> **pivoted to top-down S&P 500 index membership, 180 names.** Found and documented a real bug along the way: Alpaca's free-tier "ADV" is IEX-feed-only (~2-3% of consolidated volume) — meaningless as an absolute threshold for genuinely liquid mega-caps; safe only as a relative ranking signal. See HANDOFF.md's 2026-06-29 section for the full blow-by-blow.

## Rejected strategies (tested, killed by the holdout — never touched paper/live capital)

- **Daily MA (50/200) + Volume Profile liquidity-zone swing strategy — REJECTED 2026-06-28.** heff's primary discretionary chart workflow, built as a third signal generator (`gen_volprofile`, candidate "Scanner #3"), run through the locked 75/25 holdout. Search PF 0.98/avg net R −0.044; holdout PF 0.67/avg net R −0.274 — worse on every metric. Core hypothesis (MA-touch = reliable trigger) has no inherent edge in this regime. **Kept as reusable utility, not deleted:** `compute_volume_profile`, `find_hvn_lvn`, the LVN-stop-routing pattern — useful for any future strategy with a proven directional edge.

## Known gaps / risks

1. **The box is single-core — the dominant operational constraint.** Never run two CPU-heavy jobs concurrently. Check `uptime` before launching anything heavy; sequence, don't parallelize.
2. **The sp500_index universe (180 names, 2026-06-29) is brand new and unvalidated**, same risk posture as wide500k was — but structurally cleaner: zero leveraged ETFs/speculative microcaps/crypto-miners (verified), residual elevated backtest R (0.51R/tr vs curated-99's ~0.10-0.15) traced to real S&P 500 constituents in volatile sectors (AI-capex semis, biotech, crypto-adjacent fintech, airlines), not junk. Watch its first live trades before trusting it like core99.
3. **IEX-feed-only "ADV" pitfall (found 2026-06-29): do not reintroduce an absolute ADV threshold sourced from `fetch_daily_volume_batch()`/Alpaca's free feed without remembering this.** It undercounts consolidated volume by roughly an order of magnitude or more for liquid large-caps (confirmed: S&P 500 median IEX-only ADV ~166K). Fine as a relative ranking signal, never as an absolute liquidity cutoff.
4. **`continuous_search.py` grid search is reset to zero (2026-06-29)**, not running — its ledger/champion were archived for the universe cutover. Resumes automatically via the weekday 23:50 UTC cron on the new universe, starting completely fresh.
5. **`promote_champion.py` is disabled (2026-06-29)**, commented out pending a few clean nightly runs on the new universe. Re-enabling it is a deliberate, separate decision — it won't silently turn back on.
6. **GEX/options regime data isn't gating anything live** — sample too thin per the BT1 backtest. Stays informational until/unless live paper data changes that picture.
7. **The single-leg "buy ~0.25-delta weekly option in the ORB direction" idea is not built.** Separate from the 10-strategy spread tournament that is live — still on the backlog if wanted.
8. **`market_regime.py` and `pairs_screener.py` are staged, not active (2026-06-29).** Written, compiled, synthetic-self-tested only. Nothing auto-activates them — wiring `market_regime.py` into a scanner, or authorizing `pairs_screener.py --screen` against real data, are both fresh decisions for whenever heff wants them.
9. **Infra tools explicitly evaluated and rejected 2026-06-28 (don't re-propose without new information):** PM2, DuckDB for `wf_cache`, Redis for signal routing. Each replaced with a narrower fix that solved the actual underlying problem.
10. **`data/wf_cache/` has grown to 367 files** across every universe round — mostly harmless leftover disk usage, not a correctness risk, but worth a cleanup pass eventually.
## Operations hardening — 2026-07-11

- Execution remains Alpaca PAPER / ARMED. No strategy thresholds or sizing parameters were changed.
- The executor, mean-reversion, and ORB wrappers gate their broad UTC cron windows in
  America/New_York time. This preserves EDT/EST coverage while avoiding post-close Python
  launches and log churn.
- Stop-to-breakeven replacement is idempotent: the executor skips Alpaca PATCH calls when
  the protective stop is already at the requested cent, avoiding repeated HTTP 422 errors.
- OpenClaw gateway ownership is canonical under root's enabled systemd user unit.
  The duplicate heff unit is disabled; root's unit owns loopback port 18789.
- Server memory protection: /swapfile is a persistent 2 GiB swap file with swappiness 10.
- Credentials remain under credentials/ with directory mode 0700 and files mode 0600,
  and are ignored/untracked by Git. Because secrets existed in earlier Git history, rotate
  the GitHub PAT and every credential previously committed (Apify, Databento, GCP, QC).
- Git remote URLs must never embed tokens. Origin is the credential-free HTTPS URL.
- Pre-change rollback artifacts are in /home/heff/ops-backups/ and per-file
  .bak_20260711_codex copies sit beside changed files.
