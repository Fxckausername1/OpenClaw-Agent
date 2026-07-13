# Candidate Research Milestone

This milestone tests a small, pre-registered family of research-only changes against
the frozen canonical MR and ORB intents. It does not alter signal generation, live
orders, cron, the dashboard, or Netlify.

## Structural hypothesis

The canonical replay found strong fill-selection bias. Both frozen strategies wait for
a close-confirmed move and then place a limit back at the old boundary. Continuations
often never fill, while a fill can indicate that the move has already failed. The main
candidate therefore enters at the next five-minute bar open, pays the same explicit
6/12/20/30 bp friction, and preserves pessimistic stop-first same-bar ordering.

## Pre-registered candidate family

Each strategy retains its frozen boundary-limit baseline. One-variable candidates test:

- next-bar-open entry with end-of-day, 1R, or 1.5R exits;
- a maximum 0.25R favorable gap extension;
- a 0.50% MR stop-distance floor or 0.35% ORB opening-range floor;
- prior-day SPY regime filtering;
- an ORB signal cutoff at 10:30.

One combined candidate per strategy tests the most defensible filters together. This is
not an unrestricted parameter grid.

## Point-in-time and walk-forward rules

- SPY regime uses only closes strictly before the signal date.
- Bull requires the prior close at least 0.5% above its prior 20-session average and a
  positive prior five-session return. Bear is symmetric; all other cases are neutral.
- Fold 1 trains on 2024 and tests 2025.
- Fold 2 trains on 2024-2025 and tests 2026.
- Candidate selection maximizes the training daily-block-bootstrap lower 95% bound at
  6 bp, with at least 200 intents and 60 training days.
- Historical passage requires both test-fold means above zero, at least 150 combined
  out-of-sample days, and a positive combined bootstrap lower bound at 6 bp.

## Promotion safety

The candidate family was designed after inspecting the full frozen baseline. Historical
walk-forward results are therefore discovery-contaminated. Even a historical pass is
blocked from live promotion until it earns a genuinely future forward sample. A failure
is rejected immediately.
