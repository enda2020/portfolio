# Troubleshooting

## App Does Not Start

Check that dependencies are installed:

```bash
pip install -r requirements.txt
```

Check that `.env` exists and contains:

```text
FLASK_SECRET_KEY=replace_with_a_generated_secret
```

For Docker, check logs:

```bash
docker-compose logs -f
```

## Market Data Looks Stale

Market data is cached for five minutes.

On the dashboard, use:

```text
Refresh Prices
```

If live market data is unavailable, the app may show cost-basis fallback values and skip the daily history snapshot.

You can also clear cached quotes from:

```text
Tools -> Data Maintenance -> Clear Market Cache
```

## Huge P&L Move After A Stock Split

If Yahoo Finance serves an old pre-split quote after you adjusted your trades, the dashboard can show a very large temporary loss.

Use:

```text
Tools -> Data Maintenance -> Manual Price Overrides
```

Add an override for the affected symbol, currency, and instrument type with the correct post-split current price. The dashboard and Health page will use the manual quote until you remove it.

Once Yahoo Finance has the correct split-adjusted quote, remove the override and clear the market cache.

## Need To Apply A Stock Split

Use:

```text
Tools -> Corporate Actions
```

The Stock Split workflow previews all affected pre-effective-date trades, then applies the split by multiplying quantities and dividing prices.

Do not apply a split if you already manually edited those trades. Applying both manual edits and the corporate action tool will adjust the same trades twice.

## Today P&L Looks Wrong After A Weekend

The app uses a daily reset time. By default this is 08:30 in Asia/Tokyo.

Change it from:

```text
Tools -> Config
```

After reset, stale daily changes from a previous market day are treated as zero.

## CSV Import Fails

Check:

- The selected import profile matches the CSV format.
- The file encoding matches the profile.
- The header row number is correct.
- Required app fields are mapped or provided as defaults.
- Broker, account name, and tax status values exist in `Tools -> Config`.

Import errors show row numbers and validation messages.

## CSV Import Says Duplicates Were Ignored

This means records already exist in the database with the same parsed values.

The import summary includes duplicate details such as row number, symbol, transaction type, date, quantity, and broker/account/tax status.

## Filters Hide Expected Data

Check filters on:

- Home
- Dividends
- Tax Report

Choose `All` for broker, account, tax status, currency, year, or filing treatment to remove filters.

## Monetary Values Are Hidden

Click the eye icon in the navigation bar. Privacy mode is saved in your browser.

## Database Backup

The main database is:

```text
data/holdings.db
```

Stop the app before copying the database for backup.
