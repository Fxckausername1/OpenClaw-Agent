# Signal-Quality Results — 2026-07-13

Corrected run: `signal_quality_20260713T044138Z`

The replay evaluated 18 pre-registered signal-quality candidates. Four threshold-
neighborhood candidates were excluded from walk-forward selection. All 12,530 frozen
intents had market breadth and prior-five-minute breadth available after correcting the
initial predecessor-timestamp coverage defect. Gap, reclaim, and other unaffected
metrics reproduced exactly across the two runs.

## MR

Close reclaim plus a 70% aligned signal-bar close location was the best broad filter:

- full sample: -0.037R at 6 bp, 6,700 intents / 504 days;
- 2024: -0.142R;
- 2025: -0.001R;
- 2026: -0.028R;
- bootstrap 95% interval: [-0.106R, +0.038R].

It was selected in both anchored folds. Combined selected out-of-sample evidence was
-0.011R across 5,381 intents / 368 days with interval [-0.089R, +0.068R]. The breadth-
turn hypothesis was negative-confirmed. MR fails the historical gate.

## ORB

A direction-aligned overnight gap of at least 0.3% produced +0.028R at 6 bp over 417
intents / 219 days, but the yearly path was unstable:

- 2024: -0.353R;
- 2025: +0.193R;
- 2026: -0.241R;
- bootstrap 95% interval: [-0.263R, +0.362R].

The 0.5% robustness threshold was +0.077R but had an even wider interval and turned
negative at 12 bp. The primary gap candidate lacked enough 2024 training coverage to be
selected in the first fold. Combined training-selected out-of-sample evidence was
-0.049R across 1,266 intents / 298 days with interval [-0.177R, +0.091R]. ORB fails the
historical gate.

## Decision

Reject both signal-quality families for live promotion. Do not convert the full-sample
ORB gap result into a live rule. It is a regime-dependent research observation, not a
stable edge.

True historical premarket-volume data remains unavailable; only seven snapshots exist.
Any future ORB gap research needs a newly collected, timestamped premarket dataset and a
sealed forward protocol. Live trading, cron, dashboard, and Netlify remain unchanged.
