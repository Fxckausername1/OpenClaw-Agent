import os, glob, json
import pandas as pd
import pyarrow.parquet as pq
from math import floor

# Parameters (from user)
START=None
END=None
PORTFOLIO=800.0
PER_TRADE=266.0
MAX_OPEN=3
ENTRY_WINDOW_MIN=90
BAR_15=15
BAR_5=5
ROLL_LOOKBACK=20
STOP_PCT=0.20

DATA_DIR='data/databento'
OUT_DIR='backtest-output'
if not os.path.exists(OUT_DIR): os.makedirs(OUT_DIR)

# helpers
def market_open_dt(dt):
    return pd.Timestamp(year=dt.year, month=dt.month, day=dt.day, hour=13, minute=30, tz='UTC')

def market_close_dt(dt):
    return pd.Timestamp(year=dt.year, month=dt.month, day=dt.day, hour=20, minute=0, tz='UTC')

# load parquet files into one dataset (iterative to save memory)
files = sorted(glob.glob(os.path.join(DATA_DIR,'*.parquet')))
if not files:
    raise SystemExit('No databento parquet files found')
print('Files:', files)

# We'll process symbol-by-symbol: first gather symbols in all files
symbols = set()
for fn in files:
    pf = pq.ParquetFile(fn)
    # sample first few row groups for symbols
    for rg in range(min(4, pf.num_row_groups)):
        tbl = pf.read_row_group(rg, columns=['symbol'])
        s = tbl.column(0).to_pandas()
        symbols.update(s.unique())

symbols = sorted([s for s in symbols if s and isinstance(s,str)])
print('Symbols found:', len(symbols))

# function to stream symbol data across files
def read_symbol(sym):
    parts = []
    for fn in files:
        pf = pq.ParquetFile(fn)
        # attempt to read symbol by filtering; to avoid reading entire file, read and filter
        tbl = pf.read(columns=['symbol','open','high','low','close','ts_event'])
        df = tbl.to_pandas()
        if 'ts_event' in df.columns:
            df.index = pd.to_datetime(df['ts_event'])
            df = df.drop(columns=['ts_event'])
        else:
            df.index = df.index
        df = df[df['symbol']==sym]
        if not df.empty:
            parts.append(df)
    if not parts:
        return None
    df = pd.concat(parts)
    df = df.sort_index()
    return df

# Backtest state
all_trades = []
portfolio = PORTFOLIO
equity_curve = []

# iterate symbols
for i,sym in enumerate(symbols):
    if (i+1) % 20 == 0:
        print(f'Processing {i+1}/{len(symbols)}: {sym}')
    df = read_symbol(sym)
    if df is None or len(df) < 200:
        continue
    # ensure tz-aware index
    if df.index.tzinfo is None:
        df.index = df.index.tz_localize('UTC')
    # assume input is 1-minute (or irregular) bars; resampleagg
    # resample to 5m and 15m
    ohlc = df[['open','high','low','close']]
    ohlc_5 = ohlc.resample('5T').agg({'open':'first','high':'max','low':'min','close':'last'}).dropna()
    ohlc_15 = ohlc.resample('15T').agg({'open':'first','high':'max','low':'min','close':'last'}).dropna()
    if len(ohlc_15) < ROLL_LOOKBACK+5:
        continue
    # compute rolling highs/lows on 15m
    roll_high = ohlc_15['high'].rolling(ROLL_LOOKBACK).max()
    roll_low = ohlc_15['low'].rolling(ROLL_LOOKBACK).min()
    # we'll iterate days
    days = ohlc_15.index.normalize().unique()
    open_trades = []  # active trades list of dicts
    for day in days:
        # identify 15m bars for that day
        day_mask_15 = (ohlc_15.index.normalize() == day)
        day_bars_15 = ohlc_15[day_mask_15]
        if day_bars_15.empty:
            continue
        day_start = market_open_dt(day.to_pydatetime())
        entry_cutoff = day_start + pd.Timedelta(minutes=ENTRY_WINDOW_MIN)
        # find breakout signals in first N minutes on 15m
        for idx15, row15 in day_bars_15.iterrows():
            if idx15 > entry_cutoff:
                break
            idx_loc = ohlc_15.index.get_loc(idx15)
            if idx_loc < ROLL_LOOKBACK:
                continue
            prev_high = roll_high.iloc[idx_loc-1]
            prev_low = roll_low.iloc[idx_loc-1]
            if pd.isna(prev_high) or pd.isna(prev_low):
                continue
            # breakout
            if row15['close'] > prev_high:
                level = prev_high
                typ = 'long'
            elif row15['close'] < prev_low:
                level = prev_low
                typ = 'short'
            else:
                continue
            # entry on 5m: find first 5m bar after idx15
            # map idx15 timestamp to 5m index
            candidate_5 = ohlc_5[ohlc_5.index > idx15]
            if candidate_5.empty:
                continue
            # enforce entry within entry_cutoff
            candidate_5 = candidate_5[candidate_5.index <= entry_cutoff]
            if candidate_5.empty:
                continue
            # require two-bar confirmation on 5m after entry (two consecutive closes beyond level)
            entered = False
            for j in range(len(candidate_5)-1):
                e1 = candidate_5.iloc[j]
                e2 = candidate_5.iloc[j+1]
                if typ=='long':
                    if (e1['close'] > level) and (e2['close'] > level):
                        entry_time = candidate_5.index[j+1]
                        entry_price = e2['close']
                        entered = True
                        break
                else:
                    if (e1['close'] < level) and (e2['close'] < level):
                        entry_time = candidate_5.index[j+1]
                        entry_price = e2['close']
                        entered = True
                        break
            if not entered:
                continue
            # position sizing
            qty = int(floor(PER_TRADE / entry_price))
            if qty <= 0:
                continue
            # enforce max open trades
            if len(open_trades) >= MAX_OPEN:
                continue
            # create trade
            stop_price = entry_price * (1 - STOP_PCT) if typ=='long' else entry_price * (1 + STOP_PCT)
            trade = {
                'symbol': sym,
                'entry_time': str(entry_time),
                'entry_price': float(entry_price),
                'qty': qty,
                'type': typ,
                'stop_price': float(stop_price),
                'exit_time': None,
                'exit_price': None,
                'pnl': None
            }
            open_trades.append(trade)
            # remove ability to re-enter same day for same symbol
            break
        # now simulate intraday 5m price movement to exit trades same day or stop
        day_mask_5 = (ohlc_5.index.normalize() == day)
        day_bars_5 = ohlc_5[day_mask_5]
        if day_bars_5.empty:
            continue
        to_remove = []
        for t in open_trades:
            # iterate bars after entry_time
            bars_after = day_bars_5[day_bars_5.index >= pd.to_datetime(t['entry_time'])]
            exited=False
            for ts, br in bars_after.iterrows():
                # check stop
                if t['type']=='long' and br['low'] <= t['stop_price']:
                    exit_price = t['stop_price']
                    t['exit_time']=str(ts)
                    t['exit_price']=float(exit_price)
                    t['pnl'] = (exit_price - t['entry_price'])*t['qty']
                    exited=True
                    break
                if t['type']=='short' and br['high'] >= t['stop_price']:
                    exit_price = t['stop_price']
                    t['exit_time']=str(ts)
                    t['exit_price']=float(exit_price)
                    t['pnl'] = (t['entry_price'] - exit_price)*t['qty']
                    exited=True
                    break
            if not exited:
                # exit at day close (last bar close)
                last_close = float(day_bars_5.iloc[-1]['close'])
                t['exit_time']=str(day_bars_5.index[-1])
                t['exit_price']=last_close
                if t['type']=='long': t['pnl'] = (last_close - t['entry_price'])*t['qty']
                else: t['pnl'] = (t['entry_price'] - last_close)*t['qty']
            all_trades.append(t)
        # clear open_trades for next day (we don't carry positions)
        open_trades=[]

# summarize
wins = sum(1 for t in all_trades if t['pnl'] and t['pnl']>0)
losses = sum(1 for t in all_trades if t['pnl'] and t['pnl']<=0)
total_pnl = sum((t['pnl'] or 0) for t in all_trades)
summary = {
    'n_symbols': len(symbols),
    'n_trades': len(all_trades),
    'wins': wins,
    'losses': losses,
    'win_rate': (wins/(wins+losses) if (wins+losses)>0 else None),
    'total_pnl': total_pnl
}
with open(os.path.join(OUT_DIR,'ict_backtest_trades.json'),'w') as f:
    json.dump(all_trades,f,indent=2)
with open(os.path.join(OUT_DIR,'ict_backtest_summary.json'),'w') as f:
    json.dump(summary,f,indent=2)
print('Done. Summary:', summary)
