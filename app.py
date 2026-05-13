from flask import Flask, render_template, request, redirect, url_for, jsonify, flash, Response, abort, session
import sqlite3
import yfinance as yf
from datetime import datetime, timedelta
import csv
import html
import io
import os
import re
import secrets
import urllib.request
from flask_caching import Cache
from dotenv import load_dotenv

# Load environment variables from .env file, making them available to os.environ
load_dotenv()

for proxy_var in ['HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy']:
    if os.environ.get(proxy_var) == 'http://127.0.0.1:9':
        os.environ.pop(proxy_var, None)

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
VERSION_FILE = os.path.join(basedir, 'VERSION')
DEFAULT_LATEST_VERSION_URL = 'https://raw.githubusercontent.com/enda2020/portfolio/main/VERSION'
APP_VERSION = os.environ.get('APP_VERSION')
if not APP_VERSION:
    try:
        with open(VERSION_FILE, encoding='utf-8') as version_file:
            APP_VERSION = version_file.read().strip()
    except OSError:
        APP_VERSION = '0.0.0'
LATEST_VERSION_URL = os.environ.get('APP_LATEST_VERSION_URL', DEFAULT_LATEST_VERSION_URL).strip()
BROKERS = ['Monex', 'Interactive Brokers']
ACCOUNT_TYPES = ['Specified', 'General', 'NISA', 'Taxable']
TRADE_DETAILS = ['Standard', 'Reinvestment']
MUTUAL_FUND_PRICE_UNITS = 10000
YAHOO_JP_FUND_CODE_ALIASES = {
    'JP90C000H1T1': '0331418A',
}
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
YFINANCE_TIMEOUT_SECONDS = float(os.environ.get('YFINANCE_TIMEOUT_SECONDS', '10'))
YAHOO_JP_TIMEOUT_SECONDS = float(os.environ.get('YAHOO_JP_TIMEOUT_SECONDS', '10'))

def _get_yfinance_history(api_symbol, **kwargs):
    """Fetch yfinance history with a bounded network timeout."""
    return yf.Ticker(api_symbol).history(timeout=YFINANCE_TIMEOUT_SECONDS, **kwargs)

def _get_yfinance_info(api_symbol):
    """Fetch yfinance metadata with a bounded timeout when supported."""
    ticker = yf.Ticker(api_symbol)
    try:
        return ticker.get_info(timeout=YFINANCE_TIMEOUT_SECONDS)
    except TypeError:
        return ticker.get_info()

def _log_yfinance_history(api_symbol, history, label='history'):
    if history.empty:
        print(f"--- yfinance {label} for {api_symbol}: empty response ---")
        return
    print(f"--- yfinance {label} for {api_symbol}: {len(history)} rows, latest={history.index[-1]} ---")

def _empty_market_data():
    return {
        'current_price': 0.0,
        'change_today': 0.0,
        'sparkline_data': [],
        'is_valid': False,
        'latest_data_at': None,
        'latest_data_sort': None,
        'quote_session': 'regular',
        'includes_extended_hours': False
    }

def _resolve_yahoo_jp_fund_code(symbol):
    normalized_symbol = (symbol or '').strip().upper()
    return YAHOO_JP_FUND_CODE_ALIASES.get(normalized_symbol, normalized_symbol)

def _format_yahoo_jp_quote_date(mm_dd_value):
    try:
        month, day = [int(part) for part in mm_dd_value.split('/')]
        today = datetime.now()
        quote_date = datetime(today.year, month, day)
        if quote_date.date() > today.date() + timedelta(days=30):
            quote_date = quote_date.replace(year=today.year - 1)
        return quote_date.strftime('%Y-%m-%d'), quote_date.timestamp()
    except Exception:
        return mm_dd_value, mm_dd_value

def _html_to_text(raw_html):
    without_scripts = re.sub(r'<(script|style)\b[^>]*>.*?</\1>', ' ', raw_html, flags=re.IGNORECASE | re.DOTALL)
    without_tags = re.sub(r'<[^>]+>', ' ', without_scripts)
    return re.sub(r'\s+', ' ', html.unescape(without_tags)).strip()

def _version_tuple(version):
    parts = re.findall(r'\d+', version or '')
    return tuple(int(part) for part in parts[:3])

@cache.memoize(timeout=3600)
def _fetch_latest_app_version(latest_version_url):
    try:
        request_obj = urllib.request.Request(
            latest_version_url,
            headers={'User-Agent': f'portfolio-tracker/{APP_VERSION}'}
        )
        with urllib.request.urlopen(request_obj, timeout=3) as response:
            latest = response.read().decode('utf-8', errors='replace').strip()
        return latest or None, None
    except Exception as e:
        return None, str(e)

def get_app_version_status():
    status = {
        'current': APP_VERSION,
        'latest': None,
        'is_outdated': False,
        'check_available': bool(LATEST_VERSION_URL),
        'error': None,
    }

    if not LATEST_VERSION_URL:
        return status

    latest, error = _fetch_latest_app_version(LATEST_VERSION_URL)
    status['latest'] = latest
    status['error'] = error
    if latest:
        status['is_outdated'] = _version_tuple(latest) > _version_tuple(APP_VERSION)

    return status

@cache.memoize(timeout=21600)
def get_yahoo_jp_mutual_fund_price(symbol):
    """Fetches the latest Japanese mutual fund NAV from Yahoo Finance Japan."""
    fund_code = _resolve_yahoo_jp_fund_code(symbol)
    url = f"https://finance.yahoo.co.jp/quote/{fund_code}"
    print(f"--- CACHE MISS: Fetching Yahoo JP mutual fund data for {symbol} (fund code: {fund_code}) ---")

    result = _empty_market_data()
    try:
        request_obj = urllib.request.Request(
            url,
            headers={
                'User-Agent': 'Mozilla/5.0',
                'Accept-Language': 'ja,en;q=0.9',
            }
        )
        with urllib.request.urlopen(request_obj, timeout=YAHOO_JP_TIMEOUT_SECONDS) as response:
            page_text = _html_to_text(response.read().decode('utf-8', errors='replace'))

        previous_day_label = r'\u524d\u65e5\u6bd4'
        quote_pattern = re.compile(
            rf'{re.escape(fund_code)}\s+([0-9,]+)\s+{previous_day_label}\s+([-+]?[0-9,]+)\s*\(\s*([-+]?\s*[0-9.]+)\s*%\s*\)\s+(\d{{2}}/\d{{2}})'
        )
        quote_match = quote_pattern.search(page_text)
        if not quote_match:
            quote_match = re.search(
                rf'\u6295\u8cc7\u4fe1\u8a17.*?([0-9,]+)\s+{previous_day_label}\s+([-+]?[0-9,]+)\s*\(\s*([-+]?\s*[0-9.]+)\s*%\s*\)\s+(\d{{2}}/\d{{2}})',
                page_text
            )

        if quote_match:
            nav, change, _change_percent, mm_dd = quote_match.groups()
            latest_data_at, latest_data_sort = _format_yahoo_jp_quote_date(mm_dd)
            result.update({
                'current_price': float(nav.replace(',', '')),
                'change_today': float(change.replace(',', '')),
                'is_valid': True,
                'latest_data_at': latest_data_at,
                'latest_data_sort': latest_data_sort,
                'quote_session': 'daily NAV'
            })
            print(f"--- Using Yahoo JP NAV for {symbol} from {latest_data_at}: {result['current_price']} ---")
        else:
            print(f"Could not parse Yahoo JP mutual fund quote for {symbol}.")
    except Exception as e:
        print(f"Could not fetch Yahoo JP mutual fund quote for {symbol}: {e}")

    return result

@cache.memoize()
def get_exchange_rate():
    """Fetches the current USD/JPY exchange rate."""
    print("--- CACHE MISS: Fetching live USD/JPY exchange rate from yfinance ---")
    try:
        # Use a longer period to be robust against weekends/holidays
        history = _get_yfinance_history("JPY=X", period="5d")
        _log_yfinance_history("JPY=X", history)
        if not history.empty and ('Close' in history.columns or 'close' in history.columns):
            # The column name can be 'Close' or 'close'. For the most recent entry,
            # this value represents the "last price", not necessarily a closing price.
            price_col = 'Close' if 'Close' in history.columns else 'close'
            last_price = float(history[price_col].iloc[-1])
            latest_data_at, latest_data_sort = _format_market_timestamp(history.index[-1])
            print(f"--- Using last available price for JPY=X from {latest_data_at}: {last_price} ---")
            return {
                'rate': last_price,
                'latest_data_at': latest_data_at,
                'latest_data_sort': latest_data_sort
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

    result = _empty_market_data()

    try:
        # Fetch enough data for a 7-day sparkline and one previous day for change.
        history = _get_yfinance_history(api_symbol, period="10d")
        _log_yfinance_history(api_symbol, history)
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
            
            # Get the last 7 days for the sparkline
            result['sparkline_data'] = list(history[price_col].tail(7))

            if currency == 'USD':
                intraday_history = _get_yfinance_history(api_symbol, period="5d", interval="5m", prepost=True)
                _log_yfinance_history(api_symbol, intraday_history, label='extended-hours history')
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

def get_market_price(symbol, currency, instrument_type='stock'):
    if instrument_type == 'mutual_fund':
        return get_yahoo_jp_mutual_fund_price(symbol)
    return get_stock_price(symbol, currency)

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
        info = _get_yfinance_info(api_symbol)
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

def get_instrument_profile(symbol, currency, instrument_type='stock'):
    if instrument_type == 'mutual_fund':
        return {
            'sector': 'Fund / ETF',
            'industry': 'Mutual Fund',
            'quote_type': 'MUTUALFUND',
            'is_valid': True
        }
    return get_stock_profile(symbol, currency)

def _percent(value, total):
    return (value / total) * 100 if total else 0

def _format_relative_time(timestamp_value):
    if timestamp_value is None:
        return None

    try:
        elapsed_seconds = max(0, datetime.now().timestamp() - float(timestamp_value))
    except (TypeError, ValueError):
        return str(timestamp_value)

    if elapsed_seconds < 60:
        return 'just now'

    elapsed_minutes = int(elapsed_seconds // 60)
    if elapsed_minutes < 60:
        return f"{elapsed_minutes} min ago"

    elapsed_hours = int(elapsed_minutes // 60)
    if elapsed_hours < 24:
        return f"{elapsed_hours} hour{'s' if elapsed_hours != 1 else ''} ago"

    elapsed_days = int(elapsed_hours // 24)
    return f"{elapsed_days} day{'s' if elapsed_days != 1 else ''} ago"

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
        profile = get_instrument_profile(stock['symbol'], stock['currency'], stock.get('instrument_type', 'stock'))
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
    return {
        'csrf_token': session['csrf_token'],
        'app_version': get_app_version_status()
    }

def _validate_csrf_token():
    token = session.get('csrf_token')
    if not token or not secrets.compare_digest(token, request.form.get('csrf_token', '')):
        abort(400)

def _parse_optional_float(value):
    value = (value or '').strip()
    return float(value) if value else None

def _row_get(row, key, default=None):
    if row is None:
        return default
    if isinstance(row, dict):
        return row.get(key, default)
    try:
        if key in row.keys():
            return row[key]
    except AttributeError:
        pass
    return default

def _price_unit_factor(instrument_type):
    return MUTUAL_FUND_PRICE_UNITS if instrument_type == 'mutual_fund' else 1

def _trade_gross_value(trade, price=None):
    instrument_type = _row_get(trade, 'instrument_type', 'stock')
    quantity = _row_get(trade, 'quantity', 0) or 0
    trade_price = _row_get(trade, 'price', 0) if price is None else price
    return (quantity * (trade_price or 0)) / _price_unit_factor(instrument_type)

def _display_price_basis(value, instrument_type):
    return value * _price_unit_factor(instrument_type)

def _normalize_stock_trade(trade):
    normalized = dict(trade)
    normalized['instrument_type'] = 'stock'
    return normalized

def _normalize_mutual_fund_trade(trade):
    return {
        'id': trade['id'],
        'symbol': trade['fund_code'],
        'name': trade['fund_name'],
        'instrument_type': 'mutual_fund',
        'trade_type': trade['transaction_type'],
        'quantity': trade['executed_units'],
        'price': trade['nav_per_10000'],
        'currency': trade['currency'],
        'trade_date': trade['trade_date'],
        'broker': trade['broker'],
        'fx_rate': trade['fx_rate'],
        'fee_amount': 0,
        'fee_currency': None,
    }

def _fetch_normalized_trades(order='ASC'):
    with sqlite3.connect(DATABASE) as conn:
        conn.row_factory = sqlite3.Row
        stock_trades = conn.execute(f'SELECT * FROM trades ORDER BY trade_date {order}').fetchall()
        fund_trades = conn.execute(f'SELECT * FROM mutual_fund_trades ORDER BY trade_date {order}').fetchall()

    normalized = [_normalize_stock_trade(trade) for trade in stock_trades]
    normalized.extend(_normalize_mutual_fund_trade(trade) for trade in fund_trades)
    reverse = order.upper() == 'DESC'
    return sorted(normalized, key=lambda trade: trade['trade_date'], reverse=reverse)

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

def _parse_mutual_fund_trade_form(form):
    errors = []
    values = {
        'fund_code': form.get('fund_code', '').strip().upper(),
        'fund_name': form.get('fund_name', '').strip(),
        'transaction_type': form.get('transaction_type', '').strip().upper(),
        'transaction_detail': None,
        'account_type': None,
        'currency': 'JPY',
        'executed_units': None,
        'nav_per_10000': None,
        'trade_date': form.get('trade_date', '').strip(),
        'settlement_date': None,
        'settlement_amount': None,
        'broker': form.get('broker', '').strip(),
        'fx_rate': None,
    }

    required_fields = ['fund_code', 'fund_name', 'transaction_type', 'currency', 'trade_date', 'broker']
    for field in required_fields:
        if not values[field]:
            errors.append(f"{field.replace('_', ' ').title()} is required.")

    if values['transaction_type'] and values['transaction_type'] not in ['BUY', 'SELL']:
        errors.append("Transaction type must be BUY or SELL.")
    if values['broker'] and values['broker'] not in BROKERS:
        errors.append("Broker is not recognized.")

    try:
        values['executed_units'] = float(form.get('executed_units', ''))
        if values['executed_units'] <= 0:
            errors.append("Executed units must be positive.")
    except (TypeError, ValueError):
        errors.append("Executed units must be a valid number.")

    try:
        values['nav_per_10000'] = float(form.get('nav_per_10000', ''))
        if values['nav_per_10000'] < 0:
            errors.append("NAV cannot be negative.")
    except (TypeError, ValueError):
        errors.append("NAV must be a valid number.")

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
            CREATE TABLE IF NOT EXISTS mutual_fund_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                fund_code TEXT NOT NULL,
                fund_name TEXT NOT NULL,
                transaction_type TEXT NOT NULL, -- 'BUY' or 'SELL'
                transaction_detail TEXT,
                account_type TEXT,
                currency TEXT NOT NULL DEFAULT 'JPY',
                executed_units REAL NOT NULL,
                nav_per_10000 REAL NOT NULL,
                trade_date TEXT NOT NULL,
                settlement_date TEXT,
                settlement_amount REAL,
                broker TEXT,
                fx_rate REAL
            )
        ''')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS portfolio_history (
                date TEXT PRIMARY KEY,
                value_usd REAL NOT NULL,
                value_jpy REAL NOT NULL,
                unrealized_pnl_usd REAL,
                unrealized_pnl_jpy REAL
            )
        ''')
        history_columns = [row[1] for row in conn.execute("PRAGMA table_info(portfolio_history)").fetchall()]
        if 'unrealized_pnl_usd' not in history_columns:
            conn.execute("ALTER TABLE portfolio_history ADD COLUMN unrealized_pnl_usd REAL")
        if 'unrealized_pnl_jpy' not in history_columns:
            conn.execute("ALTER TABLE portfolio_history ADD COLUMN unrealized_pnl_jpy REAL")
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
    today_str = datetime.now().strftime('%Y-%m-%d')
    for trade in trades:
        # Aggregate by both symbol and broker for more granular tracking
        instrument_type = trade['instrument_type'] if 'instrument_type' in trade.keys() else 'stock'
        key = (trade['symbol'], trade['broker'], instrument_type)
        if key not in holdings:
            holdings[key] = {
                'symbol': trade['symbol'],
                'broker': trade['broker'],
                'instrument_type': instrument_type,
                'name': trade['name'],
                'currency': trade['currency'],
                'quantity': 0,
                'total_cost': 0,
                'realized_pnl_native': 0,
                'today_buy_quantity': 0,
                'today_buy_cost': 0
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
            trade_cost_native = _trade_gross_value(trade) + fee_in_native_currency
            holdings[key]['quantity'] += trade['quantity']
            holdings[key]['total_cost'] += trade_cost_native
            if trade['trade_date'] == today_str:
                holdings[key]['today_buy_quantity'] += trade['quantity']
                holdings[key]['today_buy_cost'] += trade_cost_native
        elif trade['trade_type'] == 'SELL':
            avg_unit_cost_basis = 0
            if holdings[key]['quantity'] > 0:
                avg_unit_cost_basis = holdings[key]['total_cost'] / holdings[key]['quantity']
            
            cost_of_shares_sold = trade['quantity'] * avg_unit_cost_basis
            proceeds = _trade_gross_value(trade) - fee_in_native_currency
            
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

        combined_key = (data['symbol'], data['currency'], data['instrument_type'])
        if combined_key not in combined_holdings:
            combined_holdings[combined_key] = {
                'symbol': data['symbol'],
                'broker': data['broker'],
                'brokers': [data['broker']],
                'instrument_type': data['instrument_type'],
                'name': data['name'],
                'currency': data['currency'],
                'quantity': 0,
                'total_cost': 0,
                'today_buy_quantity': 0,
                'today_buy_cost': 0,
            }
        elif data['broker'] not in combined_holdings[combined_key]['brokers']:
            combined_holdings[combined_key]['brokers'].append(data['broker'])

        combined_holdings[combined_key]['quantity'] += data['quantity']
        combined_holdings[combined_key]['total_cost'] += data['total_cost']
        combined_holdings[combined_key]['today_buy_quantity'] += data['today_buy_quantity']
        combined_holdings[combined_key]['today_buy_cost'] += data['today_buy_cost']

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
            data['avg_unit_cost_basis'] = data['total_cost'] / data['quantity']
            data['avg_cost_basis'] = _display_price_basis(data['avg_unit_cost_basis'], data['instrument_type'])
        else:
            data['avg_unit_cost_basis'] = 0
            data['avg_cost_basis'] = 0

        # Get current market data
        market_data = get_market_price(data['symbol'], data['currency'], data['instrument_type'])
        if not market_data.get('is_valid'):
            market_data_complete = False
            market_data = {
                **market_data,
                'current_price': data['avg_cost_basis'],
                'change_today': 0.0,
                'quote_session': 'cost basis fallback'
            }
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

        # Calculate % change for today
        prev_close = data['current_price'] - data['change_today']
        if prev_close > 0:
            data['change_today_percent'] = (data['change_today'] / prev_close) * 100
        else:
            data['change_today_percent'] = 0.0

        current_value_native = (data['quantity'] * data['current_price']) / _price_unit_factor(data['instrument_type'])

        # Calculate Today's P&L for this holding. Shares bought today use purchase cost;
        # overnight shares use the market's previous-close change.
        today_buy_quantity = min(data['today_buy_quantity'], data['quantity'])
        today_buy_cost = data['today_buy_cost']
        if data['today_buy_quantity'] > data['quantity'] and data['today_buy_quantity'] > 0:
            today_buy_cost *= data['quantity'] / data['today_buy_quantity']
        overnight_quantity = max(0, data['quantity'] - today_buy_quantity)
        overnight_today_pnl = (overnight_quantity * data['change_today']) / _price_unit_factor(data['instrument_type'])
        today_buy_current_value = (today_buy_quantity * data['current_price']) / _price_unit_factor(data['instrument_type'])
        today_pnl_native = overnight_today_pnl + (today_buy_current_value - today_buy_cost)
        data['today_pnl_native'] = today_pnl_native
        
        # Calculate P&L
        cost_of_holding = data['quantity'] * data['avg_unit_cost_basis']
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
        'oldest_market_data_ago': _format_relative_time(oldest_market_data['sort']) if oldest_market_data else None,
        'latest_market_data_ago': _format_relative_time(latest_market_data['sort']) if latest_market_data else None,
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
        last_snapshot = conn.execute("SELECT date, value_usd, value_jpy, unrealized_pnl_usd, unrealized_pnl_jpy FROM portfolio_history ORDER BY date DESC LIMIT 1").fetchone()
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
                            "INSERT OR IGNORE INTO portfolio_history (date, value_usd, value_jpy, unrealized_pnl_usd, unrealized_pnl_jpy) VALUES (?, ?, ?, ?, ?)",
                            (
                                missing_date_str,
                                last_snapshot['value_usd'],
                                last_snapshot['value_jpy'],
                                last_snapshot['unrealized_pnl_usd'],
                                last_snapshot['unrealized_pnl_jpy']
                            )
                        )
        
        # 3. Insert or Update (Upsert) today's value using the provided summary.
        # This ensures that today's value is always the most recently calculated one,
        # correcting any previously stored incorrect (e.g., zero) values.
        print(f"Upserting portfolio history snapshot for {today_str}...")
        conn.execute(
            """
            INSERT INTO portfolio_history (date, value_usd, value_jpy, unrealized_pnl_usd, unrealized_pnl_jpy)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(date) DO UPDATE SET
                value_usd = excluded.value_usd,
                value_jpy = excluded.value_jpy,
                unrealized_pnl_usd = excluded.unrealized_pnl_usd,
                unrealized_pnl_jpy = excluded.unrealized_pnl_jpy
            """,
            (
                today_str,
                current_summary['total_value_usd'],
                current_summary['total_value_jpy'],
                current_summary['total_unrealized_pnl_usd'],
                current_summary['total_unrealized_pnl_jpy']
            )
        )
        print(f"Saved portfolio snapshot for {today_str}.")

def _calculate_realized_pnl_jpy_until(trades, through_date, fallback_exchange_rate):
    """Calculates realized JPY P&L through a date using trade-date FX rates."""
    holdings = {}
    total_realized_pnl_jpy = 0.0
    through_date_str = through_date.strftime('%Y-%m-%d')

    for trade in trades:
        if trade['trade_date'] > through_date_str:
            continue

        key = (trade['symbol'], trade['broker'], trade['instrument_type'])
        if key not in holdings:
            holdings[key] = {
                'quantity': 0,
                'total_cost_jpy': 0
            }

        fee_jpy = 0
        if trade['fee_amount']:
            if trade['fee_currency'] == 'JPY':
                fee_jpy = trade['fee_amount']
            elif trade['fee_currency'] == 'USD':
                fee_jpy = trade['fee_amount'] * (trade['fx_rate'] or fallback_exchange_rate)

        gross_value = _trade_gross_value(trade)
        value_jpy = gross_value
        if trade['currency'] == 'USD':
            value_jpy = gross_value * (trade['fx_rate'] or fallback_exchange_rate)

        holding = holdings[key]
        if trade['trade_type'] == 'BUY':
            holding['quantity'] += trade['quantity']
            holding['total_cost_jpy'] += value_jpy + fee_jpy
        elif trade['trade_type'] == 'SELL':
            avg_cost_jpy = 0
            if holding['quantity'] > 0:
                avg_cost_jpy = holding['total_cost_jpy'] / holding['quantity']
            cost_of_sale_jpy = trade['quantity'] * avg_cost_jpy
            proceeds_jpy = value_jpy - fee_jpy
            total_realized_pnl_jpy += proceeds_jpy - cost_of_sale_jpy
            holding['quantity'] -= trade['quantity']
            holding['total_cost_jpy'] -= cost_of_sale_jpy

    return total_realized_pnl_jpy

def _calculate_portfolio_performance(current_summary, trades, exchange_rate):
    """Builds P&L-change snapshots for the health page performance cards."""
    today_date = datetime.now().date()
    current_value_jpy = current_summary['total_value_jpy']
    today_pnl_jpy = current_summary['total_today_pnl_jpy']
    current_realized_pnl_jpy = _calculate_realized_pnl_jpy_until(trades, today_date, exchange_rate)
    current_total_pnl_jpy = current_summary['total_unrealized_pnl_jpy'] + current_realized_pnl_jpy

    with sqlite3.connect(DATABASE) as conn:
        conn.row_factory = sqlite3.Row
        history_rows = conn.execute(
            "SELECT date, value_jpy, unrealized_pnl_jpy FROM portfolio_history ORDER BY date ASC"
        ).fetchall()

    history = []
    for row in history_rows:
        try:
            history.append({
                'date': datetime.strptime(row['date'], '%Y-%m-%d').date(),
                'date_label': row['date'],
                'value_jpy': row['value_jpy'],
                'unrealized_pnl_jpy': row['unrealized_pnl_jpy']
            })
        except (TypeError, ValueError):
            continue

    def build_entry(key, label, baseline_pnl_jpy, baseline_value_jpy, baseline_date=None, note=None):
        change_jpy = current_total_pnl_jpy - baseline_pnl_jpy
        change_percent = (change_jpy / baseline_value_jpy) * 100 if baseline_value_jpy else 0
        return {
            'key': key,
            'label': label,
            'change_jpy': change_jpy,
            'change_percent': change_percent,
            'baseline_pnl_jpy': baseline_pnl_jpy,
            'baseline_value_jpy': baseline_value_jpy,
            'baseline_date': baseline_date,
            'note': note,
            'has_baseline': baseline_value_jpy > 0
        }

    def closest_baseline(days_back):
        target_date = today_date - timedelta(days=days_back)
        previous_rows = [
            row for row in history
            if row['date'] <= target_date and row['unrealized_pnl_jpy'] is not None
        ]
        if previous_rows:
            return previous_rows[-1], False
        available_rows = [row for row in history if row['unrealized_pnl_jpy'] is not None]
        if available_rows:
            return available_rows[0], True
        return None, False

    day_baseline_value = current_value_jpy - today_pnl_jpy
    day_baseline_pnl = current_total_pnl_jpy - today_pnl_jpy
    performance = [
        build_entry('day', 'Day', day_baseline_pnl, day_baseline_value, today_date.strftime('%Y-%m-%d'), 'today')
    ]

    for key, label, days_back in [
        ('week', 'Week', 7),
        ('month', 'Month', 30),
        ('quarter', 'Quarter', 90),
        ('year', 'Year', 365),
    ]:
        baseline, is_partial = closest_baseline(days_back)
        if baseline:
            note = f"since {baseline['date_label']}"
            if is_partial:
                note = f"since first snapshot {baseline['date_label']}"
            baseline_realized_pnl_jpy = _calculate_realized_pnl_jpy_until(trades, baseline['date'], exchange_rate)
            baseline_total_pnl_jpy = baseline['unrealized_pnl_jpy'] + baseline_realized_pnl_jpy
            performance.append(
                build_entry(key, label, baseline_total_pnl_jpy, baseline['value_jpy'], baseline['date_label'], note)
            )
        else:
            performance.append(build_entry(key, label, current_total_pnl_jpy, current_value_jpy, note='no P&L history yet'))

    return performance

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
        fx_latest_data_ago = _format_relative_time(exchange_data.get('latest_data_sort'))
    else:
        exchange_rate = exchange_data or 150.0
        fx_latest_data_at = None
        fx_latest_data_ago = None
    trades = _fetch_normalized_trades()

    # 2. Perform the main calculation with filters for display. This is the single source of truth.
    summary = _calculate_portfolio_summary(trades, exchange_rate, effective_broker_filter, effective_currency_filter)
    has_live_data_issue = (not market_data_reliable) or (not summary['market_data_complete'])
    if has_live_data_issue:
        flash('Live market data is temporarily unavailable. Showing cost-basis fallback values and skipping today\'s history snapshot.', 'info')

    # 3. Ensure history is up-to-date with the TOTAL portfolio value.
    # If filters are active, we must re-calculate the summary without them for the history.
    if effective_broker_filter or effective_currency_filter:
        total_summary = _calculate_portfolio_summary(trades, exchange_rate)
        if market_data_reliable and total_summary['market_data_complete']:
            _ensure_history_updated(total_summary)
    else:
        # No filters active, so we can use the summary we already have
        if market_data_reliable and summary['market_data_complete']:
            _ensure_history_updated(summary)

    # 4. Get the history data for the chart (now includes today's correct value).
    with sqlite3.connect(DATABASE) as conn:
        conn.row_factory = sqlite3.Row
        # Fetch the latest 365 days for the chart
        history_rows = conn.execute("SELECT date, value_jpy, unrealized_pnl_jpy FROM portfolio_history ORDER BY date DESC LIMIT 365").fetchall()
        # Reverse the list so the chart shows oldest to newest
        history_data = [dict(row) for row in reversed(history_rows)]

    # 5. Render the page
    prices_last_updated = datetime.now().strftime('%Y-%m-%d %H:%M')
    return render_template('index.html', 
                           **summary, 
                           exchange_rate=exchange_rate, 
                           prices_last_updated=prices_last_updated,
                           prices_last_updated_ago='just now',
                           fx_latest_data_at=fx_latest_data_at,
                           fx_latest_data_ago=fx_latest_data_ago,
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
        fx_latest_data_ago = _format_relative_time(exchange_data.get('latest_data_sort'))
    else:
        exchange_rate = exchange_data or 150.0
        fx_latest_data_at = None
        fx_latest_data_ago = None
    trades = _fetch_normalized_trades()

    summary = _calculate_portfolio_summary(trades, exchange_rate)
    if exchange_data is None or not summary['market_data_complete']:
        flash('Live market data is temporarily unavailable. Health checks are using cost-basis fallback values.', 'info')
    else:
        _ensure_history_updated(summary)

    settings = get_health_settings()
    health = _calculate_portfolio_health(summary, settings)
    performance = _calculate_portfolio_performance(summary, trades, exchange_rate)
    prices_last_updated = datetime.now().strftime('%Y-%m-%d %H:%M')

    return render_template(
        'health.html',
        **summary,
        health=health,
        performance=performance,
        exchange_rate=exchange_rate,
        fx_latest_data_at=fx_latest_data_at,
        fx_latest_data_ago=fx_latest_data_ago,
        prices_last_updated_ago='just now',
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

    trades = _fetch_normalized_trades()

    # 2. Perform the main calculation with filters.
    summary = _calculate_portfolio_summary(trades, exchange_rate, effective_broker_filter, effective_currency_filter)
    
    # 3. Return as JSON
    return jsonify(summary)

@app.route('/api/version')
def api_version():
    return jsonify(get_app_version_status())

def generate_tax_report_data(year):
    """
    Generates a tax report for a given year using the moving-average cost basis method.
    All calculations are performed in JPY.
    """
    trades = _fetch_normalized_trades()

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
                cost_jpy = _trade_gross_value(trade) + fee_jpy
            elif trade['currency'] == 'USD' and trade['fx_rate']:
                cost_jpy = (_trade_gross_value(trade) * trade['fx_rate']) + fee_jpy

            # Calculate cost of the buy transaction in native currency
            cost_native = _trade_gross_value(trade) + fee_native

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
                proceeds_jpy = _trade_gross_value(trade) - fee_jpy
            elif trade['currency'] == 'USD' and trade['fx_rate']:
                proceeds_jpy = (_trade_gross_value(trade) * trade['fx_rate']) - fee_jpy

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
                    'avg_cost_per_share_jpy': _display_price_basis(avg_cost_jpy, trade['instrument_type']),
                    'avg_cost_per_share_native': _display_price_basis(avg_cost_native, trade['instrument_type']),
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
        trade_rows = conn.execute('SELECT * FROM trades ORDER BY trade_date DESC').fetchall()

    today = datetime.now().date()
    trades = []
    for trade in trade_rows:
        trade_data = dict(trade)
        trade_data['days_since_purchase'] = None
        trade_data['sell_allowed'] = None
        if trade_data['trade_type'] == 'BUY':
            try:
                purchase_date = datetime.strptime(trade_data['trade_date'], '%Y-%m-%d').date()
                trade_data['days_since_purchase'] = max(0, (today - purchase_date).days)
                trade_data['sell_allowed'] = trade_data['days_since_purchase'] >= 30
            except ValueError:
                pass
        trades.append(trade_data)

    return render_template('trades.html', trades=trades)

@app.route('/tax_report', methods=['GET', 'POST'])
def tax_report():
    """Handles the tax report generation."""
    with sqlite3.connect(DATABASE) as conn:
        # Get distinct years from trades to populate the dropdown
        years_cursor = conn.execute(
            """
            SELECT year FROM (
                SELECT DISTINCT SUBSTR(trade_date, 1, 4) as year FROM trades
                UNION
                SELECT DISTINCT SUBSTR(trade_date, 1, 4) as year FROM mutual_fund_trades
            ) ORDER BY year DESC
            """
        )
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
            return render_template('add_trade.html', today=values.get('trade_date') or datetime.utcnow().strftime('%Y-%m-%d'), values=values, brokers=BROKERS)

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
    return render_template('add_trade.html', today=datetime.utcnow().strftime('%Y-%m-%d'), values={}, brokers=BROKERS)

@app.route('/mutual_funds')
def list_mutual_fund_trades():
    """Displays all mutual fund transactions."""
    with sqlite3.connect(DATABASE) as conn:
        conn.row_factory = sqlite3.Row
        trades = conn.execute('SELECT * FROM mutual_fund_trades ORDER BY trade_date DESC').fetchall()
    return render_template('mutual_fund_trades.html', trades=trades)

@app.route('/add_mutual_fund_trade', methods=['GET', 'POST'])
def add_mutual_fund_trade():
    """Handles adding a Japanese mutual fund transaction."""
    if request.method == 'POST':
        _validate_csrf_token()
        values, errors = _parse_mutual_fund_trade_form(request.form)
        if errors:
            for error in errors:
                flash(error, 'danger')
            return render_template(
                'add_mutual_fund_trade.html',
                today=values.get('trade_date') or datetime.utcnow().strftime('%Y-%m-%d'),
                values=values,
                brokers=BROKERS
            )

        with sqlite3.connect(DATABASE) as conn:
            conn.execute(
                """
                INSERT INTO mutual_fund_trades (
                    fund_code, fund_name, transaction_type, transaction_detail,
                    account_type, currency, executed_units, nav_per_10000,
                    trade_date, settlement_date, settlement_amount, broker, fx_rate
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    values['fund_code'],
                    values['fund_name'],
                    values['transaction_type'],
                    values['transaction_detail'],
                    values['account_type'],
                    values['currency'],
                    values['executed_units'],
                    values['nav_per_10000'],
                    values['trade_date'],
                    values['settlement_date'],
                    values['settlement_amount'],
                    values['broker'],
                    values['fx_rate']
                )
            )
        return redirect(url_for('list_mutual_fund_trades'))

    return render_template(
        'add_mutual_fund_trade.html',
        today=datetime.utcnow().strftime('%Y-%m-%d'),
        values={},
        brokers=BROKERS
    )

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

@app.route('/delete_mutual_fund_trade/<int:trade_id>', methods=['POST'])
def delete_mutual_fund_trade(trade_id):
    """Deletes a mutual fund transaction."""
    _validate_csrf_token()
    with sqlite3.connect(DATABASE) as conn:
        conn.execute('DELETE FROM mutual_fund_trades WHERE id = ?', (trade_id,))
    flash('Mutual fund transaction deleted.', 'success')
    return redirect(url_for('list_mutual_fund_trades'))

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
                            'symbol': row['symbol'].strip().upper(), 'name': row['name'], 'trade_type': trade_type,
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
                                (
                                    trade['symbol'],
                                    trade['name'],
                                    trade['trade_type'],
                                    trade['quantity'],
                                    trade['price'],
                                    trade['currency'],
                                    trade['trade_date'],
                                    trade['broker'],
                                    trade['fx_rate'],
                                    trade['fee_amount'],
                                    trade['fee_currency']
                                )
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
