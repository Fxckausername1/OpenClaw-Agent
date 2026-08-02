Python trading tools (local venv)

Quick start (run on your machine where you created the venv):

1. Activate your venv
   - Linux/macOS: . trading-venv/bin/activate
   - Windows PowerShell: .\trading-venv\Scripts\Activate.ps1

2. Install dependencies if you haven't already:
   pip install -r trading-py/requirements.txt

3. Examples:
   - Fetch historical data:
       python trading-py/scripts/fetch_ohlcv.py --symbol AAPL --period 1y --outfile data/AAPL.csv

   - Run a simple backtest (SMA crossover template):
       python trading-py/scripts/simple_backtest.py --symbol AAPL --period 2y

Output and artifacts go into trading-py/output/ by default.

Notes:
- These are templates and safe to run (no live trading). If you want paper/live trading, we'll add connectors and API-key handling next.
- For ICT/SMC-specific rules, tell me which rule set you want implemented and I'll extend the backtester.

