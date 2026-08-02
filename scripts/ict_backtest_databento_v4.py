import os, glob, json, gc
import pandas as pd
import pyarrow.parquet as pq
from math import floor

# Parameters — IDENTICAL strategy logic to v2/v3. Only the DATA TRAVERSAL changed:
# read each of the 8 files ONCE and group symbols within it (8 file reads total),
# instead of scanning the whole dataset once PER symbol (99 full scans = the v3
# slowness). Peak memory = one file at a time (~150-200 MB).
#
# Boundary note: the 20-bar rolling lookback restarts at each file boundary, so the
# first ~1 trading day of each of the 8 quarterly files is skipped (rolling NaN).
# That's ~7 extra warmup days vs a single continuous frame (~1.4% of ~500 days) —
# aggregate stats (win rate, avg pnl) are essentially unchanged; total trade count
# is ~1.4% lower. A (symbol, entry-day) dedup makes overlapping boundary dates safe.
PORTFOLIO=800.0
PER_TRADE=266.0
MAX_OPEN=3
ENTRY_WINDOW_MIN=90
ROLL_LOOKBACK=20
STOP_PCT=0.20
DATA_DIR='data/databento'
OUT_DIR='backtest-output'
NEEDED=['ts_event','symbol','open','high','low','close']

if not os.path.exists(OUT_DIR): os.makedirs(OUT_DIR)
files = sorted(glob.glob(os.path.join(DATA_DIR,'*.parquet')))
if not files:
    raise SystemExit('No databento parquet files')
print('Using files:', files, flush=True)

all_trades = []
seen_trades = set()   # (symbol, entry_day) — dedups boundary-overlap days


def market_open_dt(dt):
    return pd.Timestamp(year=dt.year, month=dt.month, day=dt.day, hour=13, minute=30, tz='UTC')


def process_symbol_frame(sym, df):
    """One symbol's rows from ONE file. Trade logic byte-identical to v2/v3, plus a
    (sym, entry-day) dedup so a boundary date present in two files can't double-trade."""
    df = df.sort_index()
    if len(df) < 200:
        return
    ohlc = df[['open','high','low','close']]
    ohlc_5 = ohlc.resample('5min').agg({'open':'first','high':'max','low':'min','close':'last'}).dropna()
    ohlc_15 = ohlc.resample('15min').agg({'open':'first','high':'max','low':'min','close':'last'}).dropna()
    if len(ohlc_15) < ROLL_LOOKBACK+5:
        return
    roll_high = ohlc_15['high'].rolling(ROLL_LOOKBACK).max()
    roll_low = ohlc_15['low'].rolling(ROLL_LOOKBACK).min()
    days = ohlc_15.index.normalize().unique()
    for day in days:
        day_start = market_open_dt(day.to_pydatetime())
        entry_cutoff = day_start + pd.Timedelta(minutes=ENTRY_WINDOW_MIN)
        day_bars_15 = ohlc_15[ (ohlc_15.index.normalize()==day) & (ohlc_15.index<=entry_cutoff) ]
        if day_bars_15.empty:
            continue
        for idx15, row15 in day_bars_15.iterrows():
            loc = ohlc_15.index.get_loc(idx15)
            if loc < ROLL_LOOKBACK:
                continue
            prev_high = roll_high.iloc[loc-1]
            prev_low = roll_low.iloc[loc-1]
            if pd.isna(prev_high) or pd.isna(prev_low):
                continue
            if row15['close'] > prev_high:
                level = prev_high; typ='long'
            elif row15['close'] < prev_low:
                level = prev_low; typ='short'
            else:
                continue
            candidate_5 = ohlc_5[(ohlc_5.index>idx15) & (ohlc_5.index<=entry_cutoff)]
            if candidate_5.empty:
                continue
            entered=False
            closes = candidate_5['close'].values
            times = candidate_5.index
            for j in range(len(closes)-1):
                if typ=='long' and closes[j]>level and closes[j+1]>level:
                    entry_time = times[j+1]; entry_price = closes[j+1]; entered=True; break
                if typ=='short' and closes[j]<level and closes[j+1]<level:
                    entry_time = times[j+1]; entry_price = closes[j+1]; entered=True; break
            if not entered:
                continue
            entry_day = pd.to_datetime(entry_time).normalize()
            if (sym, str(entry_day)) in seen_trades:   # boundary-overlap guard
                continue
            qty = int(floor(PER_TRADE / entry_price))
            if qty<=0:
                continue
            trades_today = [t for t in all_trades if pd.to_datetime(t['entry_time']).normalize()==entry_day]
            if len(trades_today) >= MAX_OPEN:
                continue
            stop_price = entry_price*(1-STOP_PCT) if typ=='long' else entry_price*(1+STOP_PCT)
            day_5bars = ohlc_5[ohlc_5.index.normalize()==day]
            after = day_5bars[day_5bars.index>=entry_time]
            exited=False
            for ts, br in after.iterrows():
                if typ=='long' and br['low'] <= stop_price:
                    exit_price = stop_price; exit_time=ts; pnl=(exit_price-entry_price)*qty; exited=True; break
                if typ=='short' and br['high'] >= stop_price:
                    exit_price = stop_price; exit_time=ts; pnl=(entry_price-exit_price)*qty; exited=True; break
            if not exited:
                exit_time = day_5bars.index[-1]
                exit_price = float(day_5bars.iloc[-1]['close'])
                pnl = (exit_price-entry_price)*qty if typ=='long' else (entry_price-exit_price)*qty
            all_trades.append({
                'symbol': sym, 'entry_time': str(entry_time), 'entry_price': float(entry_price),
                'qty': qty, 'type': typ, 'exit_time': str(exit_time),
                'exit_price': float(exit_price), 'pnl': float(pnl)
            })
            seen_trades.add((sym, str(entry_day)))
            break


for fi, fn in enumerate(files):
    print(f'File {fi+1}/{len(files)}: {os.path.basename(fn)}', flush=True)
    df = pq.read_table(fn, columns=NEEDED).to_pandas()
    # databento's ts_event may come back as a column OR as the index (per-file
    # pq.read_table restores it as the index from pandas metadata). Handle both.
    if 'ts_event' in df.columns:
        idx = pd.DatetimeIndex(pd.to_datetime(df['ts_event']))
        df = df.drop(columns=['ts_event'])
    else:
        idx = pd.DatetimeIndex(pd.to_datetime(df.index))
    if idx.tz is None:
        idx = idx.tz_localize('UTC')
    df.index = idx
    for sym, sub in df.groupby('symbol'):
        if not sym or not isinstance(sym, str):
            continue
        process_symbol_frame(sym, sub[['open','high','low','close']])
    del df
    gc.collect()

wins = sum(1 for t in all_trades if t['pnl']>0)
losses = sum(1 for t in all_trades if t['pnl']<=0)
summary = {
    'n_trades': len(all_trades),
    'wins': wins,
    'losses': losses,
    'win_rate': (wins/(wins+losses) if (wins+losses)>0 else None),
    'total_pnl': round(sum(t['pnl'] for t in all_trades), 2),
    'avg_pnl': round(sum(t['pnl'] for t in all_trades)/len(all_trades), 2) if all_trades else None,
    'longs': sum(1 for t in all_trades if t['type']=='long'),
    'shorts': sum(1 for t in all_trades if t['type']=='short'),
}
with open(os.path.join(OUT_DIR,'ict_backtest_trades_v4.json'),'w') as f:
    json.dump(all_trades,f,indent=2)
with open(os.path.join(OUT_DIR,'ict_backtest_summary_v4.json'),'w') as f:
    json.dump(summary,f,indent=2)
print('Done. Summary:', summary, flush=True)
