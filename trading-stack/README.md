Trading stack (node)

Installed packages (location: ./node_modules):
- ccxt (exchange connectors)
- axios (HTTP requests)
- technicalindicators (indicators)
- yahoo-finance2 (equities/market data)
- coingecko-api (crypto price data)

Notes:
- Free data sources recommended:
  - Yahoo Finance (via yahoo-finance2) — no API key required for most equities/historical data
  - CoinGecko (coingecko-api) — free crypto market data
  - CCXT supports many exchanges (some require API keys for live/private endpoints)
  - Alpha Vantage / Finnhub are also free tiers but require API keys (I can scaffold connectors)

Next steps I can do for you:
- Scaffold connectors to fetch OHLCV and indicators
- Create a simple backtester template for ICT/SMC rules
- Build alerting/cron jobs for signals
- Add Python stack (venv + yfinance + backtrader) if you want — needs python3-venv installed on the host

To run locally:
cd trading-stack
node scripts/sample_fetch.js  # (I can add sample scripts on request)

If you want me to scaffold example scripts for data fetch, backtest, or alerts, tell me which first (data source, exchanges, timeframe, and whether you want paper-trade or just signal alerts).