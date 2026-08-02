#!/usr/bin/env python3
"""Very small SMA crossover backtest template (no external backtester required).
Usage: python simple_backtest.py --symbol AAPL --period 2y --short 10 --long 50
"""
import argparse
import pandas as pd
from trading_py_helper import ensure_data

def run_backtest(df, short=10, long=50):
    df = df.copy()
    df['sma_short'] = df['Close'].rolling(short).mean()
    df['sma_long'] = df['Close'].rolling(long).mean()
    df['signal'] = 0
    df.loc[df['sma_short'] > df['sma_long'], 'signal'] = 1
    df['position'] = df['signal'].diff().fillna(0)

    cash = 10000.0
    position = 0.0
    entry_price = 0.0
    trades = []

    for i, row in df.iterrows():
        if row['position'] == 1:  # buy
            entry_price = row['Close']
            position = cash / entry_price
            cash = 0.0
            trades.append({'date': i, 'type': 'buy', 'price': entry_price})
        elif row['position'] == -1 and position>0:  # sell
            exit_price = row['Close']
            cash = position * exit_price
            trades.append({'date': i, 'type': 'sell', 'price': exit_price, 'pnl': cash - 10000.0})
            position = 0

    # final portfolio value
    final = cash if cash>0 else position * df.iloc[-1]['Close']
    return trades, final

def main():
    p = argparse.ArgumentParser()
    p.add_argument('--symbol', required=True)
    p.add_argument('--period', default='2y')
    p.add_argument('--short', type=int, default=10)
    p.add_argument('--long', type=int, default=50)
    args = p.parse_args()

    df = ensure_data(args.symbol, args.period)
    trades, final = run_backtest(df, args.short, args.long)
    print('Trades:')
    for t in trades:
        print(t)
    print('Final portfolio value:', final)

if __name__ == '__main__':
    main()

