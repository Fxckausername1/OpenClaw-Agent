import yfinance as yf
from datetime import datetime

tickers = ['AAPL','MSFT','AMZN','GOOGL','TSLA']
for t in tickers:
    print('---', t)
    try:
        df = yf.download(t, period='2d', interval='1m', progress=False, threads=False)
        if df is None or df.empty:
            print('minute data: NONE')
        else:
            print('minute data: OK, last_close=', df['Close'].iloc[-1])
    except Exception as e:
        print('minute data: ERROR', e)
    try:
        tk = yf.Ticker(t)
        exps = tk.options
        if exps:
            print('options expirations count=', len(exps), 'next=', exps[0])
        else:
            print('options: NONE')
    except Exception as e:
        print('options: ERROR', e)
