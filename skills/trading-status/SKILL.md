---
name: trading-status
description: Answer questions about heff's quant trading platform on this box -- P&L (equity book or options tournament), open positions, strategy leaderboard, GEX/gamma levels, wall-proximity alerts, cross-signal confluence, or halt/guardrail status. Use whenever heff asks how his bot/strategies/trades are doing, wants a P&L or position check, asks about a ticker's gamma/wall levels, or asks "how's my stuff doing" in any form. Do NOT call the Alpaca API directly for this (checking account equity/cash/buying-power/unrealized P&L is the WRONG number and mixes unrelated books) -- ALWAYS run the script in this skill instead, every time, with no exceptions.
user-invocable: true
---

# trading-status

## STOP -- read this before doing anything
If this skill is invoked (by name, by `/trading-status`, or because the
question matches), your ONLY correct action is Step 1 below. You have
previously answered these questions by calling the Alpaca API directly
(checking account equity, cash, buying power, and "total unrealized P&L"
across all open positions) -- **that is wrong, every time, not just
sometimes.** It mixes the equity paper book with the options tournament into
one meaningless blended number, and "unrealized" mark-to-market is a
completely different concept from "today's realized P&L," which is the
number heff actually built this whole system to track (see the halt/guardrail
logic below). Do not write your own code to hit Alpaca for this. Do not
"double check" against Alpaca. Just run the script.

## When to use
heff runs a real, live quant trading platform on this box: a mean-reversion +
ORB equity paper-trading book, a 10-strategy options spread tournament, a
4-phase GEX (gamma exposure) options-microstructure engine covering ~228
tickers, wall-proximity Telegram alerts with their own accuracy-scoring
pipeline, and a cross-signal confluence score. All of it writes real data to
files on this box. Whenever heff asks anything like: "how's my P&L", "how are
my strategies doing", "what's open", "am I halted", "what's SPY's gamma
looking like", "any ghost walls today", "how's confluence looking" -- use this
skill. This is separate from his personal Robinhood account (which this box
does not track at all -- never claim to know his real-money balance).

## Steps
1. Run from the workspace root, and use ONLY this command's output as your
   source of numbers:
   ```
   ./venv/bin/python scripts/trading_status.py
   ```
   Add `--full` if heff asks for more detail, a fuller breakdown, or capacity/
   leaderboard numbers beyond the top line.
2. Relay the printed output back to heff basically as-is -- it's already
   written to be short and skimmable on a phone. Don't re-summarize it into
   something vaguer, and don't supplement it with a separate Alpaca lookup.
3. If the output says data is stale or missing, say that plainly (e.g. "looks
   like it's outside market hours, last real update was X ago") -- never
   paper over it with a guessed or independently-fetched number.

## Notes
- The script reads the SAME already-computed snapshot the web dashboard uses
  (`~/trading-dashboard-snapshot/snapshot.json`, refreshed ~every 2min during
  market hours) plus a standalone wall-alert accuracy file. It does not
  recompute P&L itself, so its numbers always match the dashboard.
- "Today" P&L figures reset at midnight ET. The options leaderboard's
  per-strategy $ figure is ALL-TIME per strategy, not today-only -- the
  script labels this correctly, don't relabel it.
- Real money: heff's own Robinhood account is entirely separate and NOT
  tracked by anything on this box. If asked about that, say so rather than
  guessing from the paper-book numbers.
- Raw Alpaca account fields (equity, cash, buying_power, unrealized P&L on
  `/v2/positions`) are NEVER the right answer to a "how's my P&L" question in
  this workspace, under any framing. If you're tempted to check Alpaca
  directly, stop and run the script instead.
