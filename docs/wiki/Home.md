# Portfolio Tracker Wiki

This wiki explains how to use the portfolio tracker day to day.

## Pages

- [Usage Guide](Usage.md): setup, daily workflow, trades, dividends, reports, and privacy mode.
- [Import Profiles](Import-Profiles.md): how profile-based CSV imports work and how duplicates are handled.
- [Troubleshooting](Troubleshooting.md): common fixes for setup, market data, imports, and display issues.

## What The App Tracks

- Stock and ETF buy/sell trades
- Japanese mutual fund transactions
- Dividend income and tax withholding
- Watchlist targets and stops
- Portfolio value, daily moves, realized and unrealized P&L
- Japanese tax report data using moving-average cost basis

Your portfolio data is stored locally in SQLite under `data/`. CSV exports, imported broker reports, `.env`, caches, and database files should stay out of Git.
