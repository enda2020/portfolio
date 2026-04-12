from flask import Flask, render_template, request, redirect, url_for, jsonify, flash, Response
import sqlite3
import yfinance as yf
from datetime import datetime, timedelta
import csv
import io
import os
from flask_caching import Cache

app = Flask(__name__)
app.secret_key = 'a_super_secret_key_for_flash_messages' # In production, use a more secure, environment-based key.

# Get the absolute path of the directory where this script is located
basedir = os.path.abspath(os.path.dirname(__file__))

# --- Caching Configuration ---
# Use FileSystemCache to ensure the cache is shared between Gunicorn workers.
# The cache will be stored in a 'cache' directory in the project root.
CACHE_CONFIG = {
    "CACHE_TYPE": "FileSystemCache",
    "CACHE_DIR": os.path.join(basedir, "cache"),
    "CACHE_DEFAULT_TIMEOUT": 300  # Default timeout 5 minutes (300 seconds)
}
app.config.from_mapping(CACHE_CONFIG)
cache = Cache(app)

# Define paths relative to the application's location to ensure they are always correct
DATA_DIR = os.path.join(basedir, 'data')
DATABASE = os.path.join(DATA_DIR, 'holdings.db')
BROKERS = ['Monex', 'Interactive Brokers']

@cache.memoize()
def get_exchange_rate():
    """Fetches the current USD/JPY exchange rate."""
    try:
        # Use a longer period to be robust against weekends/holidays
        history = yf.Ticker("JPY=X").history(period="5d")
        if not history.empty:
            return float(history['Close'].iloc[-1])
    except Exception as e:
        print(f"Could not fetch exchange rate: {e}. Defaulting to 150.")
    return 150.0 # Return a default value if API fails

@cache.memoize()
def get_stock_price(symbol, currency):
    """Fetches the current price of a stock symbol."""
    # Yahoo Finance uses a ".T" suffix for stocks on the Tokyo Stock Exchange
    if currency == 'JPY':
        symbol += '.T'
    try:
        # Use a longer period to be robust against weekends/holidays
        history = yf.Ticker(symbol).history(period="5d")
        if not history.empty:
            return float(history['Close'].iloc[-1])
    except Exception as e:
        print(f"Could not fetch price for {symbol}: {e}")
    
    # Return 0 if price isn't found
    return 0.0

# Database setup
def init_db():
    """Initializes the database, creating the data directory and tables if they don't exist."""
    os.makedirs(DATA_DIR, exist_ok=True) # Ensure the data directory exists
    with sqlite3.connect(DATABASE) as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                name TEXT NOT NULL,
                trade_type TEXT NOT NULL, -- 'BUY' or 'SELL'
                quantity REAL NOT NULL,
                price REAL NOT NULL,
                currency TEXT NOT NULL,
                trade_date TEXT NOT NULL,
                broker TEXT,
                fx_rate REAL,
                fee_amount REAL,
                fee_currency TEXT
            )
        ''')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS portfolio_history (
                date TEXT PRIMARY KEY,
                value_usd REAL NOT NULL,
                value_jpy REAL NOT NULL
            )
        ''')
    print("Database tables ensured to exist.")

def _calculate_portfolio_summary(trades, exchange_rate):
    """
    Helper function to perform the main portfolio calculation.
    This is refactored out of the index() route for reuse.
    """
    # --- Aggregation Logic ---
    holdings = {}
    for trade in trades:
        # Aggregate by both symbol and broker for more granular tracking
        key = (trade['symbol'], trade['broker'])
        if key not in holdings:
            holdings[key] = {
                'symbol': trade['symbol'],
                'broker': trade['broker'],
                'name': trade['name'],
                'currency': trade['currency'],
                'quantity': 0,
                'total_cost': 0,
                'total_shares_bought': 0,
                'realized_pnl_native': 0
            }
        
        # Fee calculation
        fee_amount = trade['fee_amount'] or 0.0
        fee_currency = trade['fee_currency']
        trade_currency = trade['currency']

        fee_in_native_currency = fee_amount
        if fee_currency and fee_currency != trade_currency and exchange_rate > 0:
            if fee_currency == 'JPY' and trade_currency == 'USD':
                fee_in_native_currency = fee_amount / exchange_rate
            elif fee_currency == 'USD' and trade_currency == 'JPY':
                fee_in_native_currency = fee_amount * exchange_rate

        if trade['trade_type'] == 'BUY':
            holdings[key]['quantity'] += trade['quantity']
            holdings[key]['total_cost'] += (trade['quantity'] * trade['price']) + fee_in_native_currency
            holdings[key]['total_shares_bought'] += trade['quantity']
        elif trade['trade_type'] == 'SELL':
            avg_cost_basis = 0
            if holdings[key]['total_shares_bought'] > 0:
                avg_cost_basis = holdings[key]['total_cost'] / holdings[key]['total_shares_bought']
            
            cost_of_shares_sold = trade['quantity'] * avg_cost_basis
            proceeds = (trade['quantity'] * trade['price']) - fee_in_native_currency
            
            holdings[key]['realized_pnl_native'] += proceeds - cost_of_shares_sold
            holdings[key]['quantity'] -= trade['quantity']

    # --- Enrichment and Summary ---
    summary_list = []
    total_portfolio_value_usd = 0.0
    total_realized_pnl_usd = 0.0
    total_unrealized_pnl_usd = 0.0
    
    for key, data in holdings.items():
        if data['quantity'] <= 0.00001: # Use a small epsilon for float comparison
            continue # Skip fully sold-off stocks

        # Calculate average cost basis
        if data['total_shares_bought'] > 0:
            data['avg_cost_basis'] = data['total_cost'] / data['total_shares_bought']
        else:
            data['avg_cost_basis'] = 0

        # Get current market data
        data['current_price'] = get_stock_price(data['symbol'], data['currency'])
        current_value_native = data['quantity'] * data['current_price']
        
        # Calculate P&L
        cost_of_holding = data['quantity'] * data['avg_cost_basis']
        data['pnl_native'] = current_value_native - cost_of_holding

        # Convert to USD
        if data['currency'] == 'JPY' and exchange_rate > 0:
            data['current_value_usd'] = current_value_native / exchange_rate
            data['pnl_usd'] = data['pnl_native'] / exchange_rate
        else:
            data['current_value_usd'] = current_value_native
            data['pnl_usd'] = data['pnl_native']
        
        # Add JPY values for the table and chart
        data['current_value_jpy'] = data['current_value_usd'] * exchange_rate
        data['pnl_jpy'] = data['pnl_usd'] * exchange_rate

        total_portfolio_value_usd += data['current_value_usd']
        total_unrealized_pnl_usd += data['pnl_usd']
        
        # Add to total realized pnl
        realized_pnl_native = data['realized_pnl_native']
        if data['currency'] == 'JPY' and exchange_rate > 0:
            total_realized_pnl_usd += realized_pnl_native / exchange_rate
        else:
            total_realized_pnl_usd += realized_pnl_native

        summary_list.append(data)

    total_portfolio_value_jpy = total_portfolio_value_usd * exchange_rate if exchange_rate > 0 else 0
    total_realized_pnl_jpy = total_realized_pnl_usd * exchange_rate if exchange_rate > 0 else 0
    total_unrealized_pnl_jpy = total_unrealized_pnl_usd * exchange_rate if exchange_rate > 0 else 0

    return {
        'stocks': summary_list,
        'total_value_usd': total_portfolio_value_usd,
        'total_value_jpy': total_portfolio_value_jpy,
        'total_realized_pnl_usd': total_realized_pnl_usd,
        'total_realized_pnl_jpy': total_realized_pnl_jpy,
        'total_unrealized_pnl_usd': total_unrealized_pnl_usd,
        'total_unrealized_pnl_jpy': total_unrealized_pnl_jpy,
    }

def update_portfolio_history():
    """
    Calculates and stores a daily snapshot of the portfolio's total value.
    If there are missing days since the last snapshot, it backfills them by carrying forward the last known value.
    """
    today_str = datetime.now().strftime('%Y-%m-%d')
    
    with sqlite3.connect(DATABASE) as conn:
        conn.row_factory = sqlite3.Row
        # Check if today's snapshot already exists
        if conn.execute("SELECT 1 FROM portfolio_history WHERE date = ?", (today_str,)).fetchone():
            return # Already recorded today

        # Get the most recent snapshot to backfill from
        last_snapshot = conn.execute("SELECT date, value_usd, value_jpy FROM portfolio_history ORDER BY date DESC LIMIT 1").fetchone()

        if last_snapshot:
            last_date = datetime.strptime(last_snapshot['date'], '%Y-%m-%d').date()
            days_to_fill = (datetime.now().date() - last_date).days
            if days_to_fill > 1:
                print(f"Backfilling {days_to_fill - 1} missing day(s) in portfolio history...")
                for i in range(1, days_to_fill):
                    missing_date_str = (last_date + timedelta(days=i)).strftime('%Y-%m-%d')
                    conn.execute(
                        "INSERT OR IGNORE INTO portfolio_history (date, value_usd, value_jpy) VALUES (?, ?, ?)",
                        (missing_date_str, last_snapshot['value_usd'], last_snapshot['value_jpy'])
                    )

        # Calculate and insert today's value
        print(f"Generating portfolio history snapshot for {today_str}...")
        trades = conn.execute('SELECT * FROM trades ORDER BY trade_date ASC').fetchall()
    
    exchange_rate = get_exchange_rate()
    summary = _calculate_portfolio_summary(trades, exchange_rate)
    
    with sqlite3.connect(DATABASE) as conn:
        conn.execute(
            "INSERT INTO portfolio_history (date, value_usd, value_jpy) VALUES (?, ?, ?)",
            (today_str, summary['total_value_usd'], summary['total_value_jpy'])
        )
        print(f"Saved portfolio snapshot for {today_str}.")

@app.route('/')
def index():
    """Calculates and displays a summary of current holdings from trades."""
    update_portfolio_history() # Add daily snapshot if needed

    exchange_rate = get_exchange_rate()

    with sqlite3.connect(DATABASE) as conn:
        conn.row_factory = sqlite3.Row
        trades = conn.execute('SELECT * FROM trades ORDER BY trade_date ASC').fetchall()
        history_rows = conn.execute("SELECT date, value_jpy FROM portfolio_history ORDER BY date ASC LIMIT 365").fetchall()
        history_data = [dict(row) for row in history_rows]

    summary = _calculate_portfolio_summary(trades, exchange_rate)
    prices_last_updated = datetime.now().strftime('%Y-%m-%d %H:%M')

    return render_template('index.html', **summary, exchange_rate=exchange_rate, prices_last_updated=prices_last_updated, history_data=history_data, brokers=BROKERS)

def generate_tax_report_data(year):
    """
    Generates a tax report for a given year using the moving-average cost basis method.
    All calculations are performed in JPY.
    """
    with sqlite3.connect(DATABASE) as conn:
        conn.row_factory = sqlite3.Row
        # Fetch all trades to build history correctly
        trades = conn.execute('SELECT * FROM trades ORDER BY trade_date ASC').fetchall()

    holdings = {}  # Tracks the moving-average cost for each stock
    buy_history = {} # Tracks all buy transactions for the breakdown
    sales_report = []

    for trade in trades:
        symbol = trade['symbol']
        trade_year = int(trade['trade_date'][:4])

        if symbol not in holdings:
            holdings[symbol] = {
                'quantity': 0, 
                'total_cost_jpy': 0,
                'total_cost_native': 0,
                'last_purchase_date': None
            }
            buy_history[symbol] = []

        # --- Cost Calculation (for BUYs) ---
        if trade['trade_type'] == 'BUY':
            cost_jpy = 0
            fee_jpy = 0
            cost_native = 0
            
            # Convert fee to JPY if necessary, using the trade's specific FX rate
            if trade['fee_amount']:
                if trade['fee_currency'] == 'JPY':
                    fee_jpy = trade['fee_amount']
                elif trade['fee_currency'] == 'USD' and trade['fx_rate']:
                    fee_jpy = trade['fee_amount'] * trade['fx_rate']

            # Fee in native currency
            fee_native = 0
            if trade['fee_amount']:
                if trade['fee_currency'] == trade['currency']:
                    fee_native = trade['fee_amount']
                elif trade['fee_currency'] == 'JPY' and trade['currency'] == 'USD' and trade['fx_rate']:
                    fee_native = trade['fee_amount'] / trade['fx_rate']
                elif trade['fee_currency'] == 'USD' and trade['currency'] == 'JPY' and trade['fx_rate']:
                    fee_native = trade['fee_amount'] * trade['fx_rate']

            # Calculate cost of the buy transaction in JPY
            if trade['currency'] == 'JPY':
                cost_jpy = (trade['quantity'] * trade['price']) + fee_jpy
            elif trade['currency'] == 'USD' and trade['fx_rate']:
                cost_jpy = (trade['quantity'] * trade['price'] * trade['fx_rate']) + fee_jpy

            # Calculate cost of the buy transaction in native currency
            cost_native = (trade['quantity'] * trade['price']) + fee_native

            holdings[symbol]['quantity'] += trade['quantity']
            holdings[symbol]['total_cost_jpy'] += cost_jpy
            holdings[symbol]['total_cost_native'] += cost_native
            holdings[symbol]['last_purchase_date'] = trade['trade_date']

            buy_history[symbol].append({
                'date': trade['trade_date'],
                'quantity': trade['quantity'],
                'price_native': trade['price'],
                'currency': trade['currency'],
                'fx_rate': trade['fx_rate'],
                'fee_jpy': fee_jpy,
                'total_cost_jpy': cost_jpy
            })

        # --- P&L Calculation (for SELLs in the target year) ---
        elif trade['trade_type'] == 'SELL' and trade_year == year:
            current_holding = holdings[symbol]
            avg_cost_jpy = 0
            if current_holding['quantity'] > 0:
                avg_cost_jpy = current_holding['total_cost_jpy'] / current_holding['quantity']
            
            avg_cost_native = 0
            if current_holding['quantity'] > 0:
                avg_cost_native = current_holding['total_cost_native'] / current_holding['quantity']

            cost_of_sale_jpy = trade['quantity'] * avg_cost_jpy

            # Calculate proceeds from the sale in JPY
            proceeds_jpy = 0
            fee_jpy = 0
            if trade['fee_amount']:
                if trade['fee_currency'] == 'JPY':
                    fee_jpy = trade['fee_amount']
                elif trade['fee_currency'] == 'USD' and trade['fx_rate']:
                    fee_jpy = trade['fee_amount'] * trade['fx_rate']
            
            if trade['currency'] == 'JPY':
                proceeds_jpy = (trade['quantity'] * trade['price']) - fee_jpy
            elif trade['currency'] == 'USD' and trade['fx_rate']:
                proceeds_jpy = (trade['quantity'] * trade['price'] * trade['fx_rate']) - fee_jpy

            pnl_jpy = proceeds_jpy - cost_of_sale_jpy

            sales_report.append({
                'symbol': symbol, 'name': trade['name'], 
                'trade_date': trade['trade_date'], 'quantity': trade['quantity'], 
                'proceeds_jpy': proceeds_jpy, 'cost_basis_jpy': cost_of_sale_jpy, 
                'pnl_jpy': pnl_jpy, 'broker': trade['broker'],
                'selling_fee_jpy': fee_jpy,
                'last_purchase_date': holdings[symbol]['last_purchase_date'],
                # --- Additions for breakdown ---
                'avg_cost_per_share_jpy': avg_cost_jpy,
                'avg_cost_per_share_native': avg_cost_native,
                'sale_price_native': trade['price'],
                'sale_currency': trade['currency'],
                'sale_fx_rate': trade['fx_rate'],
                'acquisition_history': list(buy_history[symbol])
            })

            # Update holdings after the sale
            holdings[symbol]['quantity'] -= trade['quantity']
            holdings[symbol]['total_cost_jpy'] -= cost_of_sale_jpy
            cost_of_sale_native = trade['quantity'] * avg_cost_native
            holdings[symbol]['total_cost_native'] -= cost_of_sale_native

    return {
        'sales': sales_report,
        'total_proceeds_jpy': sum(s['proceeds_jpy'] for s in sales_report),
        'total_cost_basis_jpy': sum(s['cost_basis_jpy'] for s in sales_report),
        'total_pnl_jpy': sum(s['pnl_jpy'] for s in sales_report),
        'year': year
    }

@app.route('/trades')
def list_trades():
    """Displays a list of all trades."""
    with sqlite3.connect(DATABASE) as conn:
        conn.row_factory = sqlite3.Row
        trades = conn.execute('SELECT * FROM trades ORDER BY trade_date DESC').fetchall()
    return render_template('trades.html', trades=trades)

@app.route('/tax_report', methods=['GET', 'POST'])
def tax_report():
    """Handles the tax report generation."""
    with sqlite3.connect(DATABASE) as conn:
        # Get distinct years from trades to populate the dropdown
        years_cursor = conn.execute("SELECT DISTINCT SUBSTR(trade_date, 1, 4) as year FROM trades ORDER BY year DESC")
        available_years = [row[0] for row in years_cursor]

    report_data = None
    if request.method == 'POST':
        selected_year = request.form.get('year')
        if selected_year:
            report_data = generate_tax_report_data(int(selected_year))
    
    return render_template('tax_report.html', years=available_years, report_data=report_data)


@app.route('/add_trade', methods=['GET', 'POST'])
def add_trade():
    """Handles adding a new trade."""
    if request.method == 'POST':
        with sqlite3.connect(DATABASE) as conn:
            conn.execute(
                'INSERT INTO trades (symbol, name, trade_type, quantity, price, currency, trade_date, broker, fx_rate, fee_amount, fee_currency) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (
                    request.form['symbol'],
                    request.form['name'],
                    request.form['trade_type'],
                    float(request.form['quantity']),
                    float(request.form['price']),
                    request.form['currency'],
                    request.form['trade_date'],
                    request.form['broker'],
                    float(request.form.get('fx_rate')) if request.form.get('fx_rate') else None,
                    float(request.form.get('fee_amount')) if request.form.get('fee_amount') else None,
                    request.form.get('fee_currency') or None
                )
            )
        return redirect(url_for('list_trades'))
    return render_template('add_trade.html', today=datetime.utcnow().strftime('%Y-%m-%d'), brokers=BROKERS)

@app.route('/edit_trade/<int:trade_id>', methods=['GET', 'POST'])
def edit_trade(trade_id):
    """Handles editing an existing trade."""
    if request.method == 'POST':
        with sqlite3.connect(DATABASE) as conn:
            conn.execute(
                'UPDATE trades SET symbol=?, name=?, trade_type=?, quantity=?, price=?, currency=?, trade_date=?, broker=?, fx_rate=?, fee_amount=?, fee_currency=? WHERE id=?',
                (
                    request.form['symbol'],
                    request.form['name'],
                    request.form['trade_type'],
                    float(request.form['quantity']),
                    float(request.form['price']),
                    request.form['currency'],
                    request.form['trade_date'],
                    request.form['broker'],
                    float(request.form.get('fx_rate')) if request.form.get('fx_rate') else None,
                    float(request.form.get('fee_amount')) if request.form.get('fee_amount') else None,
                    request.form.get('fee_currency') or None,
                    trade_id
                )
            )
        return redirect(url_for('list_trades'))

    # GET request: fetch trade and show edit form
    with sqlite3.connect(DATABASE) as conn:
        conn.row_factory = sqlite3.Row
        trade = conn.execute('SELECT * FROM trades WHERE id = ?', (trade_id,)).fetchone()
    return render_template('edit_trade.html', trade=trade, brokers=BROKERS)

@app.route('/delete_trade/<int:trade_id>')
def delete_trade(trade_id):
    """Deletes a trade from the database."""
    with sqlite3.connect(DATABASE) as conn:
        conn.execute('DELETE FROM trades WHERE id = ?', (trade_id,))
    return redirect(url_for('list_trades'))

@app.route('/export_trades')
def export_trades():
    """Exports all trades to a CSV file in the same format as the bulk uploader."""
    try:
        with sqlite3.connect(DATABASE) as conn:
            conn.row_factory = sqlite3.Row
            trades = conn.execute('SELECT * FROM trades ORDER BY trade_date ASC').fetchall()

        # Use an in-memory string buffer to build the CSV
        output = io.StringIO()
        fieldnames = ['symbol', 'name', 'trade_type', 'quantity', 'price', 'currency', 'trade_date', 'broker', 'fx_rate', 'fee_amount', 'fee_currency']
        writer = csv.DictWriter(output, fieldnames=fieldnames)

        writer.writeheader()
        for trade in trades:
            # sqlite3.Row can be converted to a dict for the writer
            writer.writerow(dict(trade))

        return Response(
            output.getvalue(),
            mimetype="text/csv",
            headers={"Content-disposition": "attachment; filename=trades_export.csv"}
        )

    except Exception as e:
        flash(f'An error occurred during export: {e}', 'danger')
        return redirect(url_for('list_trades'))

@app.route('/bulk_upload', methods=['GET', 'POST'])
def bulk_upload():
    if request.method == 'POST':
        if 'file' not in request.files:
            flash('No file part in the request.', 'danger')
            return redirect(request.url)
        file = request.files['file']
        if file.filename == '':
            flash('No file selected for uploading.', 'danger')
            return redirect(request.url)
        if file and file.filename.endswith('.csv'):
            try:
                # Read the file in memory to avoid saving it to disk
                stream = io.StringIO(file.stream.read().decode("UTF8"), newline=None)
                csv_reader = csv.DictReader(stream)
                
                trades_to_add = []
                errors = []
                required_columns = ['symbol', 'name', 'trade_type', 'quantity', 'price', 'currency', 'trade_date', 'broker']

                for i, row in enumerate(csv_reader):
                    row_num = i + 2  # Account for header row

                    # Check for missing required columns
                    missing_cols = [col for col in required_columns if col not in row or not row[col]]
                    if missing_cols:
                        errors.append(f"Row {row_num}: Missing required data for column(s): {', '.join(missing_cols)}")
                        continue

                    try:
                        trade_type = row['trade_type'].upper()
                        if trade_type not in ['BUY', 'SELL']:
                            errors.append(f"Row {row_num}: Invalid trade_type '{row['trade_type']}'. Must be 'BUY' or 'SELL'.")
                            continue

                        currency = row['currency'].upper()
                        if currency not in ['USD', 'JPY']:
                            errors.append(f"Row {row_num}: Invalid currency '{row['currency']}'. Must be 'USD' or 'JPY'.")
                            continue
                        
                        quantity = float(row['quantity'])
                        price = float(row['price'])
                        if quantity <= 0 or price < 0:
                             errors.append(f"Row {row_num}: Quantity must be positive and price cannot be negative.")
                             continue

                        # Safely process optional values
                        fx_rate_str = row.get('fx_rate', '').strip()
                        fee_amount_str = row.get('fee_amount', '').strip()
                        fee_currency_str = row.get('fee_currency', '').strip()

                        trades_to_add.append({
                            'symbol': row['symbol'], 'name': row['name'], 'trade_type': trade_type,
                            'quantity': quantity, 'price': price, 'currency': currency, 
                            'trade_date': row['trade_date'], 'broker': row['broker'],
                            'fx_rate': float(fx_rate_str) if fx_rate_str else None,
                            'fee_amount': float(fee_amount_str) if fee_amount_str else None,
                            'fee_currency': fee_currency_str.upper() if fee_currency_str else None,
                        })
                    except (ValueError, TypeError) as ve:
                        errors.append(f"Row {row_num}: Invalid number format. Please check quantity, price, and other numeric fields. Error: {ve}")

                if errors:
                    for error in errors:
                        flash(error, 'danger')
                    return redirect(request.url)

                # If no errors, proceed with DB insertion
                if trades_to_add:
                    with sqlite3.connect(DATABASE) as conn:
                        for trade in trades_to_add:
                            conn.execute(
                                'INSERT INTO trades (symbol, name, trade_type, quantity, price, currency, trade_date, broker, fx_rate, fee_amount, fee_currency) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                                list(trade.values())
                            )
                    flash(f'Successfully uploaded and inserted {len(trades_to_add)} trades!', 'success')
                    return redirect(url_for('list_trades'))

            except Exception as e:
                flash(f'An error occurred while processing the file: {e}', 'danger')
                return redirect(request.url)
        else:
            flash('Invalid file type. Please upload a CSV file.', 'warning')
            return redirect(request.url)

    return render_template('bulk_upload.html')

# Initialize database on startup.
# This ensures the necessary tables exist before the app starts.
init_db()

if __name__ == '__main__':
    # This block is for local development only. It runs the Flask development server.
    # In production (e.g., via Docker), a WSGI server like Gunicorn is used to run the app,
    # and this block is not executed.
    # The debug flag is set to True for development, which provides an interactive debugger.
    # The host '0.0.0.0' makes the server accessible from outside a container.
    app.run(host='0.0.0.0', port=5001, debug=True)