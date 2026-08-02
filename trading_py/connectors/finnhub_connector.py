import os
import time
import requests
import pandas as pd
from datetime import datetime

class FinnhubClient:
    """Simple Finnhub client for historical candles."""
    BASE = 'https://finnhub.io/api/v1'

    def __init__(self, api_key=None):
        self.api_key = api_key or os.environ.get('FINNHUB_API_KEY')
        # fallback to credentials file
        if not self.api_key:
            try:
                import json
                cred_path = os.path.join(os.path.dirname(__file__), '..', 'credentials.json')
                with open(cred_path, 'r') as f:
                    j = json.load(f)
                    self.api_key = j.get('finnhub_api_key')
            except Exception:
                pass
        if not self.api_key:
            raise RuntimeError('Finnhub API key not found. Set FINNHUB_API_KEY or trading-py/credentials.json')

    def get_candles(self, symbol, resolution='D', _from=None, to=None):
        """Fetch candle data. resolution: 1,5,15,30,60,D,W,M
        _from and to are unix timestamps (int). If omitted, _from defaults to 365 days ago and to now.
        Returns pandas.DataFrame with t,o,h,l,c,v index=datetime
        """
        if to is None:
            to = int(time.time())
        if _from is None:
            _from = int(time.time()) - 86400 * 365

        url = f"{self.BASE}/stock/candle"
        params = {
            'symbol': symbol,
            'resolution': resolution,
            'from': int(_from),
            'to': int(to),
            'token': self.api_key,
        }
        r = requests.get(url, params=params, timeout=30)
        r.raise_for_status()
        j = r.json()
        if j.get('s') != 'ok':
            raise RuntimeError(f"Finnhub failed: {j}")
        df = pd.DataFrame({
            't': j['t'],
            'o': j['o'],
            'h': j['h'],
            'l': j['l'],
            'c': j['c'],
            'v': j['v'],
        })
        df['date'] = pd.to_datetime(df['t'], unit='s')
        df = df.set_index('date').drop(columns=['t'])
        df = df.rename(columns={'o':'Open','h':'High','l':'Low','c':'Close','v':'Volume'})
        return df

