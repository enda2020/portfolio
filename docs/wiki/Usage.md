# Usage Guide

## Start The App

### Docker Compose

```bash
docker-compose pull
docker-compose up -d
```

Open:

```text
http://localhost:5001
```

### Local Development

```bash
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
python app.py
```

Open:

```text
http://localhost:5001
```

## First-Time Setup

Create `.env` in the project root:

```text
FLASK_SECRET_KEY=replace_with_a_generated_secret
```

Optional daily reset defaults for a fresh database:

```text
PORTFOLIO_DAILY_RESET_TIME=08:30
PORTFOLIO_DAILY_RESET_TIMEZONE=Asia/Tokyo
```

After the app is running, use `Tools -> Config` to manage:

- Daily reset time and timezone
- Brokers
- Account names
- Tax statuses

## Daily Workflow

1. Add new trades from `Add -> Stock / ETF Trade` or `Add -> Mutual Fund Transaction`.
2. Add dividend payments from `Dividends -> Add Dividend`.
3. Review the dashboard on `Home`.
4. Use filters for broker, account, tax status, and currency when needed.
5. Use `Health` to review concentration, performance, dividend summaries, and portfolio history.
6. Use `Tools -> Tax Report` for year-end realized gain/loss reporting.

## Add Stock Or ETF Trades

Go to `Add -> Stock / ETF Trade`.

Required fields:

- Symbol
- Name
- Buy or sell
- Quantity
- Price
- Currency
- Trade date
- Broker
- Account name
- Tax status

For foreign-currency trades, enter the trade-date FX rate. Fees can be entered separately with their own currency.

## Add Mutual Fund Transactions

Go to `Add -> Mutual Fund Transaction`.

Use the fund code, fund name, buy/sell type, executed units, NAV per 10,000 units, trade date, broker, account name, and tax status.

Japanese mutual funds are priced per 10,000 units in the app, matching the common NAV convention.

## Add Dividends

Go to `Dividends -> Add Dividend`.

Required fields:

- Symbol
- Name
- Payment date
- Currency
- Broker
- Account name
- Tax status

You can enter either:

- Gross amount directly
- Shares and amount per share, letting the app calculate gross amount

Dividend fields include:

- Other tax withheld
- Foreign tax withheld
- Japanese income tax withheld
- Japanese local tax withheld
- Deductible interest
- Source country
- Security type
- Filing treatment
- FX rate for JPY conversion

The Dividends page shows:

- Dividend income year to date
- Average monthly dividend income year to date
- Total dividend income
- Filtered gross, tax, and net totals

## Import CSV Files

Go to `Tools -> Bulk Upload`.

1. Choose a CSV file.
2. Select one or more import profiles.
3. Upload.
4. Review the import summary, validation errors, and duplicate details.

If the same record already exists, the importer ignores it and shows which row was skipped.

See [Import Profiles](Import-Profiles.md) for profile details.

## Dashboard

The `Home` page shows:

- Total portfolio value
- Today P&L
- Unrealized P&L
- Realized P&L
- Current holdings
- Holding-level trade history
- Watchlist

Use `Refresh Prices` to clear cached market data and fetch fresh quotes.

## Portfolio Health

The `Health` page shows:

- Concentration checks
- Sector exposure
- Rebalance ideas
- Portfolio performance snapshots
- Dividend income summary
- Portfolio value history
- P&L history

Use `Health -> Settings` to tune concentration thresholds.

## Tax Report

Go to `Tools -> Tax Report`.

Choose a year and optional filters for broker, account name, and tax status. The report calculates realized gains/losses in JPY using moving-average cost basis.

## Privacy Mode

Use the eye icon in the navigation bar to mask or reveal sensitive monetary values. The setting is saved in your browser.

## Data Storage

Local data lives in:

```text
data/holdings.db
```

Keep these private:

- `.env`
- `data/`
- `cache/`
- Imported broker CSV files
- Exported CSV files

These are already covered by `.gitignore`.
