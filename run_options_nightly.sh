#!/bin/bash
# Nightly options pipeline — runs POST-CLOSE, clear of the scanner window (scanners end 21:59 UTC).
#   1) Databento OPRA pull (priority): per-name ISOLATED processes (1.9GB box = OOM on one name
#      can't kill the rest), 3 idempotent passes (saved schemas skip -> re-passes are ~free),
#      budget-gated ($100 ceiling per process; planned remaining ~$45 one-time).
#   2) greeks.py --symbol per name (sequential -> RAM released between names).
#   3) gex.py --symbol per name (needs greeks output + OI).
# Cheap/safe names first, AMD last (its statistics schema is the largest = highest OOM risk).
cd /home/heff/.openclaw/workspace || exit 1
exec 9>/tmp/options_nightly.lock
flock -n 9 || { echo "$(date -u) nightly already running, skip"; exit 0; }
PY=./venv/bin/python
LOG=data/options/nightly.log
NAMES="F BAC INTC PFE CSCO PLTR XLF GDX HOOD NVDA AMD"
{
echo "================ nightly $(date -u) ================"
# 1) DATA — priority #1
for pass in 1 2 3; do
  echo "---- pull pass $pass $(date -u) ----"
  for s in $NAMES; do
    $PY -u databento_options.py --budget 100 --arm --stats --universe "$s" || echo "   ($s pull proc exited nonzero pass $pass)"
  done
done
# 2) GREEKS
for s in $NAMES; do
  if [ -f "data/options/${s}_ohlcv1d.parquet" ]; then
    echo "---- greeks $s $(date -u) ----"; $PY -u greeks.py --symbol "$s" || echo "   ($s greeks failed)"
  fi
done
# 3) GEX
for s in $NAMES; do
  if [ -f "data/options/${s}_greeks.parquet" ]; then
    echo "---- gex $s $(date -u) ----"; $PY -u gex.py --symbol "$s" || echo "   ($s gex failed)"
  fi
done
echo "================ nightly DONE $(date -u) ================"
} >> "$LOG" 2>&1

# Telegram summary
NB=$(ls data/options/*_ohlcv1d.parquet 2>/dev/null | wc -l)
NS=$(ls data/options/*_stats.parquet 2>/dev/null | wc -l)
NG=$(ls data/options/*_gex.parquet 2>/dev/null | wc -l)
/usr/bin/openclaw message send --channel telegram --target 7590346809 \
  --message "Options nightly done $(date -u +%H:%MZ): ${NB} names w/ bars, ${NS} w/ OI, ${NG} w/ GEX. Backtest data ready -> build backtest_greeks.py next. (nightly.log on box)" >/dev/null 2>&1
