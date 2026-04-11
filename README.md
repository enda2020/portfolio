# Trade-Based Stock Portfolio Tracker for Japanese Tax Reporting

A web application for tracking your stock portfolio by logging individual trades. It is designed to help with Japanese tax reporting by using the moving-average cost basis method.

## Features

- **Trade Logging**: Record individual buy/sell transactions with details like broker, fees, and currency exchange rates (TTS/TTB).
- **Portfolio Dashboard**:
    - Automatically calculates and displays a summary of your current holdings.
    - JPY-first view of Total Value, Unrealized P&L, and Realized P&L.
    - Line chart showing portfolio value history over time.
    - Interactive pie chart to visualize portfolio composition by JPY value, with filters for broker and currency.
- **Japanese Tax Reporting**:
    - Generates a detailed, year-end tax report for a selected financial year.
    - Calculates proceeds, acquisition costs, and profit/loss in JPY using the required moving-average method.
    - Provides a transparent, expandable breakdown for each sale, showing the exact data and calculations used.
- **Data Management**:
    - Bulk upload trades from a CSV file with robust validation.
    - Export all trades to a CSV file for mass editing or backup.
- **Live Market Data**: Fetches current stock prices and USD/JPY exchange rates from Yahoo Finance on-demand.

## Installation

1. Clone or download this repository
2. Create a virtual environment:
    ```bash
    python -m venv venv
    source venv/bin/activate  # On Windows: venv\Scripts\activate
    ```
3. Install dependencies:
    ```bash
    pip install -r requirements.txt
    ```
4. **Important**: If you have an old `holdings.db` file from a previous version, delete it. The application will create a new one with the correct structure.

## Usage

1. Run the application:
    ```bash
    python app.py
    ```
2. Open your browser and go to `http://localhost:5001`

## Database

The application uses SQLite for data storage. All data is stored in the `holdings.db` file, which is excluded from version control by `.gitignore`. It contains two main tables:
- `trades`: Logs every individual buy and sell transaction.
- `portfolio_history`: Stores a daily snapshot of the total portfolio value for historical charting.

## Main Routes

- `GET /` - The main portfolio summary dashboard.
- `GET /trades` - A page to view, edit, and delete all recorded trades.
- `GET, POST /add_trade` - A form to add a new trade transaction.
- `GET, POST /tax_report` - A tool to generate a detailed tax report for a specific year.
- `GET, POST /bulk_upload` - A page to upload a CSV file of trades.
- `GET /export_trades` - An endpoint to download all trades as a CSV file.

## File Structure

```
.
├── app.py                 # Main Flask application
├── requirements.txt       # Python dependencies
├── holdings.db            # SQLite database
├── templates/             # HTML templates
│   ├── base.html          # Base template
│   ├── index.html         # Main dashboard
│   ├── add_stock.html     # Add stock form
│   └── edit_stock.html    # Edit stock form
└── README.md              # This file
```

## Development

The application includes:
- Background thread for automatic price updates
- Currency conversion functions
- Responsive Bootstrap UI
- JSON API for data exchange
- Form validation and error handling

## Notes

- For demonstration purposes, stock prices are mocked. In a production environment, you would integrate with a real stock API.
- The application automatically updates stock prices every 5 minutes.
- All data is stored locally in SQLite database.
