import os, glob, json
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pyarrow.dataset as ds
import pyarrow.compute as pc
from math import floor

# Parameters
PORTFOLIO=800.0
PER_TRADE=266.0
MAX_OPEN=3
ENTRY_WINDOW_MIN=90
ROLL_LOOKBACK=20
STOP_PCT=0.20
DATA_DIR='data/databento'
OUT_DIR='backtest-output'

if not os.path.exists(OUT_DIR): os.makedirs(OUT_DIR)
files = sorted(glob.glob(os.path.join(DATA_DIR,'*.parquet')))
if not files:
    raise SystemExit('No databento parquet files')
print('Using files:', files)

# build symbols list by scanning each file's symbol column metadata efficiently
symbols = set()
for fn in files:
    pf = pq.ParquetFile(fn)
    # read symbol column in each row group
    for rg in range(pf.num_row_groups):
        tbl = pf.read_row_group(rg, columns=['symbol'])
        s = tbl.column(0).to_pandas()
        symbols.update(s.unique())
symbols = sorted([s for s in symbols if s and isinstance(s,str)])
print('Found symbols:', len(symbols))

# open dataset once
dataset = ds.dataset(files, format='parquet')

all_trades = []

# helpers for market times
import pandas as pd
from datetime import datetime

def market_open_dt(dt):
    return pd.Timestamp(year=dt.year, month=dt.month, day=dt.day, hour=13, minute=30, tz='UTC')

for idx,sym in enumerate(symbols):
    if (idx+1) % 50 == 0:
        print(f'Processing {idx+1}/{len(symbols)}: {sym}')
    # read only rows matching symbol using dataset filter
    filt = (pc.field('symbol') == sym)
    try:
        table = dataset.to_table(filter=filt)
    except Exception:
        continue
    if table.num_rows == 0:
        continue
    df = table.to_pandas()
    # convert ts_event index if present
    if 'ts_event' in df.columns:
        df.index = pd.to_datetime(df['ts_event']).tz_localize('UTC') if df['ts_event'].dtype == object else pd.to_datetime(df['ts_event'])
        df = df.drop(columns=['ts_event'])
    else:
        df.index = pd.to_datetime(df.index)
    df = df.sort_index()
    if len(df) < 200:
        continue
    ohlc = df[['open','high','low','close']]
    # resample
    ohlc_5 = ohlc.resample('5min').agg({'open':'first','high':'max','low':'min','close':'last'}).dropna()
    ohlc_15 = ohlc.resample('15min').agg({'open':'first','high':'max','low':'min','close':'last'}).dropna()
    if len(ohlc_15) < ROLL_LOOKBACK+5:
        continue
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
            # find 5min candidate bars after idx15 up to cutoff
            candidate_5 = ohlc_5[(ohlc_5.index>idx15) & (ohlc_5.index<=entry_cutoff)]
            if candidate_5.empty:
                continue
            entered=False
            # require two consecutive 5min closes beyond level
            closes = candidate_5['close'].values
            times = candidate_5.index
            for j in range(len(closes)-1):
                if typ=='long' and closes[j]>level and closes[j+1]>level:
                    entry_time = times[j+1]; entry_price = closes[j+1]; entered=True; break
                if typ=='short' and closes[j]<level and closes[j+1]<level:
                    entry_time = times[j+1]; entry_price = closes[j+1]; entered=True; break
            if not entered:
                continue
            qty = int(floor(PER_TRADE / entry_price))
            if qty<=0:
                continue
            # check no more than MAX_OPEN trades across same day across symbols
            trades_today = [t for t in all_trades if pd.to_datetime(t['entry_time']).normalize()==pd.to_datetime(entry_time).normalize()]
            if len(trades_today) >= MAX_OPEN:
                continue
            stop_price = entry_price*(1-STOP_PCT) if typ=='long' else entry_price*(1+STOP_PCT)
            # simulate until EOD bars
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
            trade = {
                'symbol': sym,
                'entry_time': str(entry_time),
                'entry_price': float(entry_price),
                'qty': qty,
                'type': typ,
                'exit_time': str(exit_time),
                'exit_price': float(exit_price),
                'pnl': float(pnl)
            }
            all_trades.append(trade)
            # only one trade per symbol per day
            break

# summary
wins = sum(1 for t in all_trades if t['pnl']>0)
losses = sum(1 for t in all_trades if t['pnl']<=0)
summary = {
    'n_symbols': len(symbols),
    'n_trades': len(all_trades),
    'wins': wins,
    'losses': losses,
    'win_rate': (wins/(wins+losses) if (wins+losses)>0 else None),
    'total_pnl': sum(t['pnl'] for t in all_trades)
}
with open(os.path.join(OUT_DIR,'ict_backtest_trades_v2.json'),'w') as f:
    json.dump(all_trades,f,indent=2)
with open(os.path.join(OUT_DIR,'ict_backtest_summary_v2.json'),'w') as f:
    json.dump(summary,f,indent=2)
print('Done. Summary:', summary)
