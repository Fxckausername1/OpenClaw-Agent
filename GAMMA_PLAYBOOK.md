# THE GAMMA PLAYBOOK — source framework for gex.py / BT1
*(heff's personal reference, ingested 2026-06-25. "Not financial advice — gamma is a lean,
not a guarantee." A number either comes from a source or it doesn't appear. Never overrides
`no trigger = no trade`.)*

## Core idea
Market makers hedge to stay delta-neutral; that forced hedging is real flow on the tape. **GEX
measures how much and which way.** It's not a prediction — it's a mechanical consequence of where
options open interest sits. It explains the *structural why* behind fills/reversals already read by feel.

## The two regimes (the whole model — ask before the session)
| | POSITIVE GAMMA | NEGATIVE GAMMA |
|---|---|---|
| Dealer hedging | buy dips, sell rips | sell dips, buy rips |
| Effect on price | **suppresses** vol | **amplifies** vol |
| Day character | range-bound, mean-reverting, pinned | trending, violent, momentum |
| Fills | sweeps stall & reverse at walls | sweeps run clean to next pool |
| Gaps | fill then fade | run away (breakaway risk) |
| Default stance | **fade extremes, shorter targets** | **trade momentum, let winners run** |

→ **This IS our BT1 hypothesis: POS-gamma favors MEAN-REVERSION; NEG-gamma favors ORB/momentum.**

## The three levels you mark
- **Gamma flip (zero-gamma line):** spot where regime flips sign. Above = suppressed; below = amplified.
  The single most important pivot; a clean break of it sets the day's tone.
- **Call wall:** largest +gamma strike *above* price — magnet/ceiling; rallies & gap-fills stall/reverse there.
- **Put wall:** largest +gamma strike *below* price — magnet/floor; flushes/sweeps find support there.
- Walls are strong magnets in POS gamma (dealers defend), weak in NEG gamma (hedging pushes through).
  **Walls move intraday — recompute, don't trust yesterday's lines.**

## The aggregation (what the code does) — Section 4
For each strike: **`gamma × OI × 100 × S² × 0.01`**, dealer sign (**calls +, puts −**). Sum across chain.
Net positive → suppressive; negative → amplifying. **Flip = where the running sum crosses zero;
walls = largest-magnitude strikes.** (We compute gamma via Black-Scholes from daily bars — modeled.)

## Three things that keep the number honest
1. **Greeks are modeled, not exchange truth** → tag them (`BS_modeled` in our `gex.py`).
2. **Deep-ITM / illiquid strikes missing** → report **"computed over N of M strikes"**; an incomplete
   GEX that *admits* it's incomplete is honest. (`gex.py` emits `n_oi`/`m_contracts`/`coverage`; thin
   chain → regime `none`.)
3. **Verify before you trust** → check computed flip/walls vs a free reference (FlashAlpha, GEXStream)
   for the same ticker/minute before any LIVE use, until the math is proven.

## The timescale truth (critical)
| When | Reliability | Use for |
|---|---|---|
| 9:30–9:32 | noisy/unsettled — avoid | prior-close levels only |
| 10:00+ | reliable (chain settled) | partial-gap reversals, intraday levels |
| EOD / next-day | most reliable (settled) | swing levels, consolidation reads |
**T-1 OI lag:** OI is prior-settle. Our daily backtest predicts day *t* from OI known at *t*'s open →
honest, no-lookahead. Daily EOD = the "most reliable" bucket.

## Setup × regime cheat sheet (Section 7)
| Setup | Gamma's role | Play |
|---|---|---|
| Liquidity sweep | ride vs. stall | POS → short target, TP into wall. NEG → let it run. |
| Fast gap-fill (1–2m) | **none live** — context only | microstructure trade; prior-close walls as terrain only |
| Partial gap (1hr) | **primary** | half-fill stalls at wall → reversal; VWAP reject confirms |
| BTC/MSTR follow | secondary (level map) | beta drives it; MSTR walls show where follow-move stalls |
| Consolidation | forward tell | POS predicts chop; walls bracket range; trade the break |
| Swing (4H/D) | **most reliable** | daily flip/walls = real S/R; flip break = invalidation |

## Guardrails (don't skip)
Gamma is a lean, not a guarantee · confluence is the whole point (a wall that lines up with your
existing level = high conviction; a wall alone is weak) · it does NOT override your trigger · know the
regime before the bell · recompute (positioning moves) · **never fabricate — "no clean read today"
is a valid read.** These map directly onto our discipline: KILL if it doesn't carry on the locked
holdout; report coverage; don't hand-wave a number.
