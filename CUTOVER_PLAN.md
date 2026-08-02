# SMC PAPER daemon cutover plan

**Status: prepared, not executed.** The daemon, dashboard bridge, Telegram
signal path, one-entry latch, and offline load tests are built. The remaining
gates require a live market: live ThetaData/Alpaca measurements, one controlled
PAPER lifecycle, soak, and restart reconciliation.

## Verified current state

- Four legacy SMC cron entries remain present, but their wrappers are disarmed.
- Cron has not been changed in this build.
- Theta Terminal and the daemon are stopped.
- No broker order was placed during this build.
- `paper-connectivity` cannot submit an entry, independently of readiness.
- `paper-forward --max-entry-submissions 1` consumes its allowance before the
  POST, including an unknown-fate timeout.

## Theta Terminal lifecycle

The SMC daemon starts Theta Terminal automatically before opening the local
WebSocket client. It uses the private Java 21 runtime and official terminal JAR
under the `heff` account, injects the API key through the child environment
instead of argv, watches the process, and restarts it after an unexpected exit.
The dashboard field `theta_terminal_manager` shows its state, PID, ownership,
restart count, paths, and last error. Normal daemon shutdown stops a terminal it
started; a terminal that was already running is reused and left alone.

## Gate 0 — Monday validation before touching cron

Start the full production graph in connectivity mode. Theta Terminal starts
with it; signals are visible in Telegram and BOT_NEXUS, but broker entry
submission is disabled by mode:

```bash
ssh heff@165.227.221.54 'cd /home/heff/.openclaw/workspace && nohup env SMC_MAX_CONCURRENT_POSITIONS=1 SMC_MAX_ENTRIES_PER_WINDOW=1 SMC_MAX_DAILY_REALIZED_LOSS=100 ./venv/bin/python -m smc.run_daemon --mode paper-connectivity > logs/smc_daemon_connectivity.log 2>&1 < /dev/null &'
```

Verify:

- `data/live_heff_smc/daemon_dashboard.json` is current.
- `theta_terminal_manager.ready` is true and `state` is `ready_owned` or
  `ready_external`.
- `trading_readiness.blocking` is empty during market hours.
- `thetadata.reader_callback_ms`, `quote_worker`, `live_detector_cycle`, and
  `minute_scheduler` contain live observations.
- A detector signal appears in `recent_signals`, BOT_NEXUS
  `smc_paper_pipeline`, and Telegram.
- Broker orders remain zero.

Stop connectivity mode:

```bash
ssh heff@165.227.221.54 'pkill -f "[s]mc.run_daemon --mode paper-connectivity"'
```

## Gate 1 — one controlled PAPER lifecycle

This command is prepared but must not be run until Gate 0 passes. It allows
exactly one entry POST attempt for the process lifetime, then continues
managing that attempt/position and blocks later entries:

```bash
ssh heff@165.227.221.54 'cd /home/heff/.openclaw/workspace && nohup env SMC_MAX_CONCURRENT_POSITIONS=1 SMC_MAX_CORRELATED_QQQ_CONTRACTS=1 SMC_MAX_ENTRIES_PER_WINDOW=1 SMC_MAX_DAILY_REALIZED_LOSS=100 ./venv/bin/python -m smc.run_daemon --mode paper-forward --max-entry-submissions 1 > logs/smc_daemon_canary.log 2>&1 < /dev/null &'
```

Review the full record: signal, contract selection, intent commit, Alpaca POST
and acknowledgement latency, fill price, ThetaData quote at decision, slippage,
managed exit, P&L, Telegram, BOT_NEXUS, and broker/local reconciliation.

## Gate 2 — soak and restart reconciliation

- Run a full market session in connectivity mode and confirm no reader backlog,
  loop timeout, stale generation, or detector clock gap.
- During a controlled PAPER position, restart once and prove broker truth is
  reconciled into the same local position with no duplicate order.
- Keep the legacy cron unchanged until both tests pass.

## Final cron cutover

Only after Gates 0–2 pass:

1. Back up cron to an explicit timestamped file.
2. Comment exactly the four `live_heff_smc_*_wrapper.sh` entries; do not delete
   or alter unrelated entries.
3. Start `paper-forward` with the canary environment above.
4. Prove exactly one daemon and one singleton lock exist.
5. Confirm dashboard, Telegram, reconciliation, detector, entry, and exit
   health before leaving it unattended.

Backup command:

```bash
ssh root@165.227.221.54 'crontab -u heff -l > /home/heff/crontab_backup_precutover_$(date +%Y%m%d_%H%M%S).txt'
```

Comment only the four SMC entries:

```bash
ssh root@165.227.221.54 'crontab -u heff -l | sed -E "s#^([^#].*live_heff_smc_(detector|selector|executor|exit_manager)_wrapper\.sh.*)$#\# CUTOVER (replaced by SMC PAPER daemon): \1#" | crontab -u heff -'
```

## Rollback

Stop the daemon and restore the exact backup selected by timestamp—never a
wildcard:

```bash
ssh root@165.227.221.54 'pkill -f "[s]mc.run_daemon"; crontab -u heff /home/heff/crontab_backup_precutover_TIMESTAMP.txt'
```

## Gate checklist

- [x] Production daemon graph assembled
- [x] Dashboard JSON and BOT_NEXUS bridge wired
- [x] Telegram signal lifecycle wired
- [x] Confirmed-minute scheduler, fresh-bar ingestion, and rollover tested
- [x] Reader isolation tested at 10/50/100/500 ms handler delays
- [x] One-entry submission latch tested
- [ ] Monday live-message and latency gate passed
- [ ] Controlled PAPER entry and managed exit measured
- [ ] Full-session resource soak passed
- [ ] Restart/reconciliation with a PAPER position passed

Do not perform the final cron cutover until every box is checked.
