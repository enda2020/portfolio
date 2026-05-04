from flask import Flask, render_template, request, redirect, url_for, jsonify, flash, Response, abort, session
import sqlite3
import yfinance as yf
from datetime import datetime, timedelta
import csv
import io
import os
import secrets
from flask_caching import Cache
from dotenv import load_dotenv

# Load environment variables from .env file, making them available to os.environ
load_dotenv()

app = Flask(__name__)
# Load the secret key from an environment variable for production.
# The default value is only for local development and should not be used in production.
app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'a_super_secret_key_for_flash_messages')

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
HEALTH_SETTING_DEFAULTS = {
    'single_stock_warning_percent': 25.0,
    'single_stock_danger_percent': 35.0,
    'single_stock_target_percent': 25.0,
    'sector_warning_percent': 35.0,
    'sector_danger_percent': 50.0,
    'min_holdings_count': 5,
    'max_rebalance_ideas': 6,
}
HEALTH_SETTING_LABELS = {
    'single_stock_warning_percent': 'Single Stock Warning (%)',
    'single_stock_danger_percent': 'Single Stock Danger (%)',
    'single_stock_target_percent': 'Single Stock Target (%)',
    'sector_warning_percent': 'Sector Warning (%)',
    'sector_danger_percent': 'Sector Danger (%)',
    'min_holdings_count': 'Minimum Holdings Count',
    'max_rebalance_ideas': 'Maximum Rebalance Ideas',
}

@cache.memoize()
def get_exchange_rate():
    """Fetches the current USD/JPY exchange rate."""
    print("--- CACHE MISS: Fetching live USD/JPY exchange rate from yfinance ---")
    try:
        # Use a longer period to be robust against weekends/holidays
        history = yf.Ticker("JPY=X").history(period="5d")
        print("--- yfinance response for JPY=X ---")
        print(history)
        if not history.empty and ('Close' in history.columns or 'close' in history.columns):
            # The column name can be 'Close' or 'close'. For the most recent entry,
            # this value represents the "last price", not necessarily a closing price.
            price_col = 'Close' if 'Close' in history.columns else 'close'
            last_price = float(history[price_col].iloc[-1])
            latest_data_at, _ = _format_market_timestamp(history.index[-1])
            print(f"--- Using last available price for JPY=X from {latest_data_at}: {last_price} ---")
            return {
                'rate': last_price,
                'latest_data_at': latest_data_at
            }
    except Exception as e:
        print(f"Could not fetch exchange rate: {e}.")
    return None

@cache.memoize()
def get_stock_price(symbol, currency):
    """Fetches the current price, today's change, and recent history of a stock symbol."""
    api_symbol = symbol
    # Yahoo Finance uses a ".T" suffix for stocks on the Tokyo Stock Exchange
    if currency == 'JPY':
        api_symbol += '.T'
    
    print(f"--- CACHE MISS: Fetching live market data for {symbol} (API symbol: {api_symbol}) from yfinance ---")

    result = {
        'current_price': 0.0,
        'change_today': 0.0,
        'sparkline_data': [],
        'is_valid': False,
        'latest_data_at': None,
        'latest_data_sort': None,
        'quote_session': 'regular',
        'includes_extended_hours': False
    }

    try:
        # Fetch 15 days of data to get 14 days for sparkline and one previous day for change
        history = yf.Ticker(api_symbol).history(period="15d")
        print(f"--- yfinance response for {api_symbol} ---")
        print(history)
        if not history.empty and ('Close' in history.columns or 'close' in history.columns):
            # The column name can be 'Close' or 'close'. For the most recent entry,
            # this value represents the "last price", not necessarily a closing price.
            price_col = 'Close' if 'Close' in history.columns else 'close'

            result['current_price'] = float(history[price_col].iloc[-1])
            result['is_valid'] = True
            result['latest_data_at'], result['latest_data_sort'] = _format_market_timestamp(history.index[-1])
            print(f"--- Using last available price for {symbol} from {result['latest_data_at']}: {result['current_price']} ---")
            
            # Calculate today's change if there's at least one previous day
            if len(history[price_col]) > 1:
                result['change_today'] = float(history[price_col].iloc[-1] - history[price_col].iloc[-2])
            
            # Get the last 14 days for the sparkline
            result['sparkline_data'] = list(history[price_col].tail(14))

            if currency == 'USD':
                intraday_history = yf.Ticker(api_symbol).history(period="5d", interval="5m", prepost=True)
                print(f"--- yfinance extended-hours response for {api_symbol} ---")
                print(intraday_history)
                if not intraday_history.empty and ('Close' in intraday_history.columns or 'close' in intraday_history.columns):
                    intraday_price_col = 'Close' if 'Close' in intraday_history.columns else 'close'
                    result['current_price'] = float(intraday_history[intraday_price_col].iloc[-1])
                    result['latest_data_at'], result['latest_data_sort'] = _format_market_timestamp(intraday_history.index[-1])
                    result['includes_extended_hours'] = True
                    result['quote_session'] = _classify_us_market_session(intraday_history.index[-1])
                    print(f"--- Using latest US quote for {symbol} from {result['latest_data_at']} ({result['quote_session']}): {result['current_price']} ---")

    except Exception as e:
        print(f"Could not fetch price for {symbol}: {e}")
    
    return result

@cache.memoize(timeout=86400)
def get_stock_profile(symbol, currency):
    """Fetches slower-changing stock metadata used by portfolio health checks."""
    api_symbol = symbol
    if currency == 'JPY':
        api_symbol += '.T'

    result = {
        'sector': 'Unclassified',
        'industry': 'Unclassified',
        'quote_type': None,
        'is_valid': False
    }

    try:
        info = yf.Ticker(api_symbol).get_info()
        quote_type = info.get('quoteType')
        sector = info.get('sector')
        industry = info.get('industry')

        if not sector and quote_type in ['ETF', 'MUTUALFUND']:
            sector = 'Fund / ETF'
        if not industry and quote_type in ['ETF', 'MUTUALFUND']:
            industry = 'Diversified Fund'

        result.update({
            'sector': sector or 'Unclassified',
            'industry': industry or 'Unclassified',
            'quote_type': quote_type,
            'is_valid': bool(sector or industry or quote_type)
        })
    except Exception as e:
        print(f"Could not fetch profile for {symbol}: {e}")

    return result

def _percent(value, total):
    return (value / total) * 100 if total else 0

def _format_market_timestamp(timestamp):
    """Returns display and sortable forms for the latest timestamp from yfinance."""
    if timestamp is None:
        return None, None

    try:
        if hasattr(timestamp, 'to_pydatetime'):
            dt = timestamp.to_pydatetime()
        elif isinstance(timestamp, datetime):
            dt = timestamp
        else:
            return str(timestamp), str(timestamp)

        if dt.hour == 0 and dt.minute == 0 and dt.second == 0:
            display = dt.strftime('%Y-%m-%d')
        else:
            display = dt.strftime('%Y-%m-%d %H:%M %Z').strip()
        return display, dt.timestamp()
    except Exception:
        return str(timestamp), str(timestamp)

def _classify_us_market_session(timestamp):
    """Classifies a yfinance intraday timestamp as regular, pre-market, or post-market."""
    try:
        if hasattr(timestamp, 'to_pydatetime'):
            dt = timestamp.to_pydatetime()
        elif isinstance(timestamp, datetime):
            dt = timestamp
        else:
            return 'extended hours'

        market_minutes = (dt.hour * 60) + dt.minute
        regular_start = (9 * 60) + 30
        regular_end = 16 * 60

        if market_minutes < regular_start:
            return 'pre-market'
        if market_minutes >= regular_end:
            return 'post-market'
        return 'regular'
    except Exception:
        return 'extended hours'

def _calculate_portfolio_health(summary, settings=None):
    """Creates concentration checks and rebalance ideas from the current holdings summary."""
    settings = settings or HEALTH_SETTING_DEFAULTS.copy()
    stocks = summary['stocks']
    total_value = summary['total_value_jpy']
    checks = []
    ideas = []
    sector_map = {}
    score = 100

    for stock in stocks:
        profile = get_stock_profile(stock['symbol'], stock['currency'])
        stock['sector'] = profile['sector']
        stock['industry'] = profile['industry']
        stock['weight_percent'] = _percent(stock['current_value_jpy'], total_value)

        sector = stock['sector']
        if sector not in sector_map:
            sector_map[sector] = {
                'sector': sector,
                'value_jpy': 0,
                'weight_percent': 0,
                'holdings': []
            }
        sector_map[sector]['value_jpy'] += stock['current_value_jpy']
        sector_map[sector]['holdings'].append(stock['symbol'])

    sectors = list(sector_map.values())
    for sector in sectors:
        sector['weight_percent'] = _percent(sector['value_jpy'], total_value)
        sector['holdings'] = sorted(set(sector['holdings']))

    top_stock = max(stocks, key=lambda s: s['weight_percent'], default=None)
    top_sector = max(sectors, key=lambda s: s['weight_percent'], default=None)

    if not stocks:
        return {
            'score': 0,
            'score_label': 'No holdings',
            'checks': [{
                'severity': 'info',
                'title': 'No open holdings',
                'detail': 'Add trades before running portfolio health checks.'
            }],
            'ideas': [],
            'sectors': [],
            'stocks': [],
            'settings': settings,
        }

    for stock in stocks:
        if stock['weight_percent'] >= settings['single_stock_danger_percent']:
            score -= 25
            checks.append({
                'severity': 'danger',
                'title': f"{stock['symbol']} is very concentrated",
                'detail': f"{stock['symbol']} is {stock['weight_percent']:.1f}% of the portfolio."
            })
            target_percent = settings['single_stock_target_percent']
            target_value = total_value * (target_percent / 100)
            excess_value = max(0, stock['current_value_jpy'] - target_value)
            ideas.append({
                'title': f"Bring {stock['symbol']} closer to {target_percent:g}%",
                'detail': f"To reach {target_percent:g}%, reduce or offset about ¥{excess_value:,.0f} of exposure with future buys or trims."
            })
        elif stock['weight_percent'] >= settings['single_stock_warning_percent']:
            score -= 12
            checks.append({
                'severity': 'warning',
                'title': f"{stock['symbol']} is above the single-stock watch level",
                'detail': f"{stock['symbol']} is {stock['weight_percent']:.1f}% of the portfolio."
            })
            ideas.append({
                'title': f"Pause new buys into {stock['symbol']}",
                'detail': "Direct new contributions toward lower-weight holdings until this position drops below 25%."
            })

    for sector in sectors:
        if sector['weight_percent'] >= settings['sector_danger_percent']:
            score -= 20
            checks.append({
                'severity': 'danger',
                'title': f"{sector['sector']} sector is very overweight",
                'detail': f"{sector['sector']} is {sector['weight_percent']:.1f}% across {', '.join(sector['holdings'])}."
            })
            ideas.append({
                'title': f"Reduce dependence on {sector['sector']}",
                'detail': "Consider adding to sectors with little or no exposure before adding more here."
            })
        elif sector['weight_percent'] >= settings['sector_warning_percent']:
            score -= 10
            checks.append({
                'severity': 'warning',
                'title': f"{sector['sector']} sector is above the watch level",
                'detail': f"{sector['sector']} is {sector['weight_percent']:.1f}% across {', '.join(sector['holdings'])}."
            })

    unclassified = [stock['symbol'] for stock in stocks if stock['sector'] == 'Unclassified']
    if unclassified:
        score -= min(10, len(unclassified) * 2)
        checks.append({
            'severity': 'info',
            'title': 'Some holdings could not be classified',
            'detail': f"Sector data is missing for {', '.join(unclassified)}."
        })

    if len(stocks) < settings['min_holdings_count']:
        score -= 10
        checks.append({
            'severity': 'warning',
            'title': 'Limited number of holdings',
            'detail': f"The portfolio has {len(stocks)} open holding{'s' if len(stocks) != 1 else ''}."
        })
        ideas.append({
            'title': 'Add diversification with new contributions',
            'detail': 'New buys can target assets or sectors not already represented in the portfolio.'
        })

    if not checks:
        checks.append({
            'severity': 'success',
            'title': 'No major concentration issues found',
            'detail': 'Single-stock and sector weights are within the current watch levels.'
        })

    score = max(0, min(100, round(score)))
    if score >= 85:
        score_label = 'Healthy'
    elif score >= 70:
        score_label = 'Watch'
    elif score >= 50:
        score_label = 'Needs attention'
    else:
        score_label = 'High concentration risk'

    return {
        'score': score,
        'score_label': score_label,
        'checks': checks,
        'ideas': ideas[:int(settings['max_rebalance_ideas'])],
        'sectors': sorted(sectors, key=lambda s: s['weight_percent'], reverse=True),
        'stocks': sorted(stocks, key=lambda s: s['weight_percent'], reverse=True),
        'top_stock': top_stock,
        'top_sector': top_sector,
        'settings': settings,
    }

@app.context_processor
def inject_csrf_token():
    """Makes a per-session CSRF token available to all templates."""
    if 'csrf_token' not in session:
        session['csrf_token'] = secrets.token_urlsafe(32)
    return {'csrf_token': session['csrf_token']}

def _validate_csrf_token():
    token = session.get('csrf_token')
    if not token or not secrets.compare_digest(token, request.form.get('csrf_token', '')):
        abort(400)

def _parse_optional_float(value):
    value = (value or '').strip()
    return float(value) if value else None

def _parse_trade_form(form):
    """Validates trade form input and returns normalized values plus errors."""
    errors = []
    values = {
        'symbol': form.get('symbol', '').strip().upper(),
        'name': form.get('name', '').strip(),
        'trade_type': form.get('trade_type', '').strip().upper(),
        'quantity': None,
        'price': None,
        'currency': form.get('currency', '').strip().upper(),
        'trade_date': form.get('trade_date', '').strip(),
        'broker': form.get('broker', '').strip(),
        'fx_rate': None,
        'fee_amount': None,
        'fee_currency': form.get('fee_currency', '').strip().upper() or None
    }

    required_fields = ['symbol', 'name', 'trade_type', 'currency', 'trade_date', 'broker']
    for field in required_fields:
        if not values[field]:
            errors.append(f"{field.replace('_', ' ').title()} is required.")

    if values['trade_type'] and values['trade_type'] not in ['BUY', 'SELL']:
        errors.append("Trade type must be BUY or SELL.")
    if values['currency'] and values['currency'] not in ['USD', 'JPY']:
        errors.append("Currency must be USD or JPY.")
    if values['broker'] and values['broker'] not in BROKERS:
        errors.append("Broker is not recognized.")
    if values['fee_currency'] and values['fee_currency'] not in ['USD', 'JPY']:
        errors.append("Fee currency must be USD or JPY.")

    try:
        values['quantity'] = float(form.get('quantity', ''))
        if values['quantity'] <= 0:
            errors.append("Quantity must be positive.")
    except (TypeError, ValueError):
        errors.append("Quantity must be a valid number.")

    try:
        values['price'] = float(form.get('price', ''))
        if values['price'] < 0:
            errors.append("Price cannot be negative.")
    except (TypeError, ValueError):
        errors.append("Price must be a valid number.")

    try:
        values['fx_rate'] = _parse_optional_float(form.get('fx_rate'))
        if values['fx_rate'] is not None and values['fx_rate'] <= 0:
            errors.append("FX rate must be positive.")
    except ValueError:
        errors.append("FX rate must be a valid number.")

    try:
        values['fee_amount'] = _parse_optional_float(form.get('fee_amount'))
        if values['fee_amount'] is not None and values['fee_amount'] < 0:
            errors.append("Broker fee cannot be negative.")
    except ValueError:
        errors.append("Broker fee must be a valid number.")

    try:
        datetime.strptime(values['trade_date'], '%Y-%m-%d')
    except ValueError:
        errors.append("Trade date must be in YYYY-MM-DD format.")

    return values, errors

def get_health_settings():
    """Loads health-check thresholds from the database, seeding defaults if needed."""
    with sqlite3.connect(DATABASE) as conn:
        conn.row_factory = sqlite3.Row
        conn.executemany(
            "INSERT OR IGNORE INTO health_settings (key, value) VALUES (?, ?)",
            [(key, str(value)) for key, value in HEALTH_SETTING_DEFAULTS.items()]
        )
        rows = conn.execute("SELECT key, value FROM health_settings").fetchall()

    settings = HEALTH_SETTING_DEFAULTS.copy()
    for row in rows:
        key = row['key']
        if key not in settings:
            continue
        if key in ['min_holdings_count', 'max_rebalance_ideas']:
            settings[key] = int(float(row['value']))
        else:
            settings[key] = float(row['value'])
    return settings

def _parse_health_settings_form(form):
    settings = {}
    errors = []
    integer_keys = ['min_holdings_count', 'max_rebalance_ideas']

    for key, default in HEALTH_SETTING_DEFAULTS.items():
        raw_value = (form.get(key, '') or '').strip()
        label = HEALTH_SETTING_LABELS[key]
        try:
            if key in integer_keys:
                value = int(raw_value)
            else:
                value = float(raw_value)
        except ValueError:
            errors.append(f"{label} must be a valid number.")
            settings[key] = default
            continue

        if key.endswith('_percent') and not 0 <= value <= 100:
            errors.append(f"{label} must be between 0 and 100.")
        if key == 'min_holdings_count' and value < 1:
            errors.append("Minimum Holdings Count must be at least 1.")
        if key == 'max_rebalance_ideas' and not 1 <= value <= 20:
            errors.append("Maximum Rebalance Ideas must be between 1 and 20.")
        settings[key] = value

    if settings['single_stock_warning_percent'] > settings['single_stock_danger_percent']:
        errors.append("Single Stock Warning must be less than or equal to Single Stock Danger.")
    if settings['sector_warning_percent'] > settings['sector_danger_percent']:
        errors.append("Sector Warning must be less than or equal to Sector Danger.")
    if settings['single_stock_target_percent'] > settings['single_stock_warning_percent']:
        errors.append("Single Stock Target should be less than or equal to Single Stock Warning.")

    return settings, errors

def save_health_settings(settings):
    with sqlite3.connect(DATABASE) as conn:
        conn.executemany(
            """
            INSERT INTO health_settings (key, value)
            VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            [(key, str(value)) for key, value in settings.items()]
        )

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
        conn.execute('''
            CREATE TABLE IF NOT EXISTS health_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        ''')
        conn.executemany(
            "INSERT OR IGNORE INTO health_settings (key, value) VALUES (?, ?)",
            [(key, str(value)) for key, value in HEALTH_SETTING_DEFAULTS.items()]
        )
    print("Database tables ensured to exist.")

def _calculate_portfolio_summary(trades, exchange_rate, broker_filter=None, currency_filter=None):
    """
    Helper function to perform the main portfolio calculation.
    This is refactored out of the index() route for reuse.
    """
    # --- Aggregation Logic ---
    # This part always runs on all trades to correctly calculate
    # cost basis and realized P&L across the entire history, regardless of filters.
    # Filters are applied later before calculating summary values.
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
        elif trade['trade_type'] == 'SELL':
            avg_cost_basis = 0
            if holdings[key]['quantity'] > 0:
                avg_cost_basis = holdings[key]['total_cost'] / holdings[key]['quantity']
            
            cost_of_shares_sold = trade['quantity'] * avg_cost_basis
            proceeds = (trade['quantity'] * trade['price']) - fee_in_native_currency
            
            holdings[key]['realized_pnl_native'] += proceeds - cost_of_shares_sold
            holdings[key]['quantity'] -= trade['quantity']
            holdings[key]['total_cost'] -= cost_of_shares_sold

    # --- Combine display rows ---
    # Keep the broker-level accounting above, then combine open holdings for display.
    # This avoids fetching and showing duplicate rows for the same ticker while preserving
    # each broker's own sale history and cost basis.
    combined_holdings = {}
    summary_list = []
    total_realized_pnl_usd = 0.0

    for key, data in holdings.items():
        # Apply filters before calculating summary totals
        if broker_filter and data['broker'] != broker_filter:
            continue
        if currency_filter and data['currency'] != currency_filter:
            continue

        realized_pnl_native = data['realized_pnl_native']
        if data['currency'] == 'JPY' and exchange_rate > 0:
            total_realized_pnl_usd += realized_pnl_native / exchange_rate
        else:
            total_realized_pnl_usd += realized_pnl_native

        if data['quantity'] <= 0.00001: # Use a small epsilon for float comparison
            continue # Skip display and market-data lookup for fully sold-off stocks

        combined_key = (data['symbol'], data['currency'])
        if combined_key not in combined_holdings:
            combined_holdings[combined_key] = {
                'symbol': data['symbol'],
                'broker': data['broker'],
                'brokers': [data['broker']],
                'name': data['name'],
                'currency': data['currency'],
                'quantity': 0,
                'total_cost': 0,
            }
        elif data['broker'] not in combined_holdings[combined_key]['brokers']:
            combined_holdings[combined_key]['brokers'].append(data['broker'])

        combined_holdings[combined_key]['quantity'] += data['quantity']
        combined_holdings[combined_key]['total_cost'] += data['total_cost']

    # --- Enrichment and Summary ---
    total_portfolio_value_usd = 0.0
    total_unrealized_pnl_usd = 0.0
    total_today_pnl_usd = 0.0
    market_data_complete = True
    market_data_timestamps = []

    for data in combined_holdings.values():
        data['broker'] = ', '.join(data['brokers'])

        # Calculate average cost basis
        if data['quantity'] > 0:
            data['avg_cost_basis'] = data['total_cost'] / data['quantity']
        else:
            data['avg_cost_basis'] = 0

        # Get current market data
        market_data = get_stock_price(data['symbol'], data['currency'])
        if not market_data.get('is_valid'):
            market_data_complete = False
        data['current_price'] = market_data['current_price']
        data['change_today'] = market_data['change_today']
        data['sparkline_data'] = market_data['sparkline_data']
        data['latest_data_at'] = market_data.get('latest_data_at')
        data['quote_session'] = market_data.get('quote_session', 'regular')
        data['includes_extended_hours'] = market_data.get('includes_extended_hours', False)
        if market_data.get('latest_data_sort') is not None:
            market_data_timestamps.append({
                'display': market_data['latest_data_at'],
                'sort': market_data['latest_data_sort']
            })

        # Calculate Today's P&L for this holding
        today_pnl_native = data['quantity'] * data['change_today']
        data['today_pnl_native'] = today_pnl_native

        # Calculate % change for today
        prev_close = data['current_price'] - data['change_today']
        if prev_close > 0:
            data['change_today_percent'] = (data['change_today'] / prev_close) * 100
        else:
            data['change_today_percent'] = 0.0

        current_value_native = data['quantity'] * data['current_price']
        
        # Calculate P&L
        cost_of_holding = data['quantity'] * data['avg_cost_basis']
        data['pnl_native'] = current_value_native - cost_of_holding

        # Convert Today's P&L to USD and add to total
        today_pnl_usd = 0
        if data['currency'] == 'JPY' and exchange_rate > 0:
            today_pnl_usd = today_pnl_native / exchange_rate
        else: # USD
            today_pnl_usd = today_pnl_native
        data['today_pnl_jpy'] = today_pnl_usd * exchange_rate
        total_today_pnl_usd += today_pnl_usd

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
        
        summary_list.append(data)

    total_portfolio_value_jpy = total_portfolio_value_usd * exchange_rate if exchange_rate > 0 else 0
    total_realized_pnl_jpy = total_realized_pnl_usd * exchange_rate if exchange_rate > 0 else 0
    total_unrealized_pnl_jpy = total_unrealized_pnl_usd * exchange_rate if exchange_rate > 0 else 0
    total_today_pnl_jpy = total_today_pnl_usd * exchange_rate if exchange_rate > 0 else 0
    oldest_market_data = min(market_data_timestamps, key=lambda item: item['sort']) if market_data_timestamps else None
    latest_market_data = max(market_data_timestamps, key=lambda item: item['sort']) if market_data_timestamps else None

    return {
        'stocks': summary_list,
        'total_value_usd': total_portfolio_value_usd,
        'total_value_jpy': total_portfolio_value_jpy,
        'total_realized_pnl_usd': total_realized_pnl_usd,
        'total_realized_pnl_jpy': total_realized_pnl_jpy,
        'total_unrealized_pnl_usd': total_unrealized_pnl_usd,
        'total_unrealized_pnl_jpy': total_unrealized_pnl_jpy,
        'total_today_pnl_usd': total_today_pnl_usd,
        'total_today_pnl_jpy': total_today_pnl_jpy,
        'market_data_complete': market_data_complete,
        'oldest_market_data_at': oldest_market_data['display'] if oldest_market_data else None,
        'latest_market_data_at': latest_market_data['display'] if latest_market_data else None,
    }

def _ensure_history_updated(current_summary):
    """
    Ensures the portfolio history is up-to-date with today's snapshot.
    This is an idempotent operation that is safe to call on every request.
    It uses the pre-calculated summary to avoid redundant work.
    """
    today_str = datetime.now().strftime('%Y-%m-%d')
    today_date = datetime.now().date()

    with sqlite3.connect(DATABASE) as conn:
        conn.row_factory = sqlite3.Row
        # 1. Check if today's snapshot already exists
        # We don't return early, because we want to UPSERT to ensure the value is the most recent,
        # correcting potentially stale values from earlier in the day. The UPSERT is idempotent.

        # 2. Backfill any missing days since the last snapshot
        last_snapshot = conn.execute("SELECT date, value_usd, value_jpy FROM portfolio_history ORDER BY date DESC LIMIT 1").fetchone()
        if last_snapshot:
            last_date = datetime.strptime(last_snapshot['date'], '%Y-%m-%d').date()
            # Only backfill if the last record is from before today
            if last_date < today_date:
                days_to_fill = (today_date - last_date).days
                if days_to_fill > 1:
                    print(f"Backfilling {days_to_fill - 1} missing day(s) in portfolio history...")
                    for i in range(1, days_to_fill):
                        missing_date_str = (last_date + timedelta(days=i)).strftime('%Y-%m-%d')
                        # Use INSERT OR IGNORE to be safe against race conditions during backfill.
                        conn.execute(
                            "INSERT OR IGNORE INTO portfolio_history (date, value_usd, value_jpy) VALUES (?, ?, ?)",
                            (missing_date_str, last_snapshot['value_usd'], last_snapshot['value_jpy'])
                        )
        
        # 3. Insert or Update (Upsert) today's value using the provided summary.
        # This ensures that today's value is always the most recently calculated one,
        # correcting any previously stored incorrect (e.g., zero) values.
        print(f"Upserting portfolio history snapshot for {today_str}...")
        conn.execute(
            """
            INSERT INTO portfolio_history (date, value_usd, value_jpy)
            VALUES (?, ?, ?)
            ON CONFLICT(date) DO UPDATE SET
                value_usd = excluded.value_usd,
                value_jpy = excluded.value_jpy
            """,
            (today_str, current_summary['total_value_usd'], current_summary['total_value_jpy'])
        )
        print(f"Saved portfolio snapshot for {today_str}.")

@app.route('/')
def index():
    """Calculates and displays a summary of current holdings from trades."""
    # Handle cache clearing on force-refresh
    if request.args.get('refresh') == 'true':
        cache.clear()
        flash('Market data cache has been cleared. Prices are now live.', 'info')
        print("Cache cleared due to refresh request.")
        return redirect(url_for('index'))

    # 1. Get filters and raw data
    broker_filter = request.args.get('broker', 'all')
    currency_filter = request.args.get('currency', 'all')

    # Convert 'all' to None for the calculation function, which expects None for no filter
    effective_broker_filter = broker_filter if broker_filter != 'all' else None
    effective_currency_filter = currency_filter if currency_filter != 'all' else None

    exchange_data = get_exchange_rate()
    market_data_reliable = exchange_data is not None
    if isinstance(exchange_data, dict):
        exchange_rate = exchange_data['rate']
        fx_latest_data_at = exchange_data.get('latest_data_at')
    else:
        exchange_rate = exchange_data or 150.0
        fx_latest_data_at = None
    if not market_data_reliable:
        flash('Could not fetch the live USD/JPY rate. Showing temporary values and skipping today\'s history snapshot.', 'warning')

    with sqlite3.connect(DATABASE) as conn:
        conn.row_factory = sqlite3.Row
        trades = conn.execute('SELECT * FROM trades ORDER BY trade_date ASC').fetchall()

    # 2. Perform the main calculation with filters for display. This is the single source of truth.
    summary = _calculate_portfolio_summary(trades, exchange_rate, effective_broker_filter, effective_currency_filter)

    # 3. Ensure history is up-to-date with the TOTAL portfolio value.
    # If filters are active, we must re-calculate the summary without them for the history.
    if effective_broker_filter or effective_currency_filter:
        total_summary = _calculate_portfolio_summary(trades, exchange_rate)
        if market_data_reliable and total_summary['market_data_complete']:
            _ensure_history_updated(total_summary)
        else:
            flash('Some live market data could not be fetched. Portfolio history was not updated.', 'warning')
    else:
        # No filters active, so we can use the summary we already have
        if market_data_reliable and summary['market_data_complete']:
            _ensure_history_updated(summary)
        else:
            flash('Some live market data could not be fetched. Portfolio history was not updated.', 'warning')

    # 4. Get the history data for the chart (now includes today's correct value).
    with sqlite3.connect(DATABASE) as conn:
        conn.row_factory = sqlite3.Row
        # Fetch the latest 365 days for the chart
        history_rows = conn.execute("SELECT date, value_jpy FROM portfolio_history ORDER BY date DESC LIMIT 365").fetchall()
        # Reverse the list so the chart shows oldest to newest
        history_data = [dict(row) for row in reversed(history_rows)]

    # 5. Render the page
    prices_last_updated = datetime.now().strftime('%Y-%m-%d %H:%M')
    return render_template('index.html', 
                           **summary, 
                           exchange_rate=exchange_rate, 
                           prices_last_updated=prices_last_updated, 
                           fx_latest_data_at=fx_latest_data_at,
                           history_data=history_data, 
                           brokers=BROKERS,
                           selected_broker=broker_filter,
                           selected_currency=currency_filter)

@app.route('/health')
def portfolio_health():
    """Shows concentration checks, sector exposure, and rebalance ideas."""
    exchange_data = get_exchange_rate()
    if isinstance(exchange_data, dict):
        exchange_rate = exchange_data['rate']
        fx_latest_data_at = exchange_data.get('latest_data_at')
    else:
        exchange_rate = exchange_data or 150.0
        fx_latest_data_at = None
    if exchange_data is None:
        flash('Could not fetch the live USD/JPY rate. Showing temporary health checks with a fallback rate.', 'warning')

    with sqlite3.connect(DATABASE) as conn:
        conn.row_factory = sqlite3.Row
        trades = conn.execute('SELECT * FROM trades ORDER BY trade_date ASC').fetchall()

    summary = _calculate_portfolio_summary(trades, exchange_rate)
    if not summary['market_data_complete']:
        flash('Some live market data could not be fetched. Health checks may be incomplete.', 'warning')

    settings = get_health_settings()
    health = _calculate_portfolio_health(summary, settings)
    prices_last_updated = datetime.now().strftime('%Y-%m-%d %H:%M')

    return render_template(
        'health.html',
        **summary,
        health=health,
        exchange_rate=exchange_rate,
        fx_latest_data_at=fx_latest_data_at,
        prices_last_updated=prices_last_updated
    )

@app.route('/health/settings', methods=['GET', 'POST'])
def health_settings():
    """Lets the user edit portfolio health-check thresholds."""
    settings = get_health_settings()

    if request.method == 'POST':
        _validate_csrf_token()
        settings, errors = _parse_health_settings_form(request.form)
        if errors:
            for error in errors:
                flash(error, 'danger')
        else:
            save_health_settings(settings)
            flash('Health check settings saved.', 'success')
            return redirect(url_for('portfolio_health'))

    return render_template(
        'health_settings.html',
        settings=settings,
        labels=HEALTH_SETTING_LABELS
    )

@app.route('/api/portfolio')
def api_portfolio():
    """
    API endpoint to return portfolio summary as JSON.
    Accepts 'broker' and 'currency' query parameters for filtering.
    """
    # 1. Get filters and raw data
    broker_filter = request.args.get('broker', 'all')
    currency_filter = request.args.get('currency', 'all')

    # Convert 'all' to None for the calculation function, which expects None for no filter
    effective_broker_filter = broker_filter if broker_filter != 'all' else None
    effective_currency_filter = currency_filter if currency_filter != 'all' else None

    exchange_data = get_exchange_rate()
    if exchange_data is None:
        return jsonify({'error': 'Live USD/JPY exchange rate is unavailable.'}), 503
    exchange_rate = exchange_data['rate'] if isinstance(exchange_data, dict) else exchange_data

    with sqlite3.connect(DATABASE) as conn:
        conn.row_factory = sqlite3.Row
        trades = conn.execute('SELECT * FROM trades ORDER BY trade_date ASC').fetchall()

    # 2. Perform the main calculation with filters.
    summary = _calculate_portfolio_summary(trades, exchange_rate, effective_broker_filter, effective_currency_filter)
    
    # 3. Return as JSON
    return jsonify(summary)

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

        # --- P&L Calculation (for all SELLs, reported only for the selected year) ---
        elif trade['trade_type'] == 'SELL':
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

            if trade_year == year:
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
        _validate_csrf_token()
        selected_year = request.form.get('year')
        if selected_year:
            report_data = generate_tax_report_data(int(selected_year))
    
    return render_template('tax_report.html', years=available_years, report_data=report_data)


@app.route('/add_trade', methods=['GET', 'POST'])
def add_trade():
    """Handles adding a new trade."""
    if request.method == 'POST':
        _validate_csrf_token()
        values, errors = _parse_trade_form(request.form)
        if errors:
            for error in errors:
                flash(error, 'danger')
            return render_template('add_trade.html', today=values.get('trade_date') or datetime.utcnow().strftime('%Y-%m-%d'), brokers=BROKERS)

        with sqlite3.connect(DATABASE) as conn:
            conn.execute(
                'INSERT INTO trades (symbol, name, trade_type, quantity, price, currency, trade_date, broker, fx_rate, fee_amount, fee_currency) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (
                    values['symbol'],
                    values['name'],
                    values['trade_type'],
                    values['quantity'],
                    values['price'],
                    values['currency'],
                    values['trade_date'],
                    values['broker'],
                    values['fx_rate'],
                    values['fee_amount'],
                    values['fee_currency']
                )
            )
        return redirect(url_for('list_trades'))
    return render_template('add_trade.html', today=datetime.utcnow().strftime('%Y-%m-%d'), brokers=BROKERS)

@app.route('/edit_trade/<int:trade_id>', methods=['GET', 'POST'])
def edit_trade(trade_id):
    """Handles editing an existing trade."""
    if request.method == 'POST':
        _validate_csrf_token()
        values, errors = _parse_trade_form(request.form)
        if errors:
            for error in errors:
                flash(error, 'danger')
            with sqlite3.connect(DATABASE) as conn:
                conn.row_factory = sqlite3.Row
                trade = conn.execute('SELECT * FROM trades WHERE id = ?', (trade_id,)).fetchone()
            if trade is None:
                abort(404)
            return render_template('edit_trade.html', trade=trade, brokers=BROKERS)

        with sqlite3.connect(DATABASE) as conn:
            conn.execute(
                'UPDATE trades SET symbol=?, name=?, trade_type=?, quantity=?, price=?, currency=?, trade_date=?, broker=?, fx_rate=?, fee_amount=?, fee_currency=? WHERE id=?',
                (
                    values['symbol'],
                    values['name'],
                    values['trade_type'],
                    values['quantity'],
                    values['price'],
                    values['currency'],
                    values['trade_date'],
                    values['broker'],
                    values['fx_rate'],
                    values['fee_amount'],
                    values['fee_currency'],
                    trade_id
                )
            )
        return redirect(url_for('list_trades'))

    # GET request: fetch trade and show edit form
    with sqlite3.connect(DATABASE) as conn:
        conn.row_factory = sqlite3.Row
        trade = conn.execute('SELECT * FROM trades WHERE id = ?', (trade_id,)).fetchone()
    if trade is None:
        abort(404)
    return render_template('edit_trade.html', trade=trade, brokers=BROKERS)

@app.route('/delete_trade/<int:trade_id>', methods=['POST'])
def delete_trade(trade_id):
    """Deletes a trade from the database."""
    _validate_csrf_token()
    with sqlite3.connect(DATABASE) as conn:
        conn.execute('DELETE FROM trades WHERE id = ?', (trade_id,))
    flash('Trade deleted.', 'success')
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
        _validate_csrf_token()
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
