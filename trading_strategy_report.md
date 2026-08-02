# Pentagon / Heff — Mean-Reversion Trading System

A plain-English walkthrough of the strategy, the live signaler, and the backtesting.

---

## 1. What it is (in one paragraph)
A fully automated **intraday mean-reversion** system running on the OpenClaw server.
Every 15 minutes during market hours it scans the S&P 100, finds stocks that have
stretched too far from their recent average price, and signals the moment they start
snapping back. Alerts go to Telegram. It is **paper-trading only right now** — no real
money — while we prove the edge. Everything is measured in **R (risk units)**.

## 2. The strategy (the edge)
**Mean reversion:** when a stock stretches unusually far from its short-term average
inside the day, it tends to snap back toward it. We fade the stretch — but carefully.

It is **two-stage** so we never "catch a falling knife":

- **Stage 1 — WATCH (the setup).** The stock is stretched. ALL four must be true on the
  5-minute chart:
  1. z-score ≥ 2 away from its 20-bar average (statistically far)
  2. price beyond the 2-sigma Bollinger band
  3. RSI exhausted — below 30 (oversold) or above 70 (overbought)
  4. at least 1.5% away from the session VWAP (the day's volume-weighted average price)
  This means "stretched." It does **not** mean "enter."

- **Stage 2 — TRIGGER (the entry).** The stretch starts *releasing*. A later 5-min bar
  closes back **inside** the band AND breaks the prior extended bar's low (for a short)
  or high (for a long). Only now is it actionable.

**Per trade the system defines:**
- Entry = the break level
- Stop = just past the extreme high/low (a 0.15% buffer) — if price makes a new extreme, the idea is wrong
- Target 1 = VWAP (the magnet it reverts to)
- Target 2 = the 20-bar average
- It only takes the trade if **reward:risk ≥ 1.5**, and exits at end of day if neither target nor stop is hit.

## 3. The signaler (live scanner)
- Runs **every 15 min, 9:30am–4:00pm ET, weekdays** (cron on the server)
- Scans the S&P 100; **skips** any stock in its earnings blackout (±1 day) and any stock
  over $250 (so positions are sizable on a small account)
- Sends two Telegram alert types:
  - 👀 **WATCH** — "a setup is forming, get ready, do NOT enter yet"
  - 🎯 **TRIGGER** — the actionable one, with a full ticket: entry / stop / Target 1 / Target 2 / R:R
- Dedupes — each stock/direction only alerts once a day, so no spam
- **Nothing auto-executes.** Every trade is confirm-first by the human.

## 4. Risk & how you'd actually trade it
- **R = risk unit.** 1R = the dollars you lose if the stop hits. A +2R winner makes twice
  what you risked; a loss is −1R. The strategy's quality is measured in average R per trade,
  which is what compounds an account — independent of position size.
- Stops are **tight (~0.7% of price)**, so on a $500–800 account you are effectively all-in
  on one name per trade, which naturally risks only ~0.7–1% of the account.
- **Robinhood cannot short stocks**, so only the **LONG (bounce)** side is tradable as shares.
  Conveniently, the long side is also the strategy's stronger half. (Shorts would need puts.)
- The $25k "pattern day trader" rule was **eliminated in June 2026**, so frequent
  day-trading on a small account is allowed.

## 5. The backtesting (four layers, increasing rigor)
1. **Quick backtest** (free yfinance data, ~40 trading days) — proved the logic works.
2. **Tuning backtests** — tested and locked in the best settings:
   - Target = VWAP (closer targets produced zero qualifying trades)
   - **Earnings filter adopted** (improved every metric)
   - Price cap = $250 (best balance of volume and quality)
   - Exit rule = **end-of-day** (holding to target across days was tested and REJECTED — it
     doubled the average winner but cut win rate in half; worse overall)
3. **Deep backtest** (Databento, **2 years of 5-minute data**, in progress now) — re-runs the
   *exact* live config over ~8× more history, with a **per-year breakdown** to see if the edge
   holds across different market conditions. This is the real durability test.
4. **QuantConnect** (connected) — will port the strategy to their engine for an independent
   second opinion over a much longer history. If two different engines agree on the shape,
   confidence goes way up.

## 6. Results so far + honest caveats
- In the 40-day sample, the **LONG side** (the tradable one): **59 trades, 66% win rate,
  +0.56R average per trade, profit factor 2.77** (made ~$2.77 for every $1 lost).
- **Caveats, stated plainly:** small sample (59 trades), no slippage modeled, and fills
  assumed perfect. A 40-day window can be a lucky streak.
- That is exactly why we are doing two things before risking a dollar:
  (a) **paper-trading live** — logging every real-time signal's outcome to build an
      out-of-sample track record, with a Friday review sent to Telegram, and
  (b) **validating on 2 years of professional data** + a second engine (QuantConnect).
- **Real money only after both confirm the edge holds.**

## 7. Current status
- **Signaler:** LIVE, paper-trading, logging every trade, weekly Friday review.
- **Deep data:** Databento 2-year 5-min pull running (~$12 of a $175 free credit).
- **Next:** deep-backtest verdict → QuantConnect cross-check → if the edge holds, wire a
  one-tap Robinhood confirm flow for the long side and go live small.

---
*Built on OpenClaw. Generated for review — numbers are from backtests, not live results.*
