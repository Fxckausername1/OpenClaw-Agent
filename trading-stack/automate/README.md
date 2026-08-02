Automation scaffold

Files:
- fetch_and_signal.js — fetches historical prices for symbols listed in ../symbols.json, computes a simple SMA(10/50) signal, writes per-symbol CSVs and trading-stack/auto-output/signals.json.

How to run locally:
1) Ensure dependencies installed in trading-stack (node modules): axios, yahoo-finance2, technicalindicators
2) node trading-stack/automate/fetch_and_signal.js

Automation idea:
- Add a cron job to run this script at market open (e.g., 13:30 UTC weekdays). The script writes signals.json which downstream alerting/notification scripts can consume.

