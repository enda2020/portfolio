# Import Profiles

Import profiles let the app read broker or custom CSV files without changing code for each file layout.

Open them from:

```text
Tools -> Import Profiles
```

Use them from:

```text
Tools -> Bulk Upload
```

## Built-In Profile Types

Profiles can import one of these instrument types:

- `stock`
- `mutual_fund`
- `dividend`

Each profile defines:

- File encoding
- Header row number
- Row filters
- Field mappings
- Default values

## Row Filters

Row filters choose which CSV rows the profile should import.

Supported operators:

- `equals`
- `not_equals`
- `contains`
- `in`
- `not_in`

Example:

```json
[
  {"column": "Product", "equals": "Stock"},
  {"column": "Action", "in": ["Buy", "Sell"]}
]
```

## Mappings

Mappings convert source CSV columns into app fields.

Simple column mapping:

```json
{
  "symbol": "Ticker"
}
```

Mapping with a transform:

```json
{
  "quantity": {"column": "Quantity", "transform": "number"}
}
```

Mapping multiple columns into one value:

```json
{
  "fee_amount": {
    "columns": ["Commission", "Tax", "Other Fees"],
    "transform": "abs_sum"
  }
}
```

## Supported Transforms

- `strip`: trim whitespace
- `number`: parse a number
- `number_zero_none`: parse a number, but treat zero as blank
- `abs_number`: parse a number and use its absolute value
- `abs_sum`: sum multiple numeric columns and use the absolute value
- `currency_from_value`: detect `USD` or `JPY` from a value
- `date_slash`: convert `YYYY/MM/DD` to `YYYY-MM-DD`
- `monex_jp_symbol`: trim a trailing zero from five-digit Japanese stock symbols

## Defaults

Defaults fill fields that are not in the CSV.

Example:

```json
{
  "currency": "JPY",
  "broker": "Default Broker",
  "account_name": "Default",
  "tax_status": "Taxable"
}
```

## Duplicate Protection

When importing, the app checks whether a parsed record already exists before inserting it.

Duplicates are detected with a natural-key comparison. That means the app compares the meaningful fields of the record, such as symbol, transaction type, quantity, price, date, broker, account, tax status, fees, and dividend tax fields.

If a duplicate is found:

- It is ignored.
- The import summary counts it as a duplicate.
- A detail line identifies the row and record that was skipped.

Because broker transaction reports may not include a true transaction ID, two genuinely separate records with identical values can be treated as duplicates. That tradeoff protects against accidentally importing the same report twice.

## Editing Built-In Profiles

Built-in profiles are seeded by the app. If a built-in profile is updated in code, the app may update it on startup.

For custom layouts, create a new profile with your own name instead of editing a built-in profile.
