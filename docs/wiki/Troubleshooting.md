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
