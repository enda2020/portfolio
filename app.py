from flask import Flask, render_template, request, redirect, url_for, jsonify, flash, Response, abort, session
import sqlite3
import yfinance as yf
from datetime import datetime, timedelta, time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
import csv
import html
import io
import json
import os
import re
import secrets
import urllib.request
from flask_caching import Cache
from dotenv import load_dotenv
try:
    from bs4 import BeautifulSoup
except ImportError:
    BeautifulSoup = None

# Load environment variables from .env file, making them available to os.environ
load_dotenv()

for proxy_var in ['HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy']:
    if os.environ.get(proxy_var) == 'http://127.0.0.1:9':
        os.environ.pop(proxy_var, None)

app = Flask(__name__)
# Load the secret key from an environment variable for production.
# If it is missing locally, use an ephemeral key instead of a reusable hardcoded value.
app.secret_key = os.environ.get('FLASK_SECRET_KEY') or secrets.token_urlsafe(32)

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
CONFIG_OPTION_DEFAULTS = {
    'broker': ['Monex', 'Interactive Brokers'],
    'account_name': ['Default'],
    'tax_status': ['Taxable', 'Non Taxable'],
}
CONFIG_OPTION_LABELS = {
    'broker': 'Brokers',
    'account_name': 'Account Names',
    'tax_status': 'Tax Statuses',
}
BROKERS = CONFIG_OPTION_DEFAULTS['broker']
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
APP_SETTING_DEFAULTS = {
    'daily_reset_time': os.environ.get('PORTFOLIO_DAILY_RESET_TIME', '08:30').strip(),
    'daily_reset_timezone': os.environ.get('PORTFOLIO_DAILY_RESET_TIMEZONE', 'Asia/Tokyo').strip(),
}
APP_SETTING_LABELS = {
    'daily_reset_time': 'Daily Reset Time',
    'daily_reset_timezone': 'Daily Reset Timezone',
}
IMPORT_PROFILE_DEFAULTS = [
    {
        'name': 'monex_stock',
        'instrument_type': 'stock',
        'encoding': 'cp932',
        'header_row': 2,
        'row_filters': [
            {'column': '商品', 'equals': '株式'},
            {'column': '取引', 'in': ['お買付', 'ご売却']},
        ],
        'mappings': {
            'symbol': {'column': '銘柄コード', 'transform': 'monex_jp_symbol'},
            'name': {'column': '銘柄名'},
            'trade_type': {'column': '取引', 'map': {'お買付': 'BUY', 'ご売却': 'SELL'}},
            'quantity': {'column': '数量（株/口）/返済数量', 'transform': 'number'},
            'price': {'column': '単価/返済約定単価', 'transform': 'number'},
            'trade_date': {'column': '約定日', 'transform': 'date_slash'},
            'fee_amount': {'columns': ['手数料', '税金(手数料消費税及び譲渡益税)'], 'transform': 'abs_sum'},
        },
        'defaults': {
            'currency': 'JPY',
            'broker': 'Monex',
            'account_name': 'Default',
            'tax_status': 'Taxable',
            'fee_currency': 'JPY',
        },
    },
    {
        'name': 'monex_mutual_fund',
        'instrument_type': 'mutual_fund',
        'encoding': 'cp932',
        'header_row': 2,
        'row_filters': [
            {'column': '商品', 'equals': '投信'},
            {'column': '取引', 'in': ['お買付', 'ご売却', '再投資買付']},
        ],
        'mappings': {
            'fund_code': {'column': '銘柄コード', 'transform': 'strip'},
            'fund_name': {'column': '銘柄名'},
            'transaction_type': {'column': '取引', 'map': {'お買付': 'BUY', 'ご売却': 'SELL', '再投資買付': 'BUY'}},
            'executed_units': {'column': '数量（株/口）/返済数量', 'transform': 'number'},
            'nav_per_10000': {'column': '単価/返済約定単価', 'transform': 'number'},
            'trade_date': {'column': '約定日', 'transform': 'date_slash'},
        },
        'defaults': {
            'currency': 'JPY',
            'broker': 'Monex',
            'account_name': 'Default',
            'tax_status': 'Taxable',
        },
    },
    {
        'name': 'monex_foreign_stock',
        'instrument_type': 'stock',
        'encoding': 'cp932',
        'header_row': 2,
        'row_filters': [
            {'column': '商品', 'equals': '外国株式'},
            {'column': '取引', 'in': ['お買付', 'ご売却']},
        ],
        'mappings': {
            'symbol': {'column': '銘柄コード', 'transform': 'strip'},
            'name': {'column': '銘柄名'},
            'trade_type': {'column': '取引', 'map': {'お買付': 'BUY', 'ご売却': 'SELL'}},
            'quantity': {'column': '数量（株/口）/返済数量', 'transform': 'number'},
            'price': {'column': '単価/返済約定単価', 'transform': 'number'},
            'currency': {'column': '単価/返済約定単価', 'transform': 'currency_from_value'},
            'trade_date': {'column': '約定日', 'transform': 'date_slash'},
            'fee_amount': {'columns': ['手数料', '税金(手数料消費税及び譲渡益税)', '諸経費'], 'transform': 'abs_sum'},
        },
        'defaults': {
            'currency': 'USD',
            'broker': 'Monex',
            'account_name': 'Default',
            'tax_status': 'Taxable',
            'fee_currency': 'USD',
        },
    },
    {
        'name': 'monex_dividend',
        'instrument_type': 'dividend',
        'encoding': 'cp932',
        'header_row': 2,
        'row_filters': [
            {'column': '取引', 'in': ['分配金', '配当金']},
        ],
        'mappings': {
            'symbol': {'column': '銘柄コード', 'transform': 'strip'},
            'name': {'column': '銘柄名'},
            'payment_date': {'column': '受渡日', 'transform': 'date_slash'},
            'currency': {'column': '単価/返済約定単価', 'transform': 'currency_from_value'},
            'quantity': {'column': '数量（株/口）/返済数量', 'transform': 'number'},
            'amount_per_share': {'column': '単価/返済約定単価', 'transform': 'number'},
            'gross_amount': {'column': '利金・分配金・償還金', 'transform': 'number_zero_none'},
            'tax_withheld': {'column': '税金(手数料消費税及び譲渡益税)', 'transform': 'abs_number'},
            'foreign_tax_withheld': {'column': '手数料', 'transform': 'abs_number'},
        },
        'defaults': {
            'currency': 'JPY',
            'broker': 'Monex',
            'account_name': 'Default',
            'tax_status': 'Taxable',
            'source_country': 'JP',
            'security_type': 'mutual_fund',
            'tax_treatment': 'undecided',
        },
    },
]

def _parse_daily_reset_time(value):
    try:
        hour, minute = [int(part) for part in value.split(':', 1)]
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return time(hour, minute)
    except (AttributeError, TypeError, ValueError):
        pass
    print(f"Invalid daily reset time={value!r}; falling back to 08:30.")
    return time(8, 30)

def _is_valid_timezone(timezone_name):
    try:
        ZoneInfo(timezone_name)
        return True
    except (ZoneInfoNotFoundError, ValueError):
        return False

def _daily_reset_zone(timezone_name):
    try:
        return ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError):
        print(f"Invalid daily reset timezone={timezone_name!r}; falling back to Asia/Tokyo.")
        return ZoneInfo('Asia/Tokyo')

def _get_daily_reset_settings():
    try:
        settings = get_app_settings()
        return settings['daily_reset_time'], settings['daily_reset_timezone']
    except Exception:
        return APP_SETTING_DEFAULTS['daily_reset_time'], APP_SETTING_DEFAULTS['daily_reset_timezone']

def _portfolio_now():
    _reset_time, timezone_name = _get_daily_reset_settings()
    return datetime.now(_daily_reset_zone(timezone_name))

def _portfolio_day(now=None):
    reset_time_value, _timezone_name = _get_daily_reset_settings()
    reset_at = _parse_daily_reset_time(reset_time_value)
    current = now or _portfolio_now()
    portfolio_date = current.date()
    if current.time() < reset_at:
        portfolio_date -= timedelta(days=1)
    return portfolio_date

def _portfolio_day_str(now=None):
    return _portfolio_day(now).strftime('%Y-%m-%d')

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
        'change_date': None,
        'quote_session': 'regular',
        'includes_extended_hours': False
    }

def _resolve_yahoo_jp_fund_code(symbol):
    normalized_symbol = (symbol or '').strip().upper()
    return YAHOO_JP_FUND_CODE_ALIASES.get(normalized_symbol, normalized_symbol)

def _format_yahoo_jp_quote_date(mm_dd_value):
    try:
        month, day = [int(part) for part in mm_dd_value.split('/')]
        today = _portfolio_now()
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
                'change_date': latest_data_at,
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
            result['change_date'] = _market_date_str(history.index[-1])
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
                    # When using intraday (including pre/post-market) quotes, compute today's
                    # change relative to the last available daily close so today's P/L
                    # reflects extended-hours movement.
                    try:
                        if not history.empty:
                            intraday_ts = intraday_history.index[-1]
                            # Find the most recent daily close that is before the intraday timestamp.
                            last_daily_close = None
                            for ts in reversed(history.index):
                                try:
                                    ts_dt = ts.to_pydatetime() if hasattr(ts, 'to_pydatetime') else ts
                                    intraday_dt = intraday_ts.to_pydatetime() if hasattr(intraday_ts, 'to_pydatetime') else intraday_ts
                                    if ts_dt.date() < intraday_dt.date():
                                        last_daily_close = float(history.loc[ts, price_col])
                                        break
                                except Exception:
                                    continue
                            # Fallbacks if no prior daily close found
                            if last_daily_close is None:
                                if len(history[price_col]) > 1:
                                    last_daily_close = float(history[price_col].iloc[-2])
                                else:
                                    last_daily_close = float(history[price_col].iloc[-1])
                            result['change_today'] = float(result['current_price'] - last_daily_close)
                            result['change_date'] = _market_date_str(intraday_ts)
                    except Exception:
                        pass
                    print(f"--- Using latest US quote for {symbol} from {result['latest_data_at']} ({result['quote_session']}): {result['current_price']} ---")

    except Exception as e:
        print(f"Could not fetch price for {symbol}: {e}")
    
    return result

def get_market_price(symbol, currency, instrument_type='stock'):
    override = _get_market_price_override(symbol, currency, instrument_type)
    if override:
        return override
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

def _market_date_str(timestamp):
    """Returns the calendar date represented by a daily market-data timestamp."""
    if timestamp is None:
        return None

    try:
        if hasattr(timestamp, 'to_pydatetime'):
            dt = timestamp.to_pydatetime()
        elif isinstance(timestamp, datetime):
            dt = timestamp
        else:
            return None
        return dt.date().strftime('%Y-%m-%d')
    except Exception:
        return None

def _is_daily_change_current(change_date, portfolio_day):
    if not change_date:
        return False
    if not isinstance(portfolio_day, str):
        portfolio_day = portfolio_day.strftime('%Y-%m-%d')
    return change_date == portfolio_day

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

def _parse_watchlist_form(form):
    errors = []
    values = {
        'symbol': form.get('symbol', '').strip().upper(),
        'name': form.get('name', '').strip(),
        'currency': form.get('currency', '').strip().upper(),
        'target_price': None,
        'stop_price': None,
        'notes': form.get('notes', '').strip(),
    }

    if not values['symbol']:
        errors.append("Symbol is required.")
    if not values['currency']:
        errors.append("Currency is required.")
    elif values['currency'] not in ['USD', 'JPY']:
        errors.append("Currency must be USD or JPY.")

    for field, label in [('target_price', 'Target price'), ('stop_price', 'Stop price')]:
        try:
            values[field] = _parse_optional_float(form.get(field))
            if values[field] is not None and values[field] < 0:
                errors.append(f"{label} cannot be negative.")
        except ValueError:
            errors.append(f"{label} must be a valid number.")

    return values, errors

def _fetch_watchlist():
    with sqlite3.connect(DATABASE) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute('''
            SELECT *
            FROM watchlist
            ORDER BY created_at DESC, id DESC
        ''').fetchall()

    portfolio_day = _portfolio_day()
    items = []
    for row in rows:
        item = dict(row)
        market_data = get_market_price(item['symbol'], item['currency'])
        item['current_price'] = market_data.get('current_price') if market_data.get('is_valid') else None
        item['change_today'] = market_data.get('change_today', 0.0) if market_data.get('is_valid') else 0.0
        if not _is_daily_change_current(market_data.get('change_date'), portfolio_day):
            item['change_today'] = 0.0
        item['latest_data_at'] = market_data.get('latest_data_at')
        item['quote_session'] = market_data.get('quote_session', 'regular')
        item['alert_state'] = None
        if item['current_price'] is not None:
            if item.get('target_price') is not None and item['current_price'] >= item['target_price']:
                item['alert_state'] = 'target'
            elif item.get('stop_price') is not None and item['current_price'] <= item['stop_price']:
                item['alert_state'] = 'stop'
        items.append(item)
    return items

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
    normalized['account_name'] = normalized.get('account_name') or 'Default'
    normalized['tax_status'] = normalized.get('tax_status') or 'Taxable'
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
        'account_name': _row_get(trade, 'account_name') or _row_get(trade, 'account_type') or 'Default',
        'tax_status': _row_get(trade, 'tax_status') or 'Taxable',
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
    config_options = get_config_options()
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
        'account_name': form.get('account_name', '').strip(),
        'tax_status': form.get('tax_status', '').strip(),
        'fx_rate': None,
        'fee_amount': None,
        'fee_currency': form.get('fee_currency', '').strip().upper() or None
    }

    required_fields = ['symbol', 'name', 'trade_type', 'currency', 'trade_date', 'broker', 'account_name', 'tax_status']
    for field in required_fields:
        if not values[field]:
            errors.append(f"{field.replace('_', ' ').title()} is required.")

    if values['trade_type'] and values['trade_type'] not in ['BUY', 'SELL']:
        errors.append("Trade type must be BUY or SELL.")
    if values['currency'] and values['currency'] not in ['USD', 'JPY']:
        errors.append("Currency must be USD or JPY.")
    if values['broker'] and values['broker'] not in config_options['broker']:
        errors.append("Broker is not recognized.")
    if values['account_name'] and values['account_name'] not in config_options['account_name']:
        errors.append("Account name is not recognized.")
    if values['tax_status'] and values['tax_status'] not in config_options['tax_status']:
        errors.append("Tax status is not recognized.")
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
    config_options = get_config_options()
    errors = []
    values = {
        'fund_code': form.get('fund_code', '').strip().upper(),
        'fund_name': form.get('fund_name', '').strip(),
        'transaction_type': form.get('transaction_type', '').strip().upper(),
        'transaction_detail': None,
        'account_type': None,
        'account_name': form.get('account_name', '').strip(),
        'tax_status': form.get('tax_status', '').strip(),
        'currency': 'JPY',
        'executed_units': None,
        'nav_per_10000': None,
        'trade_date': form.get('trade_date', '').strip(),
        'settlement_date': None,
        'settlement_amount': None,
        'broker': form.get('broker', '').strip(),
        'fx_rate': None,
    }

    required_fields = ['fund_code', 'fund_name', 'transaction_type', 'currency', 'trade_date', 'broker', 'account_name', 'tax_status']
    for field in required_fields:
        if not values[field]:
            errors.append(f"{field.replace('_', ' ').title()} is required.")

    if values['transaction_type'] and values['transaction_type'] not in ['BUY', 'SELL']:
        errors.append("Transaction type must be BUY or SELL.")
    if values['broker'] and values['broker'] not in config_options['broker']:
        errors.append("Broker is not recognized.")
    if values['account_name'] and values['account_name'] not in config_options['account_name']:
        errors.append("Account name is not recognized.")
    if values['tax_status'] and values['tax_status'] not in config_options['tax_status']:
        errors.append("Tax status is not recognized.")

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

def _parse_dividend_form(form):
    config_options = get_config_options()
    errors = []
    values = {
        'symbol': form.get('symbol', '').strip().upper(),
        'name': form.get('name', '').strip(),
        'payment_date': form.get('payment_date', '').strip(),
        'currency': form.get('currency', '').strip().upper(),
        'gross_amount': None,
        'tax_withheld': None,
        'foreign_tax_withheld': None,
        'japanese_income_tax_withheld': None,
        'japanese_local_tax_withheld': None,
        'deductible_interest': None,
        'quantity': None,
        'amount_per_share': None,
        'source_country': form.get('source_country', '').strip().upper(),
        'security_type': form.get('security_type', 'listed_stock').strip(),
        'tax_treatment': form.get('tax_treatment', 'undecided').strip(),
        'broker': form.get('broker', '').strip(),
        'account_name': form.get('account_name', '').strip(),
        'tax_status': form.get('tax_status', '').strip(),
        'fx_rate': None,
        'notes': form.get('notes', '').strip(),
    }

    required_fields = ['symbol', 'name', 'payment_date', 'currency', 'broker', 'account_name', 'tax_status']
    for field in required_fields:
        if not values[field]:
            errors.append(f"{field.replace('_', ' ').title()} is required.")

    if values['currency'] and values['currency'] not in ['USD', 'JPY']:
        errors.append("Currency must be USD or JPY.")
    if values['security_type'] not in ['listed_stock', 'etf', 'mutual_fund', 'other']:
        errors.append("Security type is not recognized.")
    if values['tax_treatment'] not in ['undecided', 'not_filed', 'aggregate', 'separate']:
        errors.append("Tax treatment is not recognized.")
    if values['broker'] and values['broker'] not in config_options['broker']:
        errors.append("Broker is not recognized.")
    if values['account_name'] and values['account_name'] not in config_options['account_name']:
        errors.append("Account name is not recognized.")
    if values['tax_status'] and values['tax_status'] not in config_options['tax_status']:
        errors.append("Tax status is not recognized.")

    try:
        datetime.strptime(values['payment_date'], '%Y-%m-%d')
    except ValueError:
        errors.append("Payment date must be in YYYY-MM-DD format.")

    for field, label in [
        ('gross_amount', 'Gross amount'),
        ('tax_withheld', 'Other tax withheld'),
        ('foreign_tax_withheld', 'Foreign tax withheld'),
        ('japanese_income_tax_withheld', 'Japanese income tax withheld'),
        ('japanese_local_tax_withheld', 'Japanese local tax withheld'),
        ('deductible_interest', 'Deductible interest'),
        ('quantity', 'Shares'),
        ('amount_per_share', 'Amount per share'),
        ('fx_rate', 'FX rate'),
    ]:
        try:
            values[field] = _parse_optional_float(form.get(field))
        except ValueError:
            errors.append(f"{label} must be a valid number.")

    if values['gross_amount'] is None:
        if values['quantity'] is not None and values['amount_per_share'] is not None:
            values['gross_amount'] = values['quantity'] * values['amount_per_share']
        else:
            errors.append("Gross amount is required unless shares and amount per share are provided.")

    for field, label in [
        ('gross_amount', 'Gross amount'),
        ('tax_withheld', 'Other tax withheld'),
        ('foreign_tax_withheld', 'Foreign tax withheld'),
        ('japanese_income_tax_withheld', 'Japanese income tax withheld'),
        ('japanese_local_tax_withheld', 'Japanese local tax withheld'),
        ('deductible_interest', 'Deductible interest'),
        ('quantity', 'Shares'),
        ('amount_per_share', 'Amount per share'),
    ]:
        if values[field] is not None and values[field] < 0:
            errors.append(f"{label} cannot be negative.")
    if values['fx_rate'] is not None and values['fx_rate'] <= 0:
        errors.append("FX rate must be positive.")
    total_tax = sum(values[field] or 0 for field in [
        'tax_withheld',
        'foreign_tax_withheld',
        'japanese_income_tax_withheld',
        'japanese_local_tax_withheld',
    ])
    if values['gross_amount'] is not None and total_tax > values['gross_amount']:
        errors.append("Total tax withheld cannot be greater than gross amount.")

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

def _seed_config_options(conn):
    for category, values in CONFIG_OPTION_DEFAULTS.items():
        for sort_order, value in enumerate(values):
            conn.execute(
                """
                INSERT OR IGNORE INTO config_options (category, value, sort_order)
                VALUES (?, ?, ?)
                """,
                (category, value, sort_order)
            )

def get_config_options():
    """Loads user-manageable dropdown values from the database."""
    with sqlite3.connect(DATABASE) as conn:
        conn.row_factory = sqlite3.Row
        _seed_config_options(conn)
        rows = conn.execute(
            """
            SELECT id, category, value, sort_order
            FROM config_options
            ORDER BY category, sort_order, value
            """
        ).fetchall()

    options = {category: [] for category in CONFIG_OPTION_DEFAULTS}
    option_rows = {category: [] for category in CONFIG_OPTION_DEFAULTS}
    for row in rows:
        category = row['category']
        if category not in options:
            continue
        option = dict(row)
        options[category].append(option['value'])
        option_rows[category].append(option)

    for category, defaults in CONFIG_OPTION_DEFAULTS.items():
        if not options[category]:
            options[category] = defaults[:]

    options['_rows'] = option_rows
    return options

def _get_api_filters(args):
    filters = {
        'broker': args.get('broker', 'all'),
        'currency': args.get('currency', 'all'),
        'account_name': args.get('account_name', 'all'),
        'tax_status': args.get('tax_status', 'all'),
    }
    effective_filters = {
        key: value if value != 'all' else None
        for key, value in filters.items()
    }
    return filters, effective_filters

def _get_exchange_context():
    exchange_data = get_exchange_rate()
    using_fallback = exchange_data is None
    if isinstance(exchange_data, dict):
        exchange_rate = exchange_data['rate']
        latest_data_at = exchange_data.get('latest_data_at')
        latest_data_ago = _format_relative_time(exchange_data.get('latest_data_sort'))
    else:
        exchange_rate = exchange_data or 150.0
        latest_data_at = None
        latest_data_ago = None
    return {
        'exchange_data': exchange_data,
        'exchange_rate': exchange_rate,
        'using_fallback': using_fallback,
        'latest_data_at': latest_data_at,
        'latest_data_ago': latest_data_ago,
    }

def _get_history_data(days=365):
    days = _parse_history_days(days)
    with sqlite3.connect(DATABASE) as conn:
        conn.row_factory = sqlite3.Row
        history_rows = conn.execute(
            "SELECT date, value_jpy, unrealized_pnl_jpy FROM portfolio_history ORDER BY date DESC LIMIT ?",
            (days,)
        ).fetchall()
    return [dict(row) for row in reversed(history_rows)]

def _parse_history_days(days=365):
    try:
        days = int(days)
    except (TypeError, ValueError):
        days = 365
    return max(1, min(days, 3650))

def _get_market_price_override(symbol, currency, instrument_type='stock'):
    with sqlite3.connect(DATABASE) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """
            SELECT symbol, currency, instrument_type, current_price, change_today, latest_data_at
            FROM market_price_overrides
            WHERE symbol = ? AND currency = ? AND instrument_type = ?
            """,
            (symbol, currency, instrument_type)
        ).fetchone()
    if row is None:
        return None

    current_price = row['current_price']
    change_today = row['change_today'] or 0.0
    latest_data_at = row['latest_data_at'] or _portfolio_day_str()
    try:
        latest_data_sort = datetime.strptime(latest_data_at, '%Y-%m-%d').timestamp()
    except ValueError:
        latest_data_sort = _portfolio_now().timestamp()
    return {
        'current_price': current_price,
        'change_today': change_today,
        'sparkline_data': [current_price] * 7,
        'is_valid': True,
        'latest_data_at': latest_data_at,
        'latest_data_sort': latest_data_sort,
        'change_date': latest_data_at[:10],
        'quote_session': 'manual override',
        'includes_extended_hours': False,
    }

def _fetch_market_price_overrides():
    with sqlite3.connect(DATABASE) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT symbol, currency, instrument_type, current_price, change_today,
                   latest_data_at, notes, updated_at
            FROM market_price_overrides
            ORDER BY updated_at DESC, symbol ASC
            """
        ).fetchall()
    return [dict(row) for row in rows]

def _parse_price_override_form(form):
    values = {
        'symbol': form.get('symbol', '').strip().upper(),
        'currency': form.get('currency', '').strip().upper(),
        'instrument_type': form.get('instrument_type', 'stock').strip() or 'stock',
        'current_price': None,
        'change_today': None,
        'latest_data_at': form.get('latest_data_at', '').strip() or _portfolio_day_str(),
        'notes': form.get('notes', '').strip(),
    }
    errors = []
    if not values['symbol']:
        errors.append('Symbol is required.')
    if values['currency'] not in ['JPY', 'USD']:
        errors.append('Currency must be JPY or USD.')
    if values['instrument_type'] not in ['stock', 'mutual_fund']:
        errors.append('Instrument type must be stock or mutual_fund.')
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', values['latest_data_at']):
        errors.append('Quote date must be YYYY-MM-DD.')
    try:
        values['current_price'] = float(form.get('current_price', ''))
        if values['current_price'] < 0:
            errors.append('Current price must be zero or greater.')
    except ValueError:
        errors.append('Current price is required.')
    try:
        values['change_today'] = _parse_optional_float_field(form, 'change_today')
    except ValueError:
        errors.append('Today change must be numeric or blank.')
    return values, errors

def _fetch_corporate_actions():
    with sqlite3.connect(DATABASE) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT id, symbol, currency, action_type, effective_date, ratio,
                   affected_trades, price_override, notes, applied_at
            FROM corporate_actions
            ORDER BY effective_date DESC, id DESC
            """
        ).fetchall()
    return [dict(row) for row in rows]

def _parse_stock_split_form(form):
    values = {
        'symbol': form.get('symbol', '').strip().upper(),
        'currency': form.get('currency', '').strip().upper() or 'JPY',
        'effective_date': form.get('effective_date', '').strip(),
        'ratio': None,
        'price_override': None,
        'notes': form.get('notes', '').strip(),
    }
    errors = []
    if not values['symbol']:
        errors.append('Symbol is required.')
    if values['currency'] not in ['JPY', 'USD']:
        errors.append('Currency must be JPY or USD.')
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', values['effective_date']):
        errors.append('Effective date must be YYYY-MM-DD.')
    try:
        values['ratio'] = float(form.get('ratio', ''))
        if values['ratio'] <= 0:
            errors.append('Split multiplier must be greater than zero.')
    except ValueError:
        errors.append('Split multiplier is required.')
    try:
        values['price_override'] = _parse_optional_float_field(form, 'price_override')
        if values['price_override'] is not None and values['price_override'] < 0:
            errors.append('Manual price override must be zero or greater.')
    except ValueError:
        errors.append('Manual price override must be numeric or blank.')
    return values, errors

def _preview_stock_split(values):
    with sqlite3.connect(DATABASE) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT id, trade_date, trade_type, broker, account_name, tax_status,
                   quantity, price, currency
            FROM trades
            WHERE symbol = ? AND currency = ? AND trade_date < ?
            ORDER BY trade_date ASC, id ASC
            """,
            (values['symbol'], values['currency'], values['effective_date'])
        ).fetchall()

    ratio = values['ratio']
    return [
        {
            **dict(row),
            'new_quantity': row['quantity'] * ratio,
            'new_price': row['price'] / ratio,
        }
        for row in rows
    ]

def _stock_split_already_applied(values):
    with sqlite3.connect(DATABASE) as conn:
        row = conn.execute(
            """
            SELECT id FROM corporate_actions
            WHERE action_type = 'stock_split'
              AND symbol = ?
              AND currency = ?
              AND effective_date = ?
              AND ratio = ?
            """,
            (values['symbol'], values['currency'], values['effective_date'], values['ratio'])
        ).fetchone()
    return row is not None

def _apply_stock_split(values):
    preview_rows = _preview_stock_split(values)
    if not preview_rows:
        raise ValueError('No matching pre-effective-date stock trades were found.')
    if _stock_split_already_applied(values):
        raise ValueError('This stock split has already been recorded.')

    applied_at = _portfolio_now().strftime('%Y-%m-%d %H:%M:%S')
    with sqlite3.connect(DATABASE) as conn:
        for row in preview_rows:
            conn.execute(
                """
                UPDATE trades
                SET quantity = ?, price = ?
                WHERE id = ?
                """,
                (row['new_quantity'], row['new_price'], row['id'])
            )
        if values['price_override'] is not None:
            conn.execute(
                """
                INSERT INTO market_price_overrides (
                    symbol, currency, instrument_type, current_price, change_today,
                    latest_data_at, notes, updated_at
                )
                VALUES (?, ?, 'stock', ?, 0, ?, ?, ?)
                ON CONFLICT(symbol, currency, instrument_type) DO UPDATE SET
                    current_price = excluded.current_price,
                    change_today = excluded.change_today,
                    latest_data_at = excluded.latest_data_at,
                    notes = excluded.notes,
                    updated_at = excluded.updated_at
                """,
                (
                    values['symbol'],
                    values['currency'],
                    values['price_override'],
                    values['effective_date'],
                    values['notes'] or 'Corporate action split override',
                    applied_at
                )
            )
        conn.execute(
            """
            INSERT INTO corporate_actions (
                symbol, currency, action_type, effective_date, ratio,
                affected_trades, price_override, notes, applied_at
            )
            VALUES (?, ?, 'stock_split', ?, ?, ?, ?, ?, ?)
            """,
            (
                values['symbol'],
                values['currency'],
                values['effective_date'],
                values['ratio'],
                len(preview_rows),
                values['price_override'],
                values['notes'],
                applied_at
            )
        )
    cache.clear()
    return len(preview_rows)

def _fetch_portfolio_history_rows(limit=None):
    with sqlite3.connect(DATABASE) as conn:
        conn.row_factory = sqlite3.Row
        if limit is None:
            rows = conn.execute(
                """
                SELECT date, value_usd, value_jpy, unrealized_pnl_usd, unrealized_pnl_jpy
                FROM portfolio_history
                ORDER BY date DESC
                """
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT date, value_usd, value_jpy, unrealized_pnl_usd, unrealized_pnl_jpy
                FROM portfolio_history
                ORDER BY date DESC
                LIMIT ?
                """,
                (limit,)
            ).fetchall()
    return [dict(row) for row in rows]

def _fetch_cached_fx_rates(limit=100):
    """Fetch recently cached FX rates from database."""
    with sqlite3.connect(DATABASE) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT date, tts, ttm, ttb, fetched_at
            FROM fx_rates
            ORDER BY date DESC
            LIMIT ?
            """,
            (limit,)
        ).fetchall()
    return [dict(row) for row in rows]


def _parse_optional_float_field(form, name):
    value = form.get(name, '').strip()
    if value == '':
        return None
    return float(value)

def _recalculate_today_history_snapshot():
    exchange_data = get_exchange_rate()
    if exchange_data is None:
        raise RuntimeError('USD/JPY market data is unavailable.')

    trades = _fetch_normalized_trades()
    summary = _calculate_portfolio_summary(trades, exchange_data['rate'])
    if not summary['market_data_complete']:
        raise RuntimeError('One or more live quotes are unavailable.')

    _ensure_history_updated(summary)
    return summary

def _get_available_tax_years():
    with sqlite3.connect(DATABASE) as conn:
        rows = conn.execute(
            """
            SELECT year FROM (
                SELECT DISTINCT SUBSTR(trade_date, 1, 4) as year FROM trades
                UNION
                SELECT DISTINCT SUBSTR(trade_date, 1, 4) as year FROM mutual_fund_trades
            ) ORDER BY year DESC
            """
        ).fetchall()
    return [row[0] for row in rows if row[0]]

def _summary_totals(summary):
    return {
        'value_usd': summary['total_value_usd'],
        'value_jpy': summary['total_value_jpy'],
        'today_pnl_usd': summary['total_today_pnl_usd'],
        'today_pnl_jpy': summary['total_today_pnl_jpy'],
        'realized_pnl_usd': summary['total_realized_pnl_usd'],
        'realized_pnl_jpy': summary['total_realized_pnl_jpy'],
        'unrealized_pnl_usd': summary['total_unrealized_pnl_usd'],
        'unrealized_pnl_jpy': summary['total_unrealized_pnl_jpy'],
        'unrealized_price_pnl_jpy': summary['total_unrealized_price_pnl_jpy'],
        'unrealized_fx_pnl_jpy': summary['total_unrealized_fx_pnl_jpy'],
        'realized_price_pnl_jpy': summary['total_realized_price_pnl_jpy'],
        'realized_fx_pnl_jpy': summary['total_realized_fx_pnl_jpy'],
        'total_pnl_jpy': summary['total_pnl_jpy'],
        'total_price_pnl_jpy': summary['total_price_pnl_jpy'],
        'total_fx_pnl_jpy': summary['total_fx_pnl_jpy'],
    }

def _api_meta(summary, filters, exchange_context):
    return {
        'generated_at': _portfolio_now().strftime('%Y-%m-%d %H:%M:%S'),
        'portfolio_day': _portfolio_day_str(),
        'filters': filters,
        'exchange_rate': exchange_context['exchange_rate'],
        'fx_latest_data_at': exchange_context['latest_data_at'],
        'fx_latest_data_ago': exchange_context['latest_data_ago'],
        'fx_using_fallback': exchange_context['using_fallback'],
        'market_data_complete': summary['market_data_complete'],
        'oldest_market_data_at': summary['oldest_market_data_at'],
        'latest_market_data_at': summary['latest_market_data_at'],
        'oldest_market_data_ago': summary['oldest_market_data_ago'],
        'latest_market_data_ago': summary['latest_market_data_ago'],
    }

def _build_portfolio_api_payload(include_history=False, history_days=365):
    filters, effective_filters = _get_api_filters(request.args)
    exchange_context = _get_exchange_context()
    trades = _fetch_normalized_trades()
    summary = _calculate_portfolio_summary(
        trades,
        exchange_context['exchange_rate'],
        effective_filters['broker'],
        effective_filters['currency'],
        effective_filters['account_name'],
        effective_filters['tax_status']
    )
    payload = {
        **summary,
        'meta': _api_meta(summary, filters, exchange_context),
        'filters': filters,
        'totals': _summary_totals(summary),
        'holdings': summary['stocks'],
    }
    if include_history:
        payload['history'] = _get_history_data(history_days)
    return payload, summary, trades, exchange_context, filters

def _json_to_form_payload(payload):
    """Converts JSON request bodies to the string-like shape used by form validators."""
    payload = payload or {}
    return {
        key: '' if value is None else str(value)
        for key, value in payload.items()
    }

def _insert_import_profile(profile):
    with sqlite3.connect(DATABASE) as conn:
        cursor = conn.execute(
            """
            INSERT INTO import_profiles (name, instrument_type, encoding, header_row, row_filters, mappings, defaults)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                profile['name'],
                profile['instrument_type'],
                profile.get('encoding') or 'utf-8-sig',
                int(profile.get('header_row') or 1),
                json.dumps(profile.get('row_filters') or [], ensure_ascii=False),
                json.dumps(profile.get('mappings') or {}, ensure_ascii=False),
                json.dumps(profile.get('defaults') or {}, ensure_ascii=False),
            )
        )
        return cursor.lastrowid

def _profile_from_row(row):
    profile = dict(row)
    for key, fallback in [('row_filters', []), ('mappings', {}), ('defaults', {})]:
        try:
            profile[key] = json.loads(profile.get(key) or '')
        except (TypeError, json.JSONDecodeError):
            profile[key] = fallback
    return profile

def _fetch_import_profiles():
    with sqlite3.connect(DATABASE) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute('SELECT * FROM import_profiles ORDER BY name').fetchall()
    return [_profile_from_row(row) for row in rows]

def _fetch_import_profile(profile_id):
    with sqlite3.connect(DATABASE) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute('SELECT * FROM import_profiles WHERE id = ?', (profile_id,)).fetchone()
    return _profile_from_row(row) if row else None

def _parse_import_profile_form(form):
    errors = []
    profile = {
        'name': form.get('name', '').strip(),
        'instrument_type': form.get('instrument_type', '').strip(),
        'encoding': form.get('encoding', 'utf-8-sig').strip() or 'utf-8-sig',
        'header_row': form.get('header_row', '1').strip() or '1',
        'row_filters': [],
        'mappings': {},
        'defaults': {},
    }
    if not profile['name']:
        errors.append("Profile name is required.")
    if profile['instrument_type'] not in ['stock', 'mutual_fund', 'dividend']:
        errors.append("Instrument type must be stock, mutual_fund, or dividend.")
    try:
        profile['header_row'] = int(profile['header_row'])
        if profile['header_row'] < 1:
            errors.append("Header row must be 1 or greater.")
    except ValueError:
        errors.append("Header row must be a number.")
        profile['header_row'] = 1
    for field, fallback in [('row_filters', []), ('mappings', {}), ('defaults', {})]:
        raw_value = form.get(field, '').strip()
        if not raw_value:
            profile[field] = fallback
            continue
        try:
            profile[field] = json.loads(raw_value)
        except json.JSONDecodeError as exc:
            errors.append(f"{field.replace('_', ' ').title()} JSON is invalid: {exc.msg}.")
    if not isinstance(profile['row_filters'], list):
        errors.append("Row filters must be a JSON array.")
    if not isinstance(profile['mappings'], dict):
        errors.append("Mappings must be a JSON object.")
    if not isinstance(profile['defaults'], dict):
        errors.append("Defaults must be a JSON object.")
    return profile, errors

def _clean_import_number(value):
    value = re.sub(r'\([^)]*\)', '', str(value or '')).strip().replace(',', '')
    if value in ['', '-']:
        return None
    return float(value)

def _apply_import_transform(value, transform):
    if isinstance(value, list):
        values = [_clean_import_number(item) or 0 for item in value]
        if transform == 'abs_sum':
            return abs(sum(values))
        return sum(values)
    value = str(value or '').strip()
    if transform == 'strip':
        return value
    if transform == 'number':
        return _clean_import_number(value)
    if transform == 'number_zero_none':
        number = _clean_import_number(value)
        return None if number == 0 else number
    if transform == 'abs_number':
        number = _clean_import_number(value)
        return abs(number) if number is not None else None
    if transform == 'currency_from_value':
        upper_value = value.upper()
        if '(USD)' in upper_value or 'USD' in upper_value:
            return 'USD'
        if '(JPY)' in upper_value or 'JPY' in upper_value:
            return 'JPY'
        return 'JPY'
    if transform == 'date_slash':
        return datetime.strptime(value, '%Y/%m/%d').strftime('%Y-%m-%d') if value else ''
    if transform == 'monex_jp_symbol':
        symbol = value.strip()
        if len(symbol) == 5 and symbol.endswith('0') and symbol.isdigit():
            symbol = symbol[:-1]
        return symbol
    return value

def _row_matches_import_profile(row, profile):
    for row_filter in profile.get('row_filters') or []:
        value = str(row.get(row_filter.get('column'), '') or '').strip()
        if 'equals' in row_filter and value != str(row_filter['equals']).strip():
            return False
        if 'not_equals' in row_filter and value == str(row_filter['not_equals']).strip():
            return False
        if 'contains' in row_filter and str(row_filter['contains']) not in value:
            return False
        if 'in' in row_filter and value not in [str(item).strip() for item in row_filter['in']]:
            return False
        if 'not_in' in row_filter and value in [str(item).strip() for item in row_filter['not_in']]:
            return False
    return True

def _map_import_row(row, profile):
    values = dict(profile.get('defaults') or {})
    for target_field, rule in (profile.get('mappings') or {}).items():
        if isinstance(rule, str):
            raw_value = row.get(rule, '')
            transform = None
            value_map = None
        else:
            columns = rule.get('columns')
            raw_value = [row.get(column, '') for column in columns] if columns else row.get(rule.get('column'), '')
            transform = rule.get('transform')
            value_map = rule.get('map')
        value = _apply_import_transform(raw_value, transform)
        if value_map is not None:
            value = value_map.get(str(value).strip(), value)
        values[target_field] = value
    return values

def _read_csv_rows_for_import(file_storage, profile):
    data = file_storage.stream.read()
    file_storage.stream.seek(0)
    text = data.decode(profile.get('encoding') or 'utf-8-sig')
    rows = list(csv.reader(io.StringIO(text)))
    header_index = int(profile.get('header_row') or 1) - 1
    if header_index >= len(rows):
        raise ValueError("Header row is beyond the end of the file.")
    headers = rows[header_index]
    return [
        dict(zip(headers, row))
        for row in rows[header_index + 1:]
        if any(str(cell).strip() for cell in row)
    ]

def _stock_trade_exists(values):
    with sqlite3.connect(DATABASE) as conn:
        return conn.execute(
            """
            SELECT 1
            FROM trades
            WHERE symbol = ?
              AND name = ?
              AND trade_type = ?
              AND quantity IS ?
              AND price IS ?
              AND currency = ?
              AND trade_date = ?
              AND broker = ?
              AND account_name = ?
              AND tax_status = ?
              AND fx_rate IS ?
              AND fee_amount IS ?
              AND fee_currency IS ?
            LIMIT 1
            """,
            (
                values['symbol'],
                values['name'],
                values['trade_type'],
                values['quantity'],
                values['price'],
                values['currency'],
                values['trade_date'],
                values['broker'],
                values['account_name'],
                values['tax_status'],
                values['fx_rate'],
                values['fee_amount'],
                values['fee_currency'],
            )
        ).fetchone() is not None

def _mutual_fund_trade_exists(values):
    with sqlite3.connect(DATABASE) as conn:
        return conn.execute(
            """
            SELECT 1
            FROM mutual_fund_trades
            WHERE fund_code = ?
              AND fund_name = ?
              AND transaction_type = ?
              AND transaction_detail IS ?
              AND account_type IS ?
              AND account_name = ?
              AND tax_status = ?
              AND currency = ?
              AND executed_units IS ?
              AND nav_per_10000 IS ?
              AND trade_date = ?
              AND settlement_date IS ?
              AND settlement_amount IS ?
              AND broker = ?
              AND fx_rate IS ?
            LIMIT 1
            """,
            (
                values['fund_code'],
                values['fund_name'],
                values['transaction_type'],
                values['transaction_detail'],
                values['account_type'],
                values['account_name'],
                values['tax_status'],
                values['currency'],
                values['executed_units'],
                values['nav_per_10000'],
                values['trade_date'],
                values['settlement_date'],
                values['settlement_amount'],
                values['broker'],
                values['fx_rate'],
            )
        ).fetchone() is not None

def _dividend_exists(values):
    with sqlite3.connect(DATABASE) as conn:
        return conn.execute(
            """
            SELECT 1
            FROM dividends
            WHERE symbol = ?
              AND name = ?
              AND payment_date = ?
              AND currency = ?
              AND gross_amount IS ?
              AND tax_withheld IS ?
              AND foreign_tax_withheld IS ?
              AND japanese_income_tax_withheld IS ?
              AND japanese_local_tax_withheld IS ?
              AND deductible_interest IS ?
              AND quantity IS ?
              AND amount_per_share IS ?
              AND source_country = ?
              AND security_type = ?
              AND tax_treatment = ?
              AND broker = ?
              AND account_name = ?
              AND tax_status = ?
              AND fx_rate IS ?
              AND notes = ?
            LIMIT 1
            """,
            (
                values['symbol'],
                values['name'],
                values['payment_date'],
                values['currency'],
                values['gross_amount'],
                values['tax_withheld'],
                values['foreign_tax_withheld'],
                values['japanese_income_tax_withheld'],
                values['japanese_local_tax_withheld'],
                values['deductible_interest'],
                values['quantity'],
                values['amount_per_share'],
                values['source_country'],
                values['security_type'],
                values['tax_treatment'],
                values['broker'],
                values['account_name'],
                values['tax_status'],
                values['fx_rate'],
                values['notes'],
            )
        ).fetchone() is not None

def _format_import_duplicate(profile, row_number, values):
    instrument_type = profile.get('instrument_type')
    if instrument_type == 'stock':
        return (
            f"Row {row_number}: stock {values['trade_type']} {values['symbol']} "
            f"{values['quantity']:g} @ {values['price']:g} {values['currency']} on {values['trade_date']} "
            f"({values['broker']} / {values['account_name']} / {values['tax_status']})"
        )
    if instrument_type == 'mutual_fund':
        return (
            f"Row {row_number}: mutual fund {values['transaction_type']} {values['fund_code']} "
            f"{values['executed_units']:g} units @ {values['nav_per_10000']:g} JPY on {values['trade_date']} "
            f"({values['broker']} / {values['account_name']} / {values['tax_status']})"
        )
    if instrument_type == 'dividend':
        return (
            f"Row {row_number}: dividend {values['symbol']} {values['gross_amount']:g} {values['currency']} "
            f"paid {values['payment_date']} ({values['broker']} / {values['account_name']} / {values['tax_status']})"
        )
    return f"Row {row_number}: duplicate record"

def _import_rows_with_profile(file_storage, profile):
    rows = _read_csv_rows_for_import(file_storage, profile)
    imported = 0
    skipped = 0
    duplicates = 0
    duplicate_details = []
    errors = []
    for index, row in enumerate(rows, start=int(profile.get('header_row') or 1) + 1):
        if not _row_matches_import_profile(row, profile):
            skipped += 1
            continue
        values = _map_import_row(row, profile)
        payload = _json_to_form_payload(values)
        try:
            if profile['instrument_type'] == 'stock':
                parsed, row_errors = _parse_trade_form(payload)
                if row_errors:
                    errors.extend(f"Row {index}: {error}" for error in row_errors)
                    continue
                if _stock_trade_exists(parsed):
                    duplicates += 1
                    duplicate_details.append(_format_import_duplicate(profile, index, parsed))
                    continue
                _insert_stock_trade(parsed)
            elif profile['instrument_type'] == 'mutual_fund':
                parsed, row_errors = _parse_mutual_fund_trade_form(payload)
                if row_errors:
                    errors.extend(f"Row {index}: {error}" for error in row_errors)
                    continue
                if _mutual_fund_trade_exists(parsed):
                    duplicates += 1
                    duplicate_details.append(_format_import_duplicate(profile, index, parsed))
                    continue
                _insert_mutual_fund_trade(parsed)
            elif profile['instrument_type'] == 'dividend':
                parsed, row_errors = _parse_dividend_form(payload)
                if row_errors:
                    errors.extend(f"Row {index}: {error}" for error in row_errors)
                    continue
                if _dividend_exists(parsed):
                    duplicates += 1
                    duplicate_details.append(_format_import_duplicate(profile, index, parsed))
                    continue
                _insert_dividend(parsed)
            imported += 1
        except Exception as exc:
            errors.append(f"Row {index}: {exc}")
    return {
        'imported': imported,
        'skipped': skipped,
        'duplicates': duplicates,
        'duplicate_details': duplicate_details,
        'errors': errors,
    }

def _insert_stock_trade(values):
    with sqlite3.connect(DATABASE) as conn:
        cursor = conn.execute(
            'INSERT INTO trades (symbol, name, trade_type, quantity, price, currency, trade_date, broker, account_name, tax_status, fx_rate, fee_amount, fee_currency) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (
                values['symbol'],
                values['name'],
                values['trade_type'],
                values['quantity'],
                values['price'],
                values['currency'],
                values['trade_date'],
                values['broker'],
                values['account_name'],
                values['tax_status'],
                values['fx_rate'],
                values['fee_amount'],
                values['fee_currency']
            )
        )
        return cursor.lastrowid

def _insert_mutual_fund_trade(values):
    with sqlite3.connect(DATABASE) as conn:
        cursor = conn.execute(
            """
            INSERT INTO mutual_fund_trades (
                fund_code, fund_name, transaction_type, transaction_detail,
                account_type, account_name, tax_status, currency, executed_units, nav_per_10000,
                trade_date, settlement_date, settlement_amount, broker, fx_rate
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                values['fund_code'],
                values['fund_name'],
                values['transaction_type'],
                values['transaction_detail'],
                values['account_type'],
                values['account_name'],
                values['tax_status'],
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
        return cursor.lastrowid

def _insert_dividend(values):
    with sqlite3.connect(DATABASE) as conn:
        cursor = conn.execute(
            """
            INSERT INTO dividends (
                symbol, name, payment_date, currency, gross_amount, tax_withheld,
                foreign_tax_withheld, japanese_income_tax_withheld, japanese_local_tax_withheld,
                deductible_interest, quantity, amount_per_share, source_country, security_type,
                tax_treatment, broker, account_name, tax_status, fx_rate, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                values['symbol'],
                values['name'],
                values['payment_date'],
                values['currency'],
                values['gross_amount'],
                values['tax_withheld'],
                values['foreign_tax_withheld'],
                values['japanese_income_tax_withheld'],
                values['japanese_local_tax_withheld'],
                values['deductible_interest'],
                values['quantity'],
                values['amount_per_share'],
                values['source_country'],
                values['security_type'],
                values['tax_treatment'],
                values['broker'],
                values['account_name'],
                values['tax_status'],
                values['fx_rate'],
                values['notes']
            )
        )
        return cursor.lastrowid

def _update_dividend(dividend_id, values):
    with sqlite3.connect(DATABASE) as conn:
        cursor = conn.execute(
            """
            UPDATE dividends
            SET symbol = ?,
                name = ?,
                payment_date = ?,
                currency = ?,
                gross_amount = ?,
                tax_withheld = ?,
                foreign_tax_withheld = ?,
                japanese_income_tax_withheld = ?,
                japanese_local_tax_withheld = ?,
                deductible_interest = ?,
                quantity = ?,
                amount_per_share = ?,
                source_country = ?,
                security_type = ?,
                tax_treatment = ?,
                broker = ?,
                account_name = ?,
                tax_status = ?,
                fx_rate = ?,
                notes = ?
            WHERE id = ?
            """,
            (
                values['symbol'],
                values['name'],
                values['payment_date'],
                values['currency'],
                values['gross_amount'],
                values['tax_withheld'],
                values['foreign_tax_withheld'],
                values['japanese_income_tax_withheld'],
                values['japanese_local_tax_withheld'],
                values['deductible_interest'],
                values['quantity'],
                values['amount_per_share'],
                values['source_country'],
                values['security_type'],
                values['tax_treatment'],
                values['broker'],
                values['account_name'],
                values['tax_status'],
                values['fx_rate'],
                values['notes'],
                dividend_id
            )
        )
        return cursor.rowcount

def _stock_trade_api_row(row):
    trade = dict(row)
    trade['instrument_type'] = 'stock'
    trade['days_since_purchase'] = None
    trade['sell_allowed'] = None
    if trade.get('trade_type') == 'BUY':
        try:
            purchase_date = datetime.strptime(trade['trade_date'], '%Y-%m-%d').date()
            trade['days_since_purchase'] = max(0, (_portfolio_now().date() - purchase_date).days)
            trade['sell_allowed'] = trade['days_since_purchase'] >= 30
        except (TypeError, ValueError):
            pass
    return trade

def _mutual_fund_trade_api_row(row):
    trade = dict(row)
    trade['instrument_type'] = 'mutual_fund'
    trade['symbol'] = trade['fund_code']
    trade['name'] = trade['fund_name']
    trade['trade_type'] = trade['transaction_type']
    trade['quantity'] = trade['executed_units']
    trade['price'] = trade['nav_per_10000']
    return trade

def _fetch_stock_trade_row(trade_id):
    with sqlite3.connect(DATABASE) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute('SELECT * FROM trades WHERE id = ?', (trade_id,)).fetchone()

def _fetch_mutual_fund_trade_row(trade_id):
    with sqlite3.connect(DATABASE) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute('SELECT * FROM mutual_fund_trades WHERE id = ?', (trade_id,)).fetchone()

def _dividend_api_row(row):
    dividend = dict(row)
    tax_withheld = sum(dividend.get(field) or 0 for field in [
        'tax_withheld',
        'foreign_tax_withheld',
        'japanese_income_tax_withheld',
        'japanese_local_tax_withheld',
    ])
    dividend['total_tax_withheld'] = tax_withheld
    dividend['dividend_income_amount'] = (dividend.get('gross_amount') or 0) - (dividend.get('deductible_interest') or 0)
    dividend['net_amount'] = (dividend.get('gross_amount') or 0) - tax_withheld
    if dividend.get('currency') == 'JPY':
        dividend['gross_amount_jpy'] = dividend.get('gross_amount') or 0
        dividend['tax_withheld_jpy'] = tax_withheld
        dividend['foreign_tax_withheld_jpy'] = dividend.get('foreign_tax_withheld') or 0
        dividend['japanese_income_tax_withheld_jpy'] = dividend.get('japanese_income_tax_withheld') or 0
        dividend['japanese_local_tax_withheld_jpy'] = dividend.get('japanese_local_tax_withheld') or 0
        dividend['dividend_income_amount_jpy'] = dividend['dividend_income_amount']
        dividend['net_amount_jpy'] = dividend['net_amount']
    elif dividend.get('fx_rate'):
        dividend['gross_amount_jpy'] = (dividend.get('gross_amount') or 0) * dividend['fx_rate']
        dividend['tax_withheld_jpy'] = tax_withheld * dividend['fx_rate']
        dividend['foreign_tax_withheld_jpy'] = (dividend.get('foreign_tax_withheld') or 0) * dividend['fx_rate']
        dividend['japanese_income_tax_withheld_jpy'] = (dividend.get('japanese_income_tax_withheld') or 0) * dividend['fx_rate']
        dividend['japanese_local_tax_withheld_jpy'] = (dividend.get('japanese_local_tax_withheld') or 0) * dividend['fx_rate']
        dividend['dividend_income_amount_jpy'] = dividend['dividend_income_amount'] * dividend['fx_rate']
        dividend['net_amount_jpy'] = dividend['net_amount'] * dividend['fx_rate']
    else:
        dividend['gross_amount_jpy'] = None
        dividend['tax_withheld_jpy'] = None
        dividend['foreign_tax_withheld_jpy'] = None
        dividend['japanese_income_tax_withheld_jpy'] = None
        dividend['japanese_local_tax_withheld_jpy'] = None
        dividend['dividend_income_amount_jpy'] = None
        dividend['net_amount_jpy'] = None
    return dividend

def _fetch_dividend_row(dividend_id):
    with sqlite3.connect(DATABASE) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute('SELECT * FROM dividends WHERE id = ?', (dividend_id,)).fetchone()

def _get_dividend_filter_options():
    config_options = get_config_options()
    with sqlite3.connect(DATABASE) as conn:
        years = [
            row[0]
            for row in conn.execute(
                "SELECT DISTINCT SUBSTR(payment_date, 1, 4) FROM dividends WHERE payment_date IS NOT NULL AND payment_date != '' ORDER BY 1 DESC"
            ).fetchall()
            if row[0]
        ]
    return {
        'brokers': config_options['broker'],
        'account_names': config_options['account_name'],
        'tax_statuses': config_options['tax_status'],
        'years': years,
    }

def _dividend_filter_query(filters):
    where = []
    params = []
    if filters.get('year') and filters['year'] != 'all':
        where.append("SUBSTR(payment_date, 1, 4) = ?")
        params.append(filters['year'])
    for query_key, column_name in [
        ('broker', 'broker'),
        ('account_name', 'account_name'),
        ('tax_status', 'tax_status'),
        ('tax_treatment', 'tax_treatment'),
    ]:
        value = filters.get(query_key)
        if value and value != 'all':
            where.append(f"{column_name} = ?")
            params.append(value)
    return where, params

def _calculate_dividend_income_summary():
    portfolio_day = _portfolio_day()
    current_year = str(portfolio_day.year)
    with sqlite3.connect(DATABASE) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute('SELECT * FROM dividends ORDER BY payment_date DESC, id DESC').fetchall()

    dividends = [_dividend_api_row(row) for row in rows]

    def summarize(items, monthly_divisor=None):
        known_income = [item['dividend_income_amount_jpy'] for item in items if item['dividend_income_amount_jpy'] is not None]
        known_net = [item['net_amount_jpy'] for item in items if item['net_amount_jpy'] is not None]
        income_jpy = sum(known_income)
        net_jpy = sum(known_net)
        monthly_divisor = monthly_divisor or 0
        return {
            'income_jpy': income_jpy,
            'net_jpy': net_jpy,
            'avg_monthly_income_jpy': income_jpy / monthly_divisor if monthly_divisor else 0,
            'avg_monthly_net_jpy': net_jpy / monthly_divisor if monthly_divisor else 0,
            'avg_monthly_months': monthly_divisor,
            'count': len(items),
            'missing_fx_count': sum(1 for item in items if item['dividend_income_amount_jpy'] is None),
        }

    ytd_dividends = [
        dividend for dividend in dividends
        if (dividend.get('payment_date') or '').startswith(current_year)
    ]
    return {
        'year': current_year,
        'ytd': summarize(ytd_dividends, portfolio_day.month),
        'all_time': summarize(dividends),
    }

def _parse_config_option_form(form):
    category = (form.get('category', '') or '').strip()
    value = (form.get('value', '') or '').strip()
    errors = []

    if category not in CONFIG_OPTION_DEFAULTS:
        errors.append("Configuration category is not recognized.")
    if not value:
        errors.append("Configuration value is required.")
    if len(value) > 80:
        errors.append("Configuration value must be 80 characters or fewer.")

    return category, value, errors

def _config_option_in_use(category, value):
    if category == 'broker':
        stock_column = 'broker'
        fund_column = 'broker'
    elif category == 'account_name':
        stock_column = 'account_name'
        fund_column = 'account_name'
    elif category == 'tax_status':
        stock_column = 'tax_status'
        fund_column = 'tax_status'
    else:
        return False

    with sqlite3.connect(DATABASE) as conn:
        stock_count = conn.execute(f"SELECT COUNT(*) FROM trades WHERE {stock_column} = ?", (value,)).fetchone()[0]
        fund_count = conn.execute(f"SELECT COUNT(*) FROM mutual_fund_trades WHERE {fund_column} = ?", (value,)).fetchone()[0]
    return (stock_count + fund_count) > 0

def get_app_settings():
    """Loads app-wide settings from the database, seeded from environment defaults."""
    with sqlite3.connect(DATABASE) as conn:
        conn.row_factory = sqlite3.Row
        conn.executemany(
            "INSERT OR IGNORE INTO app_settings (key, value) VALUES (?, ?)",
            [(key, str(value)) for key, value in APP_SETTING_DEFAULTS.items()]
        )
        rows = conn.execute("SELECT key, value FROM app_settings").fetchall()

    settings = APP_SETTING_DEFAULTS.copy()
    for row in rows:
        key = row['key']
        if key in settings:
            settings[key] = row['value']
    return settings

def _parse_app_settings_form(form):
    settings = {
        'daily_reset_time': (form.get('daily_reset_time', '') or '').strip(),
        'daily_reset_timezone': (form.get('daily_reset_timezone', '') or '').strip(),
    }
    errors = []

    if not settings['daily_reset_time']:
        errors.append("Daily Reset Time is required.")
    elif not re.match(r'^\d{2}:\d{2}$', settings['daily_reset_time']):
        errors.append("Daily Reset Time must use HH:MM format.")
    else:
        hour, minute = [int(part) for part in settings['daily_reset_time'].split(':', 1)]
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            errors.append("Daily Reset Time must be a valid 24-hour time.")
        else:
            settings['daily_reset_time'] = f"{hour:02d}:{minute:02d}"

    if not settings['daily_reset_timezone']:
        errors.append("Daily Reset Timezone is required.")
    elif not _is_valid_timezone(settings['daily_reset_timezone']):
        errors.append("Daily Reset Timezone must be a valid IANA timezone, like Asia/Tokyo.")

    return settings, errors

def save_app_settings(settings):
    with sqlite3.connect(DATABASE) as conn:
        conn.executemany(
            """
            INSERT INTO app_settings (key, value)
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
                account_name TEXT,
                tax_status TEXT,
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
                account_name TEXT,
                tax_status TEXT,
                fx_rate REAL
            )
        ''')
        trade_columns = [row[1] for row in conn.execute("PRAGMA table_info(trades)").fetchall()]
        if 'account_name' not in trade_columns:
            conn.execute("ALTER TABLE trades ADD COLUMN account_name TEXT")
        if 'tax_status' not in trade_columns:
            conn.execute("ALTER TABLE trades ADD COLUMN tax_status TEXT")
        fund_columns = [row[1] for row in conn.execute("PRAGMA table_info(mutual_fund_trades)").fetchall()]
        if 'account_name' not in fund_columns:
            conn.execute("ALTER TABLE mutual_fund_trades ADD COLUMN account_name TEXT")
        if 'tax_status' not in fund_columns:
            conn.execute("ALTER TABLE mutual_fund_trades ADD COLUMN tax_status TEXT")
        conn.execute("UPDATE trades SET account_name = 'Default' WHERE account_name IS NULL OR account_name = ''")
        conn.execute("UPDATE trades SET tax_status = 'Taxable' WHERE tax_status IS NULL OR tax_status = ''")
        conn.execute("UPDATE mutual_fund_trades SET account_name = COALESCE(NULLIF(account_name, ''), NULLIF(account_type, ''), 'Default')")
        conn.execute("UPDATE mutual_fund_trades SET tax_status = CASE WHEN tax_status IS NULL OR tax_status = '' THEN CASE WHEN account_type = 'NISA' THEN 'Non Taxable' ELSE 'Taxable' END ELSE tax_status END")
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
        conn.execute('''
            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        ''')
        conn.executemany(
            "INSERT OR IGNORE INTO app_settings (key, value) VALUES (?, ?)",
            [(key, str(value)) for key, value in APP_SETTING_DEFAULTS.items()]
        )
        conn.execute('''
            CREATE TABLE IF NOT EXISTS market_price_overrides (
                symbol TEXT NOT NULL,
                currency TEXT NOT NULL,
                instrument_type TEXT NOT NULL DEFAULT 'stock',
                current_price REAL NOT NULL,
                change_today REAL,
                latest_data_at TEXT,
                notes TEXT,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (symbol, currency, instrument_type)
            )
        ''')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS corporate_actions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                currency TEXT NOT NULL,
                action_type TEXT NOT NULL,
                effective_date TEXT NOT NULL,
                ratio REAL NOT NULL,
                affected_trades INTEGER NOT NULL DEFAULT 0,
                price_override REAL,
                notes TEXT,
                applied_at TEXT NOT NULL
            )
        ''')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS watchlist (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                name TEXT,
                currency TEXT NOT NULL DEFAULT 'USD',
                target_price REAL,
                stop_price REAL,
                notes TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS dividends (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                name TEXT NOT NULL,
                payment_date TEXT NOT NULL,
                currency TEXT NOT NULL,
                gross_amount REAL NOT NULL,
                tax_withheld REAL DEFAULT 0,
                foreign_tax_withheld REAL DEFAULT 0,
                japanese_income_tax_withheld REAL DEFAULT 0,
                japanese_local_tax_withheld REAL DEFAULT 0,
                deductible_interest REAL DEFAULT 0,
                quantity REAL,
                amount_per_share REAL,
                source_country TEXT,
                security_type TEXT DEFAULT 'listed_stock',
                tax_treatment TEXT DEFAULT 'undecided',
                broker TEXT,
                account_name TEXT,
                tax_status TEXT,
                fx_rate REAL,
                notes TEXT
            )
        ''')
        dividend_columns = [row[1] for row in conn.execute("PRAGMA table_info(dividends)").fetchall()]
        for column_name, column_definition in [
            ('foreign_tax_withheld', 'REAL DEFAULT 0'),
            ('japanese_income_tax_withheld', 'REAL DEFAULT 0'),
            ('japanese_local_tax_withheld', 'REAL DEFAULT 0'),
            ('deductible_interest', 'REAL DEFAULT 0'),
            ('source_country', 'TEXT'),
            ('security_type', "TEXT DEFAULT 'listed_stock'"),
            ('tax_treatment', "TEXT DEFAULT 'undecided'"),
        ]:
            if column_name not in dividend_columns:
                conn.execute(f"ALTER TABLE dividends ADD COLUMN {column_name} {column_definition}")
        conn.execute('''
            CREATE TABLE IF NOT EXISTS import_profiles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                instrument_type TEXT NOT NULL,
                encoding TEXT NOT NULL DEFAULT 'utf-8-sig',
                header_row INTEGER NOT NULL DEFAULT 1,
                row_filters TEXT NOT NULL DEFAULT '[]',
                mappings TEXT NOT NULL DEFAULT '{}',
                defaults TEXT NOT NULL DEFAULT '{}'
            )
        ''')
        for profile in IMPORT_PROFILE_DEFAULTS:
            conn.execute(
                """
                INSERT INTO import_profiles
                    (name, instrument_type, encoding, header_row, row_filters, mappings, defaults)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    instrument_type = excluded.instrument_type,
                    encoding = excluded.encoding,
                    header_row = excluded.header_row,
                    row_filters = excluded.row_filters,
                    mappings = excluded.mappings,
                    defaults = excluded.defaults
                """,
                (
                    profile['name'],
                    profile['instrument_type'],
                    profile.get('encoding') or 'utf-8-sig',
                    int(profile.get('header_row') or 1),
                    json.dumps(profile.get('row_filters') or [], ensure_ascii=False),
                    json.dumps(profile.get('mappings') or {}, ensure_ascii=False),
                    json.dumps(profile.get('defaults') or {}, ensure_ascii=False),
                )
            )
        conn.execute('''
            CREATE TABLE IF NOT EXISTS fx_rates (
                date TEXT PRIMARY KEY,
                tts REAL NOT NULL,
                ttm REAL,
                ttb REAL NOT NULL,
                fetched_at TEXT NOT NULL
            )
        ''')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS config_options (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category TEXT NOT NULL,
                value TEXT NOT NULL,
                sort_order INTEGER NOT NULL DEFAULT 0,
                UNIQUE(category, value)
            )
        ''')
        _seed_config_options(conn)
        conn.execute("INSERT OR IGNORE INTO config_options (category, value, sort_order) SELECT 'broker', broker, 100 FROM trades WHERE broker IS NOT NULL AND broker != ''")
        conn.execute("INSERT OR IGNORE INTO config_options (category, value, sort_order) SELECT 'broker', broker, 100 FROM mutual_fund_trades WHERE broker IS NOT NULL AND broker != ''")
        conn.execute("INSERT OR IGNORE INTO config_options (category, value, sort_order) SELECT 'account_name', account_name, 100 FROM trades WHERE account_name IS NOT NULL AND account_name != ''")
        conn.execute("INSERT OR IGNORE INTO config_options (category, value, sort_order) SELECT 'account_name', account_name, 100 FROM mutual_fund_trades WHERE account_name IS NOT NULL AND account_name != ''")
        conn.execute("INSERT OR IGNORE INTO config_options (category, value, sort_order) SELECT 'tax_status', tax_status, 100 FROM trades WHERE tax_status IS NOT NULL AND tax_status != ''")
        conn.execute("INSERT OR IGNORE INTO config_options (category, value, sort_order) SELECT 'tax_status', tax_status, 100 FROM mutual_fund_trades WHERE tax_status IS NOT NULL AND tax_status != ''")
    print("Database tables ensured to exist.")

def _calculate_portfolio_summary(trades, exchange_rate, broker_filter=None, currency_filter=None, account_name_filter=None, tax_status_filter=None):
    """
    Helper function to perform the main portfolio calculation.
    This is refactored out of the index() route for reuse.
    """
    # --- Aggregation Logic ---
    # This part always runs on all trades to correctly calculate
    # cost basis and realized P&L across the entire history, regardless of filters.
    # Filters are applied later before calculating summary values.
    holdings = {}
    today_str = _portfolio_day_str()
    portfolio_day = _portfolio_day()
    for trade in trades:
        # Aggregate by both symbol and broker for more granular tracking
        instrument_type = trade['instrument_type'] if 'instrument_type' in trade.keys() else 'stock'
        account_name = trade.get('account_name') or 'Default'
        tax_status = trade.get('tax_status') or 'Taxable'
        key = (trade['symbol'], trade['broker'], account_name, tax_status, instrument_type)
        if key not in holdings:
            holdings[key] = {
                'symbol': trade['symbol'],
                'broker': trade['broker'],
                'account_name': account_name,
                'tax_status': tax_status,
                'instrument_type': instrument_type,
                'name': trade['name'],
                'currency': trade['currency'],
                'quantity': 0,
                'total_cost': 0,
                'total_cost_jpy': 0,
                'realized_pnl_native': 0,
                'realized_pnl_jpy': 0,
                'realized_price_pnl_jpy': 0,
                'realized_fx_pnl_jpy': 0,
                'today_buy_quantity': 0,
                'today_buy_cost': 0,
                'trade_history': []
            }

        fee_amount = trade['fee_amount'] or 0.0
        holdings[key]['trade_history'].append({
            'trade_date': trade['trade_date'],
            'trade_type': trade['trade_type'],
            'quantity': trade['quantity'],
            'price': trade['price'],
            'price_display': trade['price'],
            'currency': trade['currency'],
            'broker': trade['broker'],
            'account_name': account_name,
            'tax_status': tax_status,
            'fee_amount': fee_amount,
            'fee_currency': trade['fee_currency'],
            'fx_rate': trade['fx_rate'],
            'instrument_type': instrument_type
        })
        
        # Fee calculation
        fee_currency = trade['fee_currency']
        trade_currency = trade['currency']
        trade_fx_rate = trade['fx_rate'] or exchange_rate

        fee_in_native_currency = fee_amount
        if fee_currency and fee_currency != trade_currency and exchange_rate > 0:
            if fee_currency == 'JPY' and trade_currency == 'USD':
                fee_in_native_currency = fee_amount / trade_fx_rate if trade_fx_rate else 0
            elif fee_currency == 'USD' and trade_currency == 'JPY':
                fee_in_native_currency = fee_amount * trade_fx_rate if trade_fx_rate else 0

        fee_jpy = 0
        if fee_currency == 'JPY':
            fee_jpy = fee_amount
        elif fee_currency == 'USD' and trade_fx_rate:
            fee_jpy = fee_amount * trade_fx_rate

        if trade['trade_type'] == 'BUY':
            gross_value_native = _trade_gross_value(trade)
            trade_cost_native = gross_value_native + fee_in_native_currency
            if trade_currency == 'JPY':
                trade_cost_jpy = gross_value_native + fee_jpy
            else:
                trade_cost_jpy = (gross_value_native * trade_fx_rate) + fee_jpy if trade_fx_rate else 0
            holdings[key]['quantity'] += trade['quantity']
            holdings[key]['total_cost'] += trade_cost_native
            holdings[key]['total_cost_jpy'] += trade_cost_jpy
            if trade['trade_date'] == today_str:
                holdings[key]['today_buy_quantity'] += trade['quantity']
                holdings[key]['today_buy_cost'] += trade_cost_native
        elif trade['trade_type'] == 'SELL':
            avg_unit_cost_basis = 0
            avg_unit_cost_basis_jpy = 0
            if holdings[key]['quantity'] > 0:
                avg_unit_cost_basis = holdings[key]['total_cost'] / holdings[key]['quantity']
                avg_unit_cost_basis_jpy = holdings[key]['total_cost_jpy'] / holdings[key]['quantity']
            
            cost_of_shares_sold = trade['quantity'] * avg_unit_cost_basis
            cost_of_shares_sold_jpy = trade['quantity'] * avg_unit_cost_basis_jpy
            gross_value_native = _trade_gross_value(trade)
            proceeds = gross_value_native - fee_in_native_currency
            if trade_currency == 'JPY':
                proceeds_jpy = gross_value_native - fee_jpy
            else:
                proceeds_jpy = (gross_value_native * trade_fx_rate) - fee_jpy if trade_fx_rate else 0
            realized_pnl_native = proceeds - cost_of_shares_sold
            realized_pnl_jpy = proceeds_jpy - cost_of_shares_sold_jpy
            realized_price_pnl_jpy = realized_pnl_native if trade_currency == 'JPY' else realized_pnl_native * trade_fx_rate
            realized_fx_pnl_jpy = realized_pnl_jpy - realized_price_pnl_jpy
            
            holdings[key]['realized_pnl_native'] += realized_pnl_native
            holdings[key]['realized_pnl_jpy'] += realized_pnl_jpy
            holdings[key]['realized_price_pnl_jpy'] += realized_price_pnl_jpy
            holdings[key]['realized_fx_pnl_jpy'] += realized_fx_pnl_jpy
            holdings[key]['quantity'] -= trade['quantity']
            holdings[key]['total_cost'] -= cost_of_shares_sold
            holdings[key]['total_cost_jpy'] -= cost_of_shares_sold_jpy

    # --- Combine display rows ---
    # Keep the broker-level accounting above, then combine holdings for display.
    # Closed broker/account buckets still contribute their trade history when the
    # same ticker remains open elsewhere.
    combined_holdings = {}
    summary_list = []
    total_realized_pnl_usd = 0.0
    total_realized_pnl_jpy = 0.0
    total_realized_price_pnl_jpy = 0.0
    total_realized_fx_pnl_jpy = 0.0

    for key, data in holdings.items():
        # Apply filters before calculating summary totals
        if broker_filter and data['broker'] != broker_filter:
            continue
        if currency_filter and data['currency'] != currency_filter:
            continue
        if account_name_filter and data['account_name'] != account_name_filter:
            continue
        if tax_status_filter and data['tax_status'] != tax_status_filter:
            continue

        total_realized_pnl_jpy += data['realized_pnl_jpy']
        total_realized_price_pnl_jpy += data['realized_price_pnl_jpy']
        total_realized_fx_pnl_jpy += data['realized_fx_pnl_jpy']
        if exchange_rate > 0:
            total_realized_pnl_usd += data['realized_pnl_jpy'] / exchange_rate

        combined_key = (data['symbol'], data['currency'], data['instrument_type'])
        if combined_key not in combined_holdings:
            combined_holdings[combined_key] = {
                'symbol': data['symbol'],
                'broker': '',
                'brokers': [],
                'account_name': '',
                'account_names': [],
                'tax_status': '',
                'tax_statuses': [],
                'instrument_type': data['instrument_type'],
                'name': data['name'],
                'currency': data['currency'],
                'quantity': 0,
                'total_cost': 0,
                'total_cost_jpy': 0,
                'today_buy_quantity': 0,
                'today_buy_cost': 0,
                'trade_history': [],
            }
        combined_holdings[combined_key]['trade_history'].extend(data['trade_history'])

        if data['quantity'] <= 0.00001:
            continue

        if data['broker'] not in combined_holdings[combined_key]['brokers']:
            combined_holdings[combined_key]['brokers'].append(data['broker'])
        if data['account_name'] not in combined_holdings[combined_key]['account_names']:
            combined_holdings[combined_key]['account_names'].append(data['account_name'])
        if data['tax_status'] not in combined_holdings[combined_key]['tax_statuses']:
            combined_holdings[combined_key]['tax_statuses'].append(data['tax_status'])

        combined_holdings[combined_key]['quantity'] += data['quantity']
        combined_holdings[combined_key]['total_cost'] += data['total_cost']
        combined_holdings[combined_key]['total_cost_jpy'] += data['total_cost_jpy']
        combined_holdings[combined_key]['today_buy_quantity'] += data['today_buy_quantity']
        combined_holdings[combined_key]['today_buy_cost'] += data['today_buy_cost']

    # --- Enrichment and Summary ---
    total_portfolio_value_usd = 0.0
    total_unrealized_pnl_usd = 0.0
    total_unrealized_pnl_jpy = 0.0
    total_unrealized_price_pnl_jpy = 0.0
    total_unrealized_fx_pnl_jpy = 0.0
    total_today_pnl_usd = 0.0
    market_data_complete = True
    market_data_timestamps = []

    for data in combined_holdings.values():
        if data['quantity'] <= 0.00001:
            continue

        data['broker'] = ', '.join(data['brokers'])
        data['account_name'] = ', '.join(data['account_names'])
        data['tax_status'] = ', '.join(data['tax_statuses'])
        data['trade_history'] = sorted(data['trade_history'], key=lambda trade: trade['trade_date'], reverse=True)

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
        elif not _is_daily_change_current(market_data.get('change_date'), portfolio_day):
            market_data = {
                **market_data,
                'change_today': 0.0,
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
            data['current_value_jpy'] = current_value_native
            data['current_value_usd'] = current_value_native / exchange_rate
            data['price_pnl_jpy'] = data['pnl_native']
        else:
            data['current_value_jpy'] = current_value_native * exchange_rate
            data['current_value_usd'] = current_value_native
            data['price_pnl_jpy'] = data['pnl_native'] * exchange_rate

        data['pnl_jpy'] = data['current_value_jpy'] - data['total_cost_jpy']
        data['fx_pnl_jpy'] = data['pnl_jpy'] - data['price_pnl_jpy']
        data['pnl_usd'] = data['pnl_jpy'] / exchange_rate if exchange_rate > 0 else 0

        total_portfolio_value_usd += data['current_value_usd']
        total_unrealized_pnl_usd += data['pnl_usd']
        total_unrealized_pnl_jpy += data['pnl_jpy']
        total_unrealized_price_pnl_jpy += data['price_pnl_jpy']
        total_unrealized_fx_pnl_jpy += data['fx_pnl_jpy']
        
        summary_list.append(data)

    total_portfolio_value_jpy = total_portfolio_value_usd * exchange_rate if exchange_rate > 0 else 0
    total_today_pnl_jpy = total_today_pnl_usd * exchange_rate if exchange_rate > 0 else 0
    total_pnl_jpy = total_realized_pnl_jpy + total_unrealized_pnl_jpy
    total_price_pnl_jpy = total_realized_price_pnl_jpy + total_unrealized_price_pnl_jpy
    total_fx_pnl_jpy = total_realized_fx_pnl_jpy + total_unrealized_fx_pnl_jpy
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
        'total_unrealized_price_pnl_jpy': total_unrealized_price_pnl_jpy,
        'total_unrealized_fx_pnl_jpy': total_unrealized_fx_pnl_jpy,
        'total_today_pnl_usd': total_today_pnl_usd,
        'total_today_pnl_jpy': total_today_pnl_jpy,
        'total_realized_price_pnl_jpy': total_realized_price_pnl_jpy,
        'total_realized_fx_pnl_jpy': total_realized_fx_pnl_jpy,
        'total_pnl_jpy': total_pnl_jpy,
        'total_price_pnl_jpy': total_price_pnl_jpy,
        'total_fx_pnl_jpy': total_fx_pnl_jpy,
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
    today_date = _portfolio_day()
    today_str = today_date.strftime('%Y-%m-%d')

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
    today_date = _portfolio_day()
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


# FX Rate Fetching Function
def _fetch_jpy_usd_rate(trade_date_str):
    """
    Fetch JPY/USD exchange rate from cache or MURC website.
    Stores rates in database for future use.
    Returns dict with 'tts', 'ttm', 'ttb' values, or error message.
    """
    try:
        # Check database cache first
        with sqlite3.connect(DATABASE) as conn:
            row = conn.execute('SELECT tts, ttm, ttb FROM fx_rates WHERE date = ?', (trade_date_str,)).fetchone()
            if row:
                return {'tts': row[0], 'ttm': row[1], 'ttb': row[2], 'date': trade_date_str, 'cached': True}
    except Exception:
        pass
    
    # Not in cache, fetch from website
    if not BeautifulSoup:
        return {'error': 'BeautifulSoup not installed'}
    
    try:
        # Parse trade date to YYMMDD format
        trade_date = datetime.strptime(trade_date_str, '%Y-%m-%d')
        date_param = trade_date.strftime('%y%m%d')
        
        # Construct URL
        url = f'https://www.murc-kawasesouba.jp/fx/past/index.php?id={date_param}'
        
        # Fetch page with timeout
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        }
        req = urllib.request.Request(url, headers=headers)
        
        with urllib.request.urlopen(req, timeout=10) as response:
            raw = response.read()
            try:
                header_charset = response.headers.get_content_charset()
            except Exception:
                header_charset = None

        # Attempt sensible decodings for Japanese websites: try utf-8, header charset,
        # then common Japanese encodings (shift_jis / cp932 / euc_jp / iso-2022-jp).
        html_content = None
        try:
            html_content = raw.decode('utf-8')
        except UnicodeDecodeError:
            # try charset from headers first
            if header_charset:
                try:
                    html_content = raw.decode(header_charset)
                except Exception:
                    html_content = None

            if html_content is None:
                for enc in ('shift_jis', 'cp932', 'euc_jp', 'iso-2022-jp'):
                    try:
                        html_content = raw.decode(enc)
                        break
                    except Exception:
                        html_content = None

            # last resort: replace invalid bytes so parsing can continue
            if html_content is None:
                html_content = raw.decode('utf-8', errors='replace')

        soup = BeautifulSoup(html_content, 'html.parser')

        # Look for a table that contains TTS/TTB headers (MURC uses a table per-date).
        tts = None
        ttm = None
        ttb = None

        # Prefer the specific table structure used on MURC (class/data-table7)
        tables = soup.find_all('table')
        for table in tables:
            # collect header texts
            header_texts = [th.get_text(strip=True) for th in table.find_all('th')]
            if any(h in ('TTS', 'TTM', 'TTB') for h in header_texts):
                # iterate body rows and find the row where code is USD
                for tr in table.find_all('tr'):
                    cells = tr.find_all(['td', 'th'])
                    texts = [c.get_text(strip=True) for c in cells]
                    # find 'USD' in the row (third column in known layout)
                    if any(t == 'USD' for t in texts):
                        try:
                            # Known layout: [Currency, Japanese name, Code, TTS, TTB, *]
                            # Defensive indexing
                            if len(texts) >= 5:
                                raw_tts = texts[3].replace(',', '')
                                raw_ttb = texts[4].replace(',', '')
                                tts = float(raw_tts) if raw_tts not in ('', '-') else None
                                ttb = float(raw_ttb) if raw_ttb not in ('', '-') else None
                        except Exception:
                            pass
                        # try to extract mid-rate (TTM) from header row if present
                        # or leave as None
                        break
                if tts is not None or ttb is not None:
                    break

        # Fallback: older parsing heuristics (search nearby cells for labels)
        if tts is None and ttb is None:
            for cell in soup.find_all(['td', 'th']):
                text = cell.get_text(strip=True)
                # look for numeric patterns directly following labels
                if 'USD' in text and (tts is None or ttb is None):
                    parent = cell.parent
                    if parent:
                        cells = parent.find_all(['td', 'th'])
                        # try to locate USD and read numeric columns
                        texts = [c.get_text(strip=True) for c in cells]
                        for i, tt in enumerate(texts):
                            if tt == 'USD':
                                try:
                                    if i + 1 < len(texts):
                                        # next columns likely TTS and TTB depending on layout
                                        if tts is None and i + 2 < len(texts):
                                            raw_tts = texts[i+1].replace(',', '')
                                            tts = float(raw_tts)
                                        if ttb is None and i + 3 < len(texts):
                                            raw_ttb = texts[i+2].replace(',', '')
                                            ttb = float(raw_ttb)
                                except Exception:
                                    pass
                                break
        
        if tts and ttb:
            # Store in database cache
            try:
                with sqlite3.connect(DATABASE) as conn:
                    conn.execute(
                        'INSERT OR REPLACE INTO fx_rates (date, tts, ttm, ttb, fetched_at) VALUES (?, ?, ?, ?, ?)',
                        (trade_date_str, tts, ttm, ttb, _portfolio_now().strftime('%Y-%m-%d %H:%M:%S'))
                    )
            except Exception:
                pass  # Silently fail on cache store
            
            return {'tts': tts, 'ttm': ttm, 'ttb': ttb, 'date': trade_date_str, 'cached': False}
        else:
            return {'error': 'Could not find rates on MURC page. Try entering manually.'}
    
    except urllib.error.URLError:
        return {'error': 'Could not fetch rates from MURC website'}
    except Exception as e:
        return {'error': f'Error fetching rates: {str(e)[:100]}'}



@app.route('/api/fx-rate', methods=['POST'])
def api_fetch_fx_rate():
    """API endpoint to fetch FX rate for a given date."""
    _validate_csrf_token()
    
    trade_date = request.form.get('trade_date', '')
    trade_type = request.form.get('trade_type', 'BUY')  # For stocks
    
    if not trade_date:
        return jsonify({'error': 'No trade date provided'}), 400
    # Prevent fetching rates for the current portfolio date (today) via the UI/API
    try:
        if trade_date == _portfolio_day_str():
            return jsonify({'error': "Fetching today's rates is disabled via the UI. Please enter the rate manually."}), 400
    except Exception:
        pass

    result = _fetch_jpy_usd_rate(trade_date)
    
    if 'error' in result:
        return jsonify(result), 400
    
    # Determine which rate to use based on trade type
    if trade_type.upper() == 'SELL':
        fx_rate = result['ttb']
        rate_type = 'TTB (Selling)'
    else:
        fx_rate = result['tts']
        rate_type = 'TTS (Buying)'
    
    return jsonify({
        'fx_rate': fx_rate,
        'rate_type': rate_type,
        'date': result['date'],
        'tts': result['tts'],
        'ttb': result['ttb']
    })



def _get_all_stock_trades():
    """Fetch all stock trades from database for reconciliation."""
    with sqlite3.connect(DATABASE) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT id, symbol, name, trade_type, quantity, price, currency, trade_date, 
                   broker, account_name, tax_status, fx_rate, fee_amount, fee_currency
            FROM trades
            ORDER BY symbol, trade_date
            """
        ).fetchall()
    return [dict(row) for row in rows]

def _get_all_mutual_fund_trades():
    """Fetch all mutual fund trades from database for reconciliation."""
    with sqlite3.connect(DATABASE) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT id, fund_code, fund_name, transaction_type, transaction_detail, account_type,
                   currency, executed_units, nav_per_10000, trade_date, settlement_date,
                   settlement_amount, broker, account_name, tax_status, fx_rate
            FROM mutual_fund_trades
            ORDER BY fund_code, trade_date
            """
        ).fetchall()
    return [dict(row) for row in rows]

def _get_all_dividends():
    """Fetch all dividends from database for reconciliation."""
    with sqlite3.connect(DATABASE) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT id, symbol, name, payment_date, currency, gross_amount, tax_withheld,
                   foreign_tax_withheld, japanese_income_tax_withheld, japanese_local_tax_withheld,
                   deductible_interest, quantity, amount_per_share, source_country, security_type,
                   tax_treatment, broker, account_name, tax_status, fx_rate, notes
            FROM dividends
            ORDER BY symbol, payment_date
            """
        ).fetchall()
    return [dict(row) for row in rows]

def _make_trade_key(trade):
    """Create a hashable key for a stock trade (without ID)."""
    return (
        trade.get('symbol'), trade.get('name'), trade.get('trade_type'),
        trade.get('quantity'), trade.get('price'), trade.get('currency'),
        trade.get('trade_date'), trade.get('broker'), trade.get('account_name'),
        trade.get('tax_status'), trade.get('fx_rate'), trade.get('fee_amount'),
        trade.get('fee_currency')
    )

def _make_fund_key(trade):
    """Create a hashable key for a mutual fund trade (without ID)."""
    return (
        trade.get('fund_code'), trade.get('fund_name'), trade.get('transaction_type'),
        trade.get('transaction_detail'), trade.get('account_type'), trade.get('currency'),
        trade.get('executed_units'), trade.get('nav_per_10000'), trade.get('trade_date'),
        trade.get('settlement_date'), trade.get('settlement_amount'), trade.get('broker'),
        trade.get('account_name'), trade.get('tax_status'), trade.get('fx_rate')
    )

def _make_dividend_key(dividend):
    """Create a hashable key for a dividend (without ID)."""
    return (
        dividend.get('symbol'), dividend.get('name'), dividend.get('payment_date'),
        dividend.get('currency'), dividend.get('gross_amount'), dividend.get('tax_withheld'),
        dividend.get('foreign_tax_withheld'), dividend.get('japanese_income_tax_withheld'),
        dividend.get('japanese_local_tax_withheld'), dividend.get('quantity'),
        dividend.get('amount_per_share'), dividend.get('source_country'),
        dividend.get('security_type'), dividend.get('tax_treatment'),
        dividend.get('broker'), dividend.get('account_name'), dividend.get('tax_status'),
        dividend.get('fx_rate')
    )

def _reconcile_records(broker_records, db_records, key_func):
    """
    Compare broker records against database records.
    Returns a dict with: matched, missing_in_db, extra_in_db, mismatched.
    """
    broker_keyset = {key_func(r): r for r in broker_records}
    db_keyset = {key_func(r): r for r in db_records}
    
    matched = []
    missing_in_db = []
    extra_in_db = []
    
    # Check broker records against DB
    for key, broker_record in broker_keyset.items():
        if key in db_keyset:
            matched.append({'broker': broker_record, 'db': db_keyset[key]})
        else:
            missing_in_db.append(broker_record)
    
    # Check DB records against broker
    for key, db_record in db_keyset.items():
        if key not in broker_keyset:
            extra_in_db.append(db_record)
    
    return {
        'matched': matched,
        'missing_in_db': missing_in_db,
        'extra_in_db': extra_in_db,
        'matched_count': len(matched),
        'missing_count': len(missing_in_db),
        'extra_count': len(extra_in_db),
    }


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
    account_name_filter = request.args.get('account_name', 'all')
    tax_status_filter = request.args.get('tax_status', 'all')

    # Convert 'all' to None for the calculation function, which expects None for no filter
    effective_broker_filter = broker_filter if broker_filter != 'all' else None
    effective_currency_filter = currency_filter if currency_filter != 'all' else None
    effective_account_name_filter = account_name_filter if account_name_filter != 'all' else None
    effective_tax_status_filter = tax_status_filter if tax_status_filter != 'all' else None

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
    summary = _calculate_portfolio_summary(
        trades,
        exchange_rate,
        effective_broker_filter,
        effective_currency_filter,
        effective_account_name_filter,
        effective_tax_status_filter
    )
    has_live_data_issue = (not market_data_reliable) or (not summary['market_data_complete'])
    if has_live_data_issue:
        flash('Live market data is temporarily unavailable. Showing cost-basis fallback values and skipping today\'s history snapshot.', 'info')

    # 3. Ensure history is up-to-date with the TOTAL portfolio value.
    # If filters are active, we must re-calculate the summary without them for the history.
    if effective_broker_filter or effective_currency_filter or effective_account_name_filter or effective_tax_status_filter:
        total_summary = _calculate_portfolio_summary(trades, exchange_rate)
        if market_data_reliable and total_summary['market_data_complete']:
            _ensure_history_updated(total_summary)
    else:
        # No filters active, so we can use the summary we already have
        if market_data_reliable and summary['market_data_complete']:
            _ensure_history_updated(summary)

    # 4. Debug logging: print filters and summary totals (temporary)
    try:
        print(f"Index view filters: broker={broker_filter} currency={currency_filter} account_name={account_name_filter} tax_status={tax_status_filter}")
        print(f"Summary totals: total_today_pnl_jpy={summary.get('total_today_pnl_jpy')} total_value_jpy={summary.get('total_value_jpy')}")
        for stock in summary.get('stocks', []):
            try:
                prev_close = stock['current_price'] - stock.get('change_today', 0.0)
                print(f"STOCK {stock['symbol']} curr={stock['current_price']} change_today={stock.get('change_today')} prev_close={prev_close} today_pnl_jpy={stock.get('today_pnl_jpy')}")
            except Exception:
                continue
    except Exception:
        pass

    # 4. Render the page
    prices_last_updated = _portfolio_now().strftime('%Y-%m-%d %H:%M')
    config_options = get_config_options()
    return render_template('index.html', 
                           **summary, 
                           exchange_rate=exchange_rate, 
                           prices_last_updated=prices_last_updated,
                           prices_last_updated_ago='just now',
                           fx_latest_data_at=fx_latest_data_at,
                           fx_latest_data_ago=fx_latest_data_ago,
                           brokers=config_options['broker'],
                           account_names=config_options['account_name'],
                           tax_statuses=config_options['tax_status'],
                           selected_broker=broker_filter,
                           selected_currency=currency_filter,
                           selected_account_name=account_name_filter,
                           selected_tax_status=tax_status_filter,
                           watchlist=_fetch_watchlist())

@app.route('/watchlist', methods=['POST'])
def add_watchlist_item():
    _validate_csrf_token()
    values, errors = _parse_watchlist_form(request.form)
    if errors:
        for error in errors:
            flash(error, 'danger')
        return redirect(url_for('index'))

    now = _portfolio_now().strftime('%Y-%m-%d %H:%M:%S')
    with sqlite3.connect(DATABASE) as conn:
        conn.execute('''
            INSERT INTO watchlist (symbol, name, currency, target_price, stop_price, notes, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            values['symbol'],
            values['name'],
            values['currency'],
            values['target_price'],
            values['stop_price'],
            values['notes'],
            now,
            now
        ))
    flash(f"{values['symbol']} added to the watch list.", 'success')
    return redirect(url_for('index'))

@app.route('/watchlist/<int:item_id>', methods=['POST'])
def update_watchlist_item(item_id):
    _validate_csrf_token()
    values, errors = _parse_watchlist_form(request.form)
    if errors:
        for error in errors:
            flash(error, 'danger')
        return redirect(url_for('index'))

    with sqlite3.connect(DATABASE) as conn:
        cursor = conn.execute('''
            UPDATE watchlist
            SET symbol = ?, name = ?, currency = ?, target_price = ?, stop_price = ?, notes = ?, updated_at = ?
            WHERE id = ?
        ''', (
            values['symbol'],
            values['name'],
            values['currency'],
            values['target_price'],
            values['stop_price'],
            values['notes'],
            _portfolio_now().strftime('%Y-%m-%d %H:%M:%S'),
            item_id
        ))
    if cursor.rowcount:
        flash(f"{values['symbol']} watch details updated.", 'success')
    else:
        flash("Watch list item was not found.", 'warning')
    return redirect(url_for('index'))

@app.route('/watchlist/<int:item_id>/delete', methods=['POST'])
def delete_watchlist_item(item_id):
    _validate_csrf_token()
    with sqlite3.connect(DATABASE) as conn:
        cursor = conn.execute("DELETE FROM watchlist WHERE id = ?", (item_id,))
    if cursor.rowcount:
        flash("Watch list item removed.", 'success')
    else:
        flash("Watch list item was not found.", 'warning')
    return redirect(url_for('index'))

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
    with sqlite3.connect(DATABASE) as conn:
        conn.row_factory = sqlite3.Row
        history_rows = conn.execute("SELECT date, value_jpy, unrealized_pnl_jpy FROM portfolio_history ORDER BY date DESC LIMIT 365").fetchall()
        history_data = [dict(row) for row in reversed(history_rows)]
    prices_last_updated = _portfolio_now().strftime('%Y-%m-%d %H:%M')

    return render_template(
        'health.html',
        **summary,
        health=health,
        performance=performance,
        dividend_income_summary=_calculate_dividend_income_summary(),
        history_data=history_data,
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

@app.route('/config', methods=['GET', 'POST'])
def app_config():
    """Lets the user edit app-wide configuration."""
    settings = get_app_settings()
    config_options = get_config_options()

    if request.method == 'POST':
        _validate_csrf_token()
        action = request.form.get('action', 'save_settings')

        if action == 'save_settings':
            settings, errors = _parse_app_settings_form(request.form)
            if errors:
                for error in errors:
                    flash(error, 'danger')
            else:
                save_app_settings(settings)
                flash('Configuration saved.', 'success')
                return redirect(url_for('app_config'))
        elif action == 'add_option':
            category, value, errors = _parse_config_option_form(request.form)
            if errors:
                for error in errors:
                    flash(error, 'danger')
            else:
                try:
                    with sqlite3.connect(DATABASE) as conn:
                        max_sort = conn.execute(
                            "SELECT COALESCE(MAX(sort_order), -1) FROM config_options WHERE category = ?",
                            (category,)
                        ).fetchone()[0]
                        conn.execute(
                            "INSERT INTO config_options (category, value, sort_order) VALUES (?, ?, ?)",
                            (category, value, max_sort + 1)
                        )
                    flash('Configuration value added.', 'success')
                except sqlite3.IntegrityError:
                    flash('That configuration value already exists.', 'warning')
                return redirect(url_for('app_config'))
        elif action == 'update_option':
            option_id = request.form.get('option_id')
            category, value, errors = _parse_config_option_form(request.form)
            if errors:
                for error in errors:
                    flash(error, 'danger')
            else:
                with sqlite3.connect(DATABASE) as conn:
                    conn.row_factory = sqlite3.Row
                    option = conn.execute("SELECT * FROM config_options WHERE id = ?", (option_id,)).fetchone()
                    if option is None:
                        abort(404)
                    if option['category'] != category:
                        abort(400)
                    if option['value'] != value and _config_option_in_use(category, option['value']):
                        flash('Cannot rename a value while trades are using it. Add the new value, update those trades, then delete the old value.', 'warning')
                    else:
                        try:
                            conn.execute("UPDATE config_options SET value = ? WHERE id = ?", (value, option_id))
                            flash('Configuration value updated.', 'success')
                        except sqlite3.IntegrityError:
                            flash('That configuration value already exists.', 'warning')
                return redirect(url_for('app_config'))
        elif action == 'delete_option':
            option_id = request.form.get('option_id')
            with sqlite3.connect(DATABASE) as conn:
                conn.row_factory = sqlite3.Row
                option = conn.execute("SELECT * FROM config_options WHERE id = ?", (option_id,)).fetchone()
                if option is None:
                    abort(404)
                count = conn.execute("SELECT COUNT(*) FROM config_options WHERE category = ?", (option['category'],)).fetchone()[0]
                if count <= 1:
                    flash('Each category must keep at least one value.', 'warning')
                elif _config_option_in_use(option['category'], option['value']):
                    flash('Cannot delete a configuration value that is used by trades.', 'warning')
                else:
                    conn.execute("DELETE FROM config_options WHERE id = ?", (option_id,))
                    flash('Configuration value deleted.', 'success')
            return redirect(url_for('app_config'))
        else:
            abort(400)

    return render_template(
        'config.html',
        settings=settings,
        labels=APP_SETTING_LABELS,
        config_option_labels=CONFIG_OPTION_LABELS,
        config_option_rows=config_options['_rows']
    )

@app.route('/api/portfolio')
def api_portfolio():
    """
    API endpoint to return portfolio summary as JSON.
    Accepts broker, currency, account_name, tax_status, include_history, and history_days query parameters.
    """
    include_history = request.args.get('include_history', '').lower() in ['1', 'true', 'yes']
    payload, *_ = _build_portfolio_api_payload(
        include_history=include_history,
        history_days=request.args.get('history_days', 365)
    )
    return jsonify(payload)

@app.route('/api/health')
def api_health():
    """API endpoint for health score, performance, P&L breakdown, and allocations."""
    _, summary, trades, exchange_context, filters = _build_portfolio_api_payload()
    settings = get_health_settings()
    health = _calculate_portfolio_health(summary, settings)
    performance = _calculate_portfolio_performance(summary, trades, exchange_context['exchange_rate'])
    return jsonify({
        'meta': _api_meta(summary, filters, exchange_context),
        'totals': _summary_totals(summary),
        'health': health,
        'performance': performance,
        'pnl_breakdown': {
            'overall_jpy': summary['total_pnl_jpy'],
            'price_instrument_jpy': summary['total_price_pnl_jpy'],
            'fx_impact_jpy': summary['total_fx_pnl_jpy'],
            'open_holdings': [
                {
                    'symbol': stock['symbol'],
                    'name': stock['name'],
                    'broker': stock['broker'],
                    'currency': stock['currency'],
                    'instrument_type': stock['instrument_type'],
                    'weight_percent': stock.get('weight_percent'),
                    'current_value_jpy': stock['current_value_jpy'],
                    'total_pnl_jpy': stock['pnl_jpy'],
                    'price_instrument_pnl_jpy': stock['price_pnl_jpy'],
                    'fx_pnl_jpy': stock['fx_pnl_jpy'],
                    'today_pnl_jpy': stock['today_pnl_jpy'],
                }
                for stock in health['stocks']
            ],
        },
    })

@app.route('/api/history')
def api_history():
    """API endpoint for portfolio history chart data."""
    days = _parse_history_days(request.args.get('days', 365))
    return jsonify({
        'meta': {
            'generated_at': _portfolio_now().strftime('%Y-%m-%d %H:%M:%S'),
            'portfolio_day': _portfolio_day_str(),
            'days': days,
        },
        'history': _get_history_data(days),
    })

@app.route('/api/tax-report')
def api_tax_report():
    """API endpoint for Japanese tax report calculations."""
    year = request.args.get('year')
    if not year:
        available_years = _get_available_tax_years()
        year = available_years[0] if available_years else str(_portfolio_day().year)
    try:
        year_int = int(year)
    except (TypeError, ValueError):
        return jsonify({'error': 'year must be a valid four-digit year'}), 400

    broker = request.args.get('broker', 'all')
    account_name = request.args.get('account_name', 'all')
    tax_status = request.args.get('tax_status', 'all')
    report = generate_tax_report_data(
        year_int,
        broker if broker != 'all' else None,
        account_name if account_name != 'all' else None,
        tax_status if tax_status != 'all' else None
    )
    return jsonify({
        'meta': {
            'generated_at': _portfolio_now().strftime('%Y-%m-%d %H:%M:%S'),
            'filters': {
                'year': year_int,
                'broker': broker,
                'account_name': account_name,
                'tax_status': tax_status,
            },
        },
        **report,
    })

@app.route('/api/options')
def api_options():
    """API endpoint for filter options and available report years."""
    config_options = get_config_options()
    return jsonify({
        'brokers': config_options['broker'],
        'account_names': config_options['account_name'],
        'tax_statuses': config_options['tax_status'],
        'currencies': ['USD', 'JPY'],
        'available_tax_years': _get_available_tax_years(),
        'health_settings': get_health_settings(),
    })

def _openapi_spec():
    return {
        'openapi': '3.1.0',
        'info': {
            'title': 'Portfolio Tracker API',
            'version': APP_VERSION,
            'description': 'Machine-readable API for portfolio summary, health, history, tax report, options, and version data.',
        },
        'servers': [{'url': '/'}],
        'paths': {
            '/api/portfolio': {
                'get': {
                    'summary': 'Get enriched portfolio summary',
                    'parameters': [
                        {'name': 'broker', 'in': 'query', 'schema': {'type': 'string', 'default': 'all'}},
                        {'name': 'currency', 'in': 'query', 'schema': {'type': 'string', 'enum': ['all', 'USD', 'JPY'], 'default': 'all'}},
                        {'name': 'account_name', 'in': 'query', 'schema': {'type': 'string', 'default': 'all'}},
                        {'name': 'tax_status', 'in': 'query', 'schema': {'type': 'string', 'default': 'all'}},
                        {'name': 'include_history', 'in': 'query', 'schema': {'type': 'boolean', 'default': False}},
                        {'name': 'history_days', 'in': 'query', 'schema': {'type': 'integer', 'default': 365, 'minimum': 1, 'maximum': 3650}},
                    ],
                    'responses': {'200': {'description': 'Portfolio summary'}},
                }
            },
            '/api/health': {
                'get': {
                    'summary': 'Get portfolio health, performance, allocation, and P&L breakdown',
                    'parameters': [
                        {'name': 'broker', 'in': 'query', 'schema': {'type': 'string', 'default': 'all'}},
                        {'name': 'currency', 'in': 'query', 'schema': {'type': 'string', 'enum': ['all', 'USD', 'JPY'], 'default': 'all'}},
                        {'name': 'account_name', 'in': 'query', 'schema': {'type': 'string', 'default': 'all'}},
                        {'name': 'tax_status', 'in': 'query', 'schema': {'type': 'string', 'default': 'all'}},
                    ],
                    'responses': {'200': {'description': 'Health payload'}},
                }
            },
            '/api/history': {
                'get': {
                    'summary': 'Get portfolio history chart rows',
                    'parameters': [
                        {'name': 'days', 'in': 'query', 'schema': {'type': 'integer', 'default': 365, 'minimum': 1, 'maximum': 3650}},
                    ],
                    'responses': {'200': {'description': 'History rows'}},
                }
            },
            '/api/tax-report': {
                'get': {
                    'summary': 'Get Japanese tax report data',
                    'parameters': [
                        {'name': 'year', 'in': 'query', 'schema': {'type': 'integer'}},
                        {'name': 'broker', 'in': 'query', 'schema': {'type': 'string', 'default': 'all'}},
                        {'name': 'account_name', 'in': 'query', 'schema': {'type': 'string', 'default': 'all'}},
                        {'name': 'tax_status', 'in': 'query', 'schema': {'type': 'string', 'default': 'all'}},
                    ],
                    'responses': {'200': {'description': 'Tax report payload'}, '400': {'description': 'Invalid year'}},
                }
            },
            '/api/options': {
                'get': {
                    'summary': 'Get filter options and settings',
                    'responses': {'200': {'description': 'Available options'}},
                }
            },
            '/api/trades': {
                'get': {
                    'summary': 'Get combined trade-level data',
                    'parameters': [
                        {'name': 'instrument_type', 'in': 'query', 'schema': {'type': 'string', 'enum': ['all', 'stock', 'mutual_fund'], 'default': 'all'}},
                        {'name': 'limit', 'in': 'query', 'schema': {'type': 'integer', 'minimum': 1, 'maximum': 10000}},
                    ],
                    'responses': {'200': {'description': 'Combined stock and mutual fund trades'}, '400': {'description': 'Invalid query parameter'}},
                }
            },
            '/api/trades/stocks': {
                'get': {
                    'summary': 'List stock and ETF trades',
                    'responses': {'200': {'description': 'Stock trade rows'}},
                },
                'post': {
                    'summary': 'Create a stock or ETF trade',
                    'requestBody': {
                        'required': True,
                        'content': {
                            'application/json': {
                                'schema': {
                                    'type': 'object',
                                    'required': ['symbol', 'name', 'trade_type', 'quantity', 'price', 'currency', 'trade_date', 'broker', 'account_name', 'tax_status'],
                                    'properties': {
                                        'symbol': {'type': 'string', 'example': 'VOO'},
                                        'name': {'type': 'string', 'example': 'Vanguard S&P 500 ETF'},
                                        'trade_type': {'type': 'string', 'enum': ['BUY', 'SELL']},
                                        'quantity': {'type': 'number', 'exclusiveMinimum': 0},
                                        'price': {'type': 'number', 'minimum': 0},
                                        'currency': {'type': 'string', 'enum': ['USD', 'JPY']},
                                        'trade_date': {'type': 'string', 'format': 'date'},
                                        'broker': {'type': 'string'},
                                        'account_name': {'type': 'string'},
                                        'tax_status': {'type': 'string'},
                                        'fx_rate': {'type': ['number', 'null'], 'exclusiveMinimum': 0},
                                        'fee_amount': {'type': ['number', 'null'], 'minimum': 0},
                                        'fee_currency': {'type': ['string', 'null'], 'enum': ['USD', 'JPY', None]},
                                    },
                                }
                            }
                        },
                    },
                    'responses': {'201': {'description': 'Stock trade created'}, '400': {'description': 'Validation errors'}, '415': {'description': 'JSON required'}},
                },
            },
            '/api/trades/stocks/{trade_id}': {
                'get': {
                    'summary': 'Get one stock or ETF trade',
                    'parameters': [{'name': 'trade_id', 'in': 'path', 'required': True, 'schema': {'type': 'integer'}}],
                    'responses': {'200': {'description': 'Stock trade row'}, '404': {'description': 'Trade not found'}},
                }
            },
            '/api/trades/mutual-funds': {
                'get': {
                    'summary': 'List mutual fund trades',
                    'responses': {'200': {'description': 'Mutual fund trade rows'}},
                },
                'post': {
                    'summary': 'Create a mutual fund trade',
                    'requestBody': {
                        'required': True,
                        'content': {
                            'application/json': {
                                'schema': {
                                    'type': 'object',
                                    'required': ['fund_code', 'fund_name', 'transaction_type', 'executed_units', 'nav_per_10000', 'trade_date', 'broker', 'account_name', 'tax_status'],
                                    'properties': {
                                        'fund_code': {'type': 'string', 'example': '0331418A'},
                                        'fund_name': {'type': 'string'},
                                        'transaction_type': {'type': 'string', 'enum': ['BUY', 'SELL']},
                                        'executed_units': {'type': 'number', 'exclusiveMinimum': 0},
                                        'nav_per_10000': {'type': 'number', 'minimum': 0},
                                        'trade_date': {'type': 'string', 'format': 'date'},
                                        'broker': {'type': 'string'},
                                        'account_name': {'type': 'string'},
                                        'tax_status': {'type': 'string'},
                                    },
                                }
                            }
                        },
                    },
                    'responses': {'201': {'description': 'Mutual fund trade created'}, '400': {'description': 'Validation errors'}, '415': {'description': 'JSON required'}},
                },
            },
            '/api/trades/mutual-funds/{trade_id}': {
                'get': {
                    'summary': 'Get one mutual fund trade',
                    'parameters': [{'name': 'trade_id', 'in': 'path', 'required': True, 'schema': {'type': 'integer'}}],
                    'responses': {'200': {'description': 'Mutual fund trade row'}, '404': {'description': 'Trade not found'}},
                }
            },
            '/api/dividends': {
                'get': {
                    'summary': 'List dividend income records',
                    'parameters': [
                        {'name': 'year', 'in': 'query', 'schema': {'type': 'string', 'default': 'all'}, 'description': 'YYYY or all'},
                        {'name': 'broker', 'in': 'query', 'schema': {'type': 'string', 'default': 'all'}},
                        {'name': 'account_name', 'in': 'query', 'schema': {'type': 'string', 'default': 'all'}},
                        {'name': 'tax_status', 'in': 'query', 'schema': {'type': 'string', 'default': 'all'}},
                        {'name': 'tax_treatment', 'in': 'query', 'schema': {'type': 'string', 'enum': ['all', 'undecided', 'not_filed', 'aggregate', 'separate'], 'default': 'all'}},
                        {'name': 'limit', 'in': 'query', 'schema': {'type': 'integer', 'minimum': 1, 'maximum': 10000}},
                    ],
                    'responses': {'200': {'description': 'Dividend rows and JPY totals'}, '400': {'description': 'Invalid query parameter'}},
                },
                'post': {
                    'summary': 'Create a dividend income record',
                    'requestBody': {
                        'required': True,
                        'content': {
                            'application/json': {
                                'schema': {
                                    'type': 'object',
                                    'required': ['symbol', 'name', 'payment_date', 'currency', 'broker', 'account_name', 'tax_status'],
                                    'properties': {
                                        'symbol': {'type': 'string', 'example': 'VOO'},
                                        'name': {'type': 'string', 'example': 'Vanguard S&P 500 ETF'},
                                        'payment_date': {'type': 'string', 'format': 'date'},
                                        'currency': {'type': 'string', 'enum': ['USD', 'JPY']},
                                        'gross_amount': {'type': ['number', 'null'], 'minimum': 0},
                                        'tax_withheld': {'type': ['number', 'null'], 'minimum': 0},
                                        'foreign_tax_withheld': {'type': ['number', 'null'], 'minimum': 0},
                                        'japanese_income_tax_withheld': {'type': ['number', 'null'], 'minimum': 0},
                                        'japanese_local_tax_withheld': {'type': ['number', 'null'], 'minimum': 0},
                                        'deductible_interest': {'type': ['number', 'null'], 'minimum': 0},
                                        'quantity': {'type': ['number', 'null'], 'minimum': 0},
                                        'amount_per_share': {'type': ['number', 'null'], 'minimum': 0},
                                        'source_country': {'type': 'string'},
                                        'security_type': {'type': 'string', 'enum': ['listed_stock', 'etf', 'mutual_fund', 'other']},
                                        'tax_treatment': {'type': 'string', 'enum': ['undecided', 'not_filed', 'aggregate', 'separate']},
                                        'broker': {'type': 'string'},
                                        'account_name': {'type': 'string'},
                                        'tax_status': {'type': 'string'},
                                        'fx_rate': {'type': ['number', 'null'], 'exclusiveMinimum': 0},
                                        'notes': {'type': 'string'},
                                    },
                                }
                            }
                        },
                    },
                    'responses': {'201': {'description': 'Dividend created'}, '400': {'description': 'Validation errors'}, '415': {'description': 'JSON required'}},
                },
            },
            '/api/dividends/{dividend_id}': {
                'get': {
                    'summary': 'Get one dividend income record',
                    'parameters': [{'name': 'dividend_id', 'in': 'path', 'required': True, 'schema': {'type': 'integer'}}],
                    'responses': {'200': {'description': 'Dividend row'}, '404': {'description': 'Dividend not found'}},
                },
                'put': {
                    'summary': 'Replace a dividend income record',
                    'parameters': [{'name': 'dividend_id', 'in': 'path', 'required': True, 'schema': {'type': 'integer'}}],
                    'responses': {'200': {'description': 'Dividend updated'}, '400': {'description': 'Validation errors'}, '404': {'description': 'Dividend not found'}, '415': {'description': 'JSON required'}},
                },
                'patch': {
                    'summary': 'Update a dividend income record',
                    'parameters': [{'name': 'dividend_id', 'in': 'path', 'required': True, 'schema': {'type': 'integer'}}],
                    'responses': {'200': {'description': 'Dividend updated'}, '400': {'description': 'Validation errors'}, '404': {'description': 'Dividend not found'}, '415': {'description': 'JSON required'}},
                },
                'delete': {
                    'summary': 'Delete a dividend income record',
                    'parameters': [{'name': 'dividend_id', 'in': 'path', 'required': True, 'schema': {'type': 'integer'}}],
                    'responses': {'200': {'description': 'Dividend deleted'}, '404': {'description': 'Dividend not found'}},
                },
            },
            '/api/version': {
                'get': {
                    'summary': 'Get application version status',
                    'responses': {'200': {'description': 'Version status'}},
                }
            },
            '/api/openapi.json': {
                'get': {
                    'summary': 'Get this OpenAPI document',
                    'responses': {'200': {'description': 'OpenAPI document'}},
                }
            },
        },
    }

@app.route('/api/openapi.json')
def api_openapi():
    return jsonify(_openapi_spec())

@app.route('/api/trades')
def api_trades():
    """API endpoint for trade-level stock and mutual fund data."""
    instrument_type = request.args.get('instrument_type', 'all')
    limit = request.args.get('limit')
    try:
        limit = int(limit) if limit else None
    except ValueError:
        return jsonify({'error': 'limit must be an integer'}), 400
    if limit is not None:
        limit = max(1, min(limit, 10000))

    stock_trades = []
    mutual_fund_trades = []
    with sqlite3.connect(DATABASE) as conn:
        conn.row_factory = sqlite3.Row
        if instrument_type in ['all', 'stock']:
            query = 'SELECT * FROM trades ORDER BY trade_date DESC, id DESC'
            if limit:
                query += ' LIMIT ?'
                stock_rows = conn.execute(query, (limit,)).fetchall()
            else:
                stock_rows = conn.execute(query).fetchall()
            stock_trades = [_stock_trade_api_row(row) for row in stock_rows]
        if instrument_type in ['all', 'mutual_fund']:
            query = 'SELECT * FROM mutual_fund_trades ORDER BY trade_date DESC, id DESC'
            if limit:
                query += ' LIMIT ?'
                fund_rows = conn.execute(query, (limit,)).fetchall()
            else:
                fund_rows = conn.execute(query).fetchall()
            mutual_fund_trades = [_mutual_fund_trade_api_row(row) for row in fund_rows]

    if instrument_type not in ['all', 'stock', 'mutual_fund']:
        return jsonify({'error': 'instrument_type must be all, stock, or mutual_fund'}), 400

    combined_trades = sorted(
        stock_trades + mutual_fund_trades,
        key=lambda trade: (trade.get('trade_date') or '', trade.get('id') or 0),
        reverse=True
    )
    if limit and instrument_type == 'all':
        combined_trades = combined_trades[:limit]

    return jsonify({
        'meta': {
            'generated_at': _portfolio_now().strftime('%Y-%m-%d %H:%M:%S'),
            'instrument_type': instrument_type,
            'limit': limit,
            'count': len(combined_trades),
        },
        'trades': combined_trades,
        'stock_trades': stock_trades,
        'mutual_fund_trades': mutual_fund_trades,
    })

@app.route('/api/trades/stocks', methods=['GET', 'POST'])
def api_stock_trades():
    """API endpoint to list or create stock/ETF trades."""
    if request.method == 'GET':
        with sqlite3.connect(DATABASE) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute('SELECT * FROM trades ORDER BY trade_date DESC, id DESC').fetchall()
        trades = [_stock_trade_api_row(row) for row in rows]
        return jsonify({'count': len(trades), 'trades': trades})

    if not request.is_json:
        return jsonify({'error': 'Request body must be JSON'}), 415
    values, errors = _parse_trade_form(_json_to_form_payload(request.get_json(silent=True)))
    if errors:
        return jsonify({'errors': errors}), 400
    trade_id = _insert_stock_trade(values)
    row = _fetch_stock_trade_row(trade_id)
    return jsonify({'message': 'Stock trade created.', 'trade': _stock_trade_api_row(row)}), 201

@app.route('/api/trades/stocks/<int:trade_id>')
def api_stock_trade(trade_id):
    """API endpoint to retrieve one stock/ETF trade."""
    row = _fetch_stock_trade_row(trade_id)
    if row is None:
        return jsonify({'error': 'Stock trade not found'}), 404
    return jsonify(_stock_trade_api_row(row))

@app.route('/api/trades/mutual-funds', methods=['GET', 'POST'])
def api_mutual_fund_trades():
    """API endpoint to list or create mutual fund trades."""
    if request.method == 'GET':
        with sqlite3.connect(DATABASE) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute('SELECT * FROM mutual_fund_trades ORDER BY trade_date DESC, id DESC').fetchall()
        trades = [_mutual_fund_trade_api_row(row) for row in rows]
        return jsonify({'count': len(trades), 'trades': trades})

    if not request.is_json:
        return jsonify({'error': 'Request body must be JSON'}), 415
    values, errors = _parse_mutual_fund_trade_form(_json_to_form_payload(request.get_json(silent=True)))
    if errors:
        return jsonify({'errors': errors}), 400
    trade_id = _insert_mutual_fund_trade(values)
    row = _fetch_mutual_fund_trade_row(trade_id)
    return jsonify({'message': 'Mutual fund trade created.', 'trade': _mutual_fund_trade_api_row(row)}), 201

@app.route('/api/trades/mutual-funds/<int:trade_id>')
def api_mutual_fund_trade(trade_id):
    """API endpoint to retrieve one mutual fund trade."""
    row = _fetch_mutual_fund_trade_row(trade_id)
    if row is None:
        return jsonify({'error': 'Mutual fund trade not found'}), 404
    return jsonify(_mutual_fund_trade_api_row(row))

@app.route('/api/dividends', methods=['GET', 'POST'])
def api_dividends():
    """API endpoint to list or create dividend income records."""
    if request.method == 'GET':
        limit = request.args.get('limit')
        filters = {
            'year': request.args.get('year', 'all'),
            'broker': request.args.get('broker', 'all'),
            'account_name': request.args.get('account_name', 'all'),
            'tax_status': request.args.get('tax_status', 'all'),
            'tax_treatment': request.args.get('tax_treatment', 'all'),
        }
        try:
            limit = int(limit) if limit else None
        except ValueError:
            return jsonify({'error': 'limit must be an integer'}), 400
        if limit is not None:
            limit = max(1, min(limit, 10000))
        if filters['year'] != 'all':
            if not re.fullmatch(r'\d{4}', filters['year']):
                return jsonify({'error': 'year must be YYYY'}), 400
        if filters['tax_treatment'] not in ['all', 'undecided', 'not_filed', 'aggregate', 'separate']:
            return jsonify({'error': 'tax_treatment must be all, undecided, not_filed, aggregate, or separate'}), 400

        where, params = _dividend_filter_query(filters)
        query = 'SELECT * FROM dividends'
        if where:
            query += ' WHERE ' + ' AND '.join(where)
        query += ' ORDER BY payment_date DESC, id DESC'
        if limit:
            query += ' LIMIT ?'
            params.append(limit)

        with sqlite3.connect(DATABASE) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(query, params).fetchall()
        dividends = [_dividend_api_row(row) for row in rows]
        totals = {
            'gross_jpy': sum(d['gross_amount_jpy'] or 0 for d in dividends),
            'tax_jpy': sum(d['tax_withheld_jpy'] or 0 for d in dividends),
            'net_jpy': sum(d['net_amount_jpy'] or 0 for d in dividends),
        }
        return jsonify({'count': len(dividends), 'filters': filters, 'totals': totals, 'dividends': dividends})

    if not request.is_json:
        return jsonify({'error': 'Request body must be JSON'}), 415
    values, errors = _parse_dividend_form(_json_to_form_payload(request.get_json(silent=True)))
    if errors:
        return jsonify({'errors': errors}), 400
    dividend_id = _insert_dividend(values)
    row = _fetch_dividend_row(dividend_id)
    return jsonify({'message': 'Dividend created.', 'dividend': _dividend_api_row(row)}), 201

@app.route('/api/dividends/<int:dividend_id>', methods=['GET', 'PUT', 'PATCH', 'DELETE'])
def api_dividend(dividend_id):
    """API endpoint to retrieve, update, or delete one dividend income record."""
    row = _fetch_dividend_row(dividend_id)
    if row is None:
        return jsonify({'error': 'Dividend not found'}), 404

    if request.method == 'GET':
        return jsonify(_dividend_api_row(row))
    if request.method == 'DELETE':
        with sqlite3.connect(DATABASE) as conn:
            conn.execute('DELETE FROM dividends WHERE id = ?', (dividend_id,))
        return jsonify({'message': 'Dividend deleted.'})

    if not request.is_json:
        return jsonify({'error': 'Request body must be JSON'}), 415
    payload = dict(row)
    payload.update(request.get_json(silent=True) or {})
    values, errors = _parse_dividend_form(_json_to_form_payload(payload))
    if errors:
        return jsonify({'errors': errors}), 400
    _update_dividend(dividend_id, values)
    row = _fetch_dividend_row(dividend_id)
    return jsonify({'message': 'Dividend updated.', 'dividend': _dividend_api_row(row)})

@app.route('/api/version')
def api_version():
    return jsonify(get_app_version_status())

def generate_tax_report_data(year, broker_filter=None, account_name_filter=None, tax_status_filter=None):
    """
    Generates a tax report for a given year using the moving-average cost basis method.
    All calculations are performed in JPY.
    """
    trades = _fetch_normalized_trades()
    if broker_filter:
        trades = [trade for trade in trades if trade.get('broker') == broker_filter]
    if account_name_filter:
        trades = [trade for trade in trades if trade.get('account_name') == account_name_filter]
    if tax_status_filter:
        trades = [trade for trade in trades if trade.get('tax_status') == tax_status_filter]

    holdings = {}  # Tracks the moving-average cost for each stock
    buy_history = {} # Tracks all buy transactions for the breakdown
    sales_report = []

    for trade in trades:
        symbol = trade['symbol']
        holding_key = (
            trade['symbol'],
            trade['broker'],
            trade.get('account_name') or 'Default',
            trade.get('tax_status') or 'Taxable',
            trade['instrument_type']
        )
        trade_year = int(trade['trade_date'][:4])

        if holding_key not in holdings:
            holdings[holding_key] = {
                'quantity': 0, 
                'total_cost_jpy': 0,
                'total_cost_native': 0,
                'last_purchase_date': None
            }
            buy_history[holding_key] = []

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

            holdings[holding_key]['quantity'] += trade['quantity']
            holdings[holding_key]['total_cost_jpy'] += cost_jpy
            holdings[holding_key]['total_cost_native'] += cost_native
            holdings[holding_key]['last_purchase_date'] = trade['trade_date']

            buy_history[holding_key].append({
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
            current_holding = holdings[holding_key]
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
                    'account_name': trade.get('account_name') or 'Default',
                    'tax_status': trade.get('tax_status') or 'Taxable',
                    'selling_fee_jpy': fee_jpy,
                    'last_purchase_date': holdings[holding_key]['last_purchase_date'],
                    # --- Additions for breakdown ---
                    'avg_cost_per_share_jpy': _display_price_basis(avg_cost_jpy, trade['instrument_type']),
                    'avg_cost_per_share_native': _display_price_basis(avg_cost_native, trade['instrument_type']),
                    'sale_price_native': trade['price'],
                    'sale_currency': trade['currency'],
                    'sale_fx_rate': trade['fx_rate'],
                    'acquisition_history': list(buy_history[holding_key])
                })

            # Update holdings after the sale
            holdings[holding_key]['quantity'] -= trade['quantity']
            holdings[holding_key]['total_cost_jpy'] -= cost_of_sale_jpy
            cost_of_sale_native = trade['quantity'] * avg_cost_native
            holdings[holding_key]['total_cost_native'] -= cost_of_sale_native

    return {
        'sales': sales_report,
        'total_proceeds_jpy': sum(s['proceeds_jpy'] for s in sales_report),
        'total_cost_basis_jpy': sum(s['cost_basis_jpy'] for s in sales_report),
        'total_pnl_jpy': sum(s['pnl_jpy'] for s in sales_report),
        'year': year,
        'selected_broker': broker_filter or 'all',
        'selected_account_name': account_name_filter or 'all',
        'selected_tax_status': tax_status_filter or 'all'
    }

@app.route('/trades')
def list_trades():
    """Displays a list of all trades."""
    with sqlite3.connect(DATABASE) as conn:
        conn.row_factory = sqlite3.Row
        trade_rows = conn.execute('SELECT * FROM trades ORDER BY trade_date DESC').fetchall()

    today = _portfolio_now().date()
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
    config_options = get_config_options()
    with sqlite3.connect(DATABASE) as conn:
        # Get distinct years from trades to populate the dropdown
        years_cursor = conn.execute(
            """
            SELECT year FROM (
                SELECT DISTINCT SUBSTR(trade_date, 1, 4) as year FROM trades
                UNION
                SELECT DISTINCT SUBSTR(trade_date, 1, 4) as year FROM mutual_fund_trades
                UNION
                SELECT DISTINCT SUBSTR(payment_date, 1, 4) as year FROM dividends
            ) ORDER BY year DESC
            """
        )
        available_years = [row[0] for row in years_cursor]

    report_data = None
    if request.method == 'POST':
        _validate_csrf_token()
        selected_year = request.form.get('year')
        selected_broker = request.form.get('broker', 'all')
        selected_account_name = request.form.get('account_name', 'all')
        selected_tax_status = request.form.get('tax_status', 'all')
        if selected_year:
            report_data = generate_tax_report_data(
                int(selected_year),
                selected_broker if selected_broker != 'all' else None,
                selected_account_name if selected_account_name != 'all' else None,
                selected_tax_status if selected_tax_status != 'all' else None
            )
    
    return render_template(
        'tax_report.html',
        years=available_years,
        report_data=report_data,
        brokers=config_options['broker'],
        account_names=config_options['account_name'],
        tax_statuses=config_options['tax_status']
    )

@app.route('/corporate_actions', methods=['GET', 'POST'])
def corporate_actions():
    preview_rows = None
    values = {
        'symbol': '',
        'currency': 'JPY',
        'effective_date': _portfolio_day_str(),
        'ratio': '',
        'price_override': '',
        'notes': '',
    }

    if request.method == 'POST':
        _validate_csrf_token()
        action = request.form.get('action')
        parsed_values, errors = _parse_stock_split_form(request.form)
        values.update({
            'symbol': parsed_values['symbol'],
            'currency': parsed_values['currency'],
            'effective_date': parsed_values['effective_date'],
            'ratio': parsed_values['ratio'] if parsed_values['ratio'] is not None else '',
            'price_override': parsed_values['price_override'] if parsed_values['price_override'] is not None else '',
            'notes': parsed_values['notes'],
        })

        if errors:
            for error in errors:
                flash(error, 'danger')
        elif action == 'preview_stock_split':
            if _stock_split_already_applied(parsed_values):
                flash('This stock split has already been recorded.', 'warning')
            preview_rows = _preview_stock_split(parsed_values)
            if not preview_rows:
                flash('No matching pre-effective-date stock trades found.', 'warning')
        elif action == 'apply_stock_split':
            if request.form.get('confirm') != 'yes':
                flash('Review the preview and tick the confirmation box before applying.', 'danger')
                preview_rows = _preview_stock_split(parsed_values)
            else:
                try:
                    affected_count = _apply_stock_split(parsed_values)
                    flash(
                        f"Applied {parsed_values['ratio']:g}-for-1 split to {affected_count} {parsed_values['symbol']} trades.",
                        'success'
                    )
                    values = {
                        'symbol': '',
                        'currency': 'JPY',
                        'effective_date': _portfolio_day_str(),
                        'ratio': '',
                        'price_override': '',
                        'notes': '',
                    }
                except ValueError as error:
                    flash(str(error), 'danger')
                    preview_rows = _preview_stock_split(parsed_values)
        else:
            flash('Unknown corporate action.', 'danger')

    return render_template(
        'corporate_actions.html',
        values=values,
        preview_rows=preview_rows,
        actions=_fetch_corporate_actions(),
        current_date=_portfolio_day_str()
    )

@app.route('/data_maintenance', methods=['GET', 'POST'])
def data_maintenance():
    """Maintains cached market data and portfolio history snapshots."""
    if request.method == 'POST':
        _validate_csrf_token()
        action = request.form.get('action')

        if action == 'clear_market_cache':
            cache.clear()
            flash('Market data cache cleared.', 'info')

        elif action == 'recalculate_today_history':
            cache.clear()
            try:
                summary = _recalculate_today_history_snapshot()
                flash(
                    f"Today's portfolio history snapshot recalculated: ¥{summary['total_value_jpy']:,.0f}.",
                    'success'
                )
            except RuntimeError as error:
                flash(str(error), 'danger')

        elif action == 'update_history':
            date_value = request.form.get('date', '').strip()
            if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', date_value):
                flash('History date must be YYYY-MM-DD.', 'danger')
            else:
                try:
                    values = {
                        'value_usd': _parse_optional_float_field(request.form, 'value_usd'),
                        'value_jpy': _parse_optional_float_field(request.form, 'value_jpy'),
                        'unrealized_pnl_usd': _parse_optional_float_field(request.form, 'unrealized_pnl_usd'),
                        'unrealized_pnl_jpy': _parse_optional_float_field(request.form, 'unrealized_pnl_jpy'),
                    }
                except ValueError:
                    flash('History values must be numeric or blank.', 'danger')
                else:
                    with sqlite3.connect(DATABASE) as conn:
                        cursor = conn.execute(
                            """
                            UPDATE portfolio_history
                            SET value_usd = ?, value_jpy = ?, unrealized_pnl_usd = ?, unrealized_pnl_jpy = ?
                            WHERE date = ?
                            """,
                            (
                                values['value_usd'],
                                values['value_jpy'],
                                values['unrealized_pnl_usd'],
                                values['unrealized_pnl_jpy'],
                                date_value
                            )
                        )
                    if cursor.rowcount:
                        flash(f'Portfolio history for {date_value} updated.', 'success')
                    else:
                        flash(f'No portfolio history row found for {date_value}.', 'warning')

        elif action == 'delete_history':
            date_value = request.form.get('date', '').strip()
            if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', date_value):
                flash('History date must be YYYY-MM-DD.', 'danger')
            else:
                with sqlite3.connect(DATABASE) as conn:
                    cursor = conn.execute('DELETE FROM portfolio_history WHERE date = ?', (date_value,))
                if cursor.rowcount:
                    flash(f'Portfolio history for {date_value} deleted.', 'success')
                else:
                    flash(f'No portfolio history row found for {date_value}.', 'warning')

        elif action == 'save_price_override':
            values, errors = _parse_price_override_form(request.form)
            if errors:
                for error in errors:
                    flash(error, 'danger')
            else:
                with sqlite3.connect(DATABASE) as conn:
                    conn.execute(
                        """
                        INSERT INTO market_price_overrides (
                            symbol, currency, instrument_type, current_price, change_today,
                            latest_data_at, notes, updated_at
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(symbol, currency, instrument_type) DO UPDATE SET
                            current_price = excluded.current_price,
                            change_today = excluded.change_today,
                            latest_data_at = excluded.latest_data_at,
                            notes = excluded.notes,
                            updated_at = excluded.updated_at
                        """,
                        (
                            values['symbol'],
                            values['currency'],
                            values['instrument_type'],
                            values['current_price'],
                            values['change_today'],
                            values['latest_data_at'],
                            values['notes'],
                            _portfolio_now().strftime('%Y-%m-%d %H:%M:%S')
                        )
                    )
                cache.clear()
                flash(f"Manual price override saved for {values['symbol']}.", 'success')

        elif action == 'delete_price_override':
            symbol = request.form.get('symbol', '').strip().upper()
            currency = request.form.get('currency', '').strip().upper()
            instrument_type = request.form.get('instrument_type', 'stock').strip() or 'stock'
            with sqlite3.connect(DATABASE) as conn:
                cursor = conn.execute(
                    """
                    DELETE FROM market_price_overrides
                    WHERE symbol = ? AND currency = ? AND instrument_type = ?
                    """,
                    (symbol, currency, instrument_type)
                )
            cache.clear()
            if cursor.rowcount:
                flash(f'Manual price override removed for {symbol}.', 'success')
            else:
                flash(f'No manual price override found for {symbol}.', 'warning')

        else:
            flash('Unknown maintenance action.', 'danger')

        return redirect(url_for('data_maintenance'))

    return render_template(
        'data_maintenance.html',
        current_date=_portfolio_day_str(),
        price_overrides=_fetch_market_price_overrides(),
        history_rows=_fetch_portfolio_history_rows(limit=None),
        fx_rates=_fetch_cached_fx_rates()
    )


@app.route('/add_trade', methods=['GET', 'POST'])
def add_trade():
    """Handles adding a new trade."""
    config_options = get_config_options()
    if request.method == 'POST':
        _validate_csrf_token()
        values, errors = _parse_trade_form(request.form)
        if errors:
            for error in errors:
                flash(error, 'danger')
            return render_template(
                'add_trade.html',
                today=values.get('trade_date') or _portfolio_day_str(),
                values=values,
                brokers=config_options['broker'],
                account_names=config_options['account_name'],
                tax_statuses=config_options['tax_status']
            )

        with sqlite3.connect(DATABASE) as conn:
            conn.execute(
                'INSERT INTO trades (symbol, name, trade_type, quantity, price, currency, trade_date, broker, account_name, tax_status, fx_rate, fee_amount, fee_currency) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (
                    values['symbol'],
                    values['name'],
                    values['trade_type'],
                    values['quantity'],
                    values['price'],
                    values['currency'],
                    values['trade_date'],
                    values['broker'],
                    values['account_name'],
                    values['tax_status'],
                    values['fx_rate'],
                    values['fee_amount'],
                    values['fee_currency']
                )
            )
        return redirect(url_for('list_trades'))
    return render_template(
        'add_trade.html',
        today=_portfolio_day_str(),
        values={},
        brokers=config_options['broker'],
        account_names=config_options['account_name'],
        tax_statuses=config_options['tax_status']
    )

@app.route('/mutual_funds')
def list_mutual_fund_trades():
    """Displays all mutual fund transactions."""
    with sqlite3.connect(DATABASE) as conn:
        conn.row_factory = sqlite3.Row
        trades = conn.execute('SELECT * FROM mutual_fund_trades ORDER BY trade_date DESC').fetchall()
    return render_template('mutual_fund_trades.html', trades=trades)

@app.route('/dividends')
def list_dividends():
    """Displays all dividend income records."""
    filter_options = _get_dividend_filter_options()
    selected_year = request.args.get('year') or _portfolio_day_str()[:4]
    selected_broker = request.args.get('broker', 'all')
    selected_account_name = request.args.get('account_name', 'all')
    selected_tax_status = request.args.get('tax_status', 'all')
    selected_tax_treatment = request.args.get('tax_treatment', 'all')
    if selected_year != 'all' and not re.fullmatch(r'\d{4}', selected_year):
        selected_year = _portfolio_day_str()[:4]
        flash('Invalid dividend year filter. Showing the current year.', 'warning')
    if selected_tax_treatment not in ['all', 'undecided', 'not_filed', 'aggregate', 'separate']:
        selected_tax_treatment = 'all'
        flash('Invalid dividend filing treatment filter. Showing all treatments.', 'warning')

    filters = {
        'year': selected_year,
        'broker': selected_broker,
        'account_name': selected_account_name,
        'tax_status': selected_tax_status,
        'tax_treatment': selected_tax_treatment,
    }
    where, params = _dividend_filter_query(filters)
    query = 'SELECT * FROM dividends'
    if where:
        query += ' WHERE ' + ' AND '.join(where)
    query += ' ORDER BY payment_date DESC, id DESC'

    with sqlite3.connect(DATABASE) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(query, params).fetchall()
    dividends = [_dividend_api_row(row) for row in rows]
    totals = {
        'gross_jpy': sum(dividend['gross_amount_jpy'] or 0 for dividend in dividends),
        'tax_jpy': sum(dividend['tax_withheld_jpy'] or 0 for dividend in dividends),
        'net_jpy': sum(dividend['net_amount_jpy'] or 0 for dividend in dividends),
    }
    return render_template(
        'dividends.html',
        dividends=dividends,
        totals=totals,
        dividend_income_summary=_calculate_dividend_income_summary(),
        years=filter_options['years'],
        brokers=filter_options['brokers'],
        account_names=filter_options['account_names'],
        tax_statuses=filter_options['tax_statuses'],
        selected_year=selected_year,
        selected_broker=selected_broker,
        selected_account_name=selected_account_name,
        selected_tax_status=selected_tax_status,
        selected_tax_treatment=selected_tax_treatment
    )

@app.route('/add_dividend', methods=['GET', 'POST'])
def add_dividend():
    """Handles adding a dividend income record."""
    config_options = get_config_options()
    if request.method == 'POST':
        _validate_csrf_token()
        values, errors = _parse_dividend_form(request.form)
        if errors:
            for error in errors:
                flash(error, 'danger')
            return render_template(
                'add_dividend.html',
                today=values.get('payment_date') or _portfolio_day_str(),
                values=values,
                brokers=config_options['broker'],
                account_names=config_options['account_name'],
                tax_statuses=config_options['tax_status']
            )

        _insert_dividend(values)
        flash('Dividend saved.', 'success')
        return redirect(url_for('list_dividends'))

    return render_template(
        'add_dividend.html',
        today=_portfolio_day_str(),
        values={},
        brokers=config_options['broker'],
        account_names=config_options['account_name'],
        tax_statuses=config_options['tax_status']
    )

@app.route('/edit_dividend/<int:dividend_id>', methods=['GET', 'POST'])
def edit_dividend(dividend_id):
    """Handles editing an existing dividend income record."""
    config_options = get_config_options()
    if request.method == 'POST':
        _validate_csrf_token()
        values, errors = _parse_dividend_form(request.form)
        if errors:
            for error in errors:
                flash(error, 'danger')
            return render_template(
                'edit_dividend.html',
                dividend_id=dividend_id,
                today=values.get('payment_date') or _portfolio_day_str(),
                values=values,
                brokers=config_options['broker'],
                account_names=config_options['account_name'],
                tax_statuses=config_options['tax_status']
            )

        if not _update_dividend(dividend_id, values):
            abort(404)
        flash('Dividend updated.', 'success')
        return redirect(url_for('list_dividends'))

    row = _fetch_dividend_row(dividend_id)
    if row is None:
        abort(404)
    return render_template(
        'edit_dividend.html',
        dividend_id=dividend_id,
        today=_portfolio_day_str(),
        values=dict(row),
        brokers=config_options['broker'],
        account_names=config_options['account_name'],
        tax_statuses=config_options['tax_status']
    )

@app.route('/delete_dividend/<int:dividend_id>', methods=['POST'])
def delete_dividend(dividend_id):
    _validate_csrf_token()
    with sqlite3.connect(DATABASE) as conn:
        cursor = conn.execute('DELETE FROM dividends WHERE id = ?', (dividend_id,))
    if cursor.rowcount == 0:
        abort(404)
    flash('Dividend deleted.', 'success')
    return redirect(url_for('list_dividends'))

@app.route('/add_mutual_fund_trade', methods=['GET', 'POST'])
def add_mutual_fund_trade():
    """Handles adding a Japanese mutual fund transaction."""
    config_options = get_config_options()
    if request.method == 'POST':
        _validate_csrf_token()
        values, errors = _parse_mutual_fund_trade_form(request.form)
        if errors:
            for error in errors:
                flash(error, 'danger')
            return render_template(
                'add_mutual_fund_trade.html',
                today=values.get('trade_date') or _portfolio_day_str(),
                values=values,
                brokers=config_options['broker'],
                account_names=config_options['account_name'],
                tax_statuses=config_options['tax_status']
            )

        with sqlite3.connect(DATABASE) as conn:
            conn.execute(
                """
                INSERT INTO mutual_fund_trades (
                    fund_code, fund_name, transaction_type, transaction_detail,
                    account_type, account_name, tax_status, currency, executed_units, nav_per_10000,
                    trade_date, settlement_date, settlement_amount, broker, fx_rate
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    values['fund_code'],
                    values['fund_name'],
                    values['transaction_type'],
                    values['transaction_detail'],
                    values['account_type'],
                    values['account_name'],
                    values['tax_status'],
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
        today=_portfolio_day_str(),
        values={},
        brokers=config_options['broker'],
        account_names=config_options['account_name'],
        tax_statuses=config_options['tax_status']
    )

@app.route('/edit_mutual_fund_trade/<int:trade_id>', methods=['GET', 'POST'])
def edit_mutual_fund_trade(trade_id):
    """Handles editing an existing mutual fund transaction."""
    config_options = get_config_options()
    if request.method == 'POST':
        _validate_csrf_token()
        values, errors = _parse_mutual_fund_trade_form(request.form)
        if errors:
            for error in errors:
                flash(error, 'danger')
            return render_template(
                'edit_mutual_fund_trade.html',
                trade_id=trade_id,
                today=values.get('trade_date') or _portfolio_day_str(),
                values=values,
                brokers=config_options['broker'],
                account_names=config_options['account_name'],
                tax_statuses=config_options['tax_status']
            )

        with sqlite3.connect(DATABASE) as conn:
            cursor = conn.execute(
                """
                UPDATE mutual_fund_trades
                SET fund_code = ?,
                    fund_name = ?,
                    transaction_type = ?,
                    transaction_detail = ?,
                    account_type = ?,
                    account_name = ?,
                    tax_status = ?,
                    currency = ?,
                    executed_units = ?,
                    nav_per_10000 = ?,
                    trade_date = ?,
                    settlement_date = ?,
                    settlement_amount = ?,
                    broker = ?,
                    fx_rate = ?
                WHERE id = ?
                """,
                (
                    values['fund_code'],
                    values['fund_name'],
                    values['transaction_type'],
                    values['transaction_detail'],
                    values['account_type'],
                    values['account_name'],
                    values['tax_status'],
                    values['currency'],
                    values['executed_units'],
                    values['nav_per_10000'],
                    values['trade_date'],
                    values['settlement_date'],
                    values['settlement_amount'],
                    values['broker'],
                    values['fx_rate'],
                    trade_id
                )
            )
            if cursor.rowcount == 0:
                abort(404)
        flash('Mutual fund transaction updated.', 'success')
        return redirect(url_for('list_mutual_fund_trades'))

    with sqlite3.connect(DATABASE) as conn:
        conn.row_factory = sqlite3.Row
        trade = conn.execute('SELECT * FROM mutual_fund_trades WHERE id = ?', (trade_id,)).fetchone()
    if trade is None:
        abort(404)

    values = dict(trade)
    values['account_name'] = values.get('account_name') or values.get('account_type') or 'Default'
    values['tax_status'] = values.get('tax_status') or 'Taxable'
    return render_template(
        'edit_mutual_fund_trade.html',
        trade_id=trade_id,
        today=values.get('trade_date') or _portfolio_day_str(),
        values=values,
        brokers=config_options['broker'],
        account_names=config_options['account_name'],
        tax_statuses=config_options['tax_status']
    )

@app.route('/edit_trade/<int:trade_id>', methods=['GET', 'POST'])
def edit_trade(trade_id):
    """Handles editing an existing trade."""
    config_options = get_config_options()
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
            return render_template(
                'edit_trade.html',
                trade=trade,
                brokers=config_options['broker'],
                account_names=config_options['account_name'],
                tax_statuses=config_options['tax_status']
            )

        with sqlite3.connect(DATABASE) as conn:
            conn.execute(
                'UPDATE trades SET symbol=?, name=?, trade_type=?, quantity=?, price=?, currency=?, trade_date=?, broker=?, account_name=?, tax_status=?, fx_rate=?, fee_amount=?, fee_currency=? WHERE id=?',
                (
                    values['symbol'],
                    values['name'],
                    values['trade_type'],
                    values['quantity'],
                    values['price'],
                    values['currency'],
                    values['trade_date'],
                    values['broker'],
                    values['account_name'],
                    values['tax_status'],
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
    return render_template(
        'edit_trade.html',
        trade=trade,
        brokers=config_options['broker'],
        account_names=config_options['account_name'],
        tax_statuses=config_options['tax_status']
    )

@app.route('/delete_mutual_fund_trade/<int:trade_id>', methods=['POST'])
def delete_mutual_fund_trade(trade_id):
    """Deletes a mutual fund transaction."""
    _validate_csrf_token()
    with sqlite3.connect(DATABASE) as conn:
        conn.execute('DELETE FROM mutual_fund_trades WHERE id = ?', (trade_id,))
    flash('Mutual fund transaction deleted.', 'success')
    return redirect(url_for('list_mutual_fund_trades'))

@app.route('/clone_mutual_fund_trade/<int:trade_id>', methods=['POST'])
def clone_mutual_fund_trade(trade_id):
    """Duplicates a mutual fund transaction."""
    _validate_csrf_token()
    with sqlite3.connect(DATABASE) as conn:
        cursor = conn.execute(
            """
            INSERT INTO mutual_fund_trades (
                fund_code, fund_name, transaction_type, transaction_detail,
                account_type, account_name, tax_status, currency, executed_units, nav_per_10000,
                trade_date, settlement_date, settlement_amount, broker, fx_rate
            )
            SELECT
                fund_code, fund_name, transaction_type, transaction_detail,
                account_type, account_name, tax_status, currency, executed_units, nav_per_10000,
                trade_date, settlement_date, settlement_amount, broker, fx_rate
            FROM mutual_fund_trades
            WHERE id = ?
            """,
            (trade_id,)
        )
        if cursor.rowcount == 0:
            abort(404)
    flash('Mutual fund transaction cloned.', 'success')
    return redirect(url_for('list_mutual_fund_trades'))

@app.route('/delete_trade/<int:trade_id>', methods=['POST'])
def delete_trade(trade_id):
    """Deletes a trade from the database."""
    _validate_csrf_token()
    with sqlite3.connect(DATABASE) as conn:
        conn.execute('DELETE FROM trades WHERE id = ?', (trade_id,))
    flash('Trade deleted.', 'success')
    return redirect(url_for('list_trades'))

@app.route('/clone_trade/<int:trade_id>', methods=['POST'])
def clone_trade(trade_id):
    """Duplicates a stock / ETF trade."""
    _validate_csrf_token()
    with sqlite3.connect(DATABASE) as conn:
        cursor = conn.execute(
            """
            INSERT INTO trades (
                symbol, name, trade_type, quantity, price, currency, trade_date,
                broker, account_name, tax_status, fx_rate, fee_amount, fee_currency
            )
            SELECT
                symbol, name, trade_type, quantity, price, currency, trade_date,
                broker, account_name, tax_status, fx_rate, fee_amount, fee_currency
            FROM trades
            WHERE id = ?
            """,
            (trade_id,)
        )
        if cursor.rowcount == 0:
            abort(404)
    flash('Trade cloned.', 'success')
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
        fieldnames = ['symbol', 'name', 'trade_type', 'quantity', 'price', 'currency', 'trade_date', 'broker', 'account_name', 'tax_status', 'fx_rate', 'fee_amount', 'fee_currency']
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

def _import_profile_form_values(profile=None):
    profile = profile or {}
    values = dict(profile)
    values['row_filters_text'] = json.dumps(profile.get('row_filters') or [], ensure_ascii=False, indent=2)
    values['mappings_text'] = json.dumps(profile.get('mappings') or {}, ensure_ascii=False, indent=2)
    values['defaults_text'] = json.dumps(profile.get('defaults') or {}, ensure_ascii=False, indent=2)
    return values

@app.route('/import_profiles')
def import_profiles():
    return render_template('import_profiles.html', profiles=_fetch_import_profiles())

@app.route('/import_profiles/add', methods=['GET', 'POST'])
def add_import_profile():
    if request.method == 'POST':
        _validate_csrf_token()
        profile, errors = _parse_import_profile_form(request.form)
        if errors:
            for error in errors:
                flash(error, 'danger')
            return render_template('import_profile_form.html', profile_id=None, values=_import_profile_form_values(profile))
        try:
            _insert_import_profile(profile)
            flash('Import profile saved.', 'success')
            return redirect(url_for('import_profiles'))
        except sqlite3.IntegrityError:
            flash('An import profile with that name already exists.', 'danger')
            return render_template('import_profile_form.html', profile_id=None, values=_import_profile_form_values(profile))
    return render_template('import_profile_form.html', profile_id=None, values=_import_profile_form_values())

@app.route('/import_profiles/<int:profile_id>/edit', methods=['GET', 'POST'])
def edit_import_profile(profile_id):
    profile = _fetch_import_profile(profile_id)
    if profile is None:
        abort(404)
    if request.method == 'POST':
        _validate_csrf_token()
        profile, errors = _parse_import_profile_form(request.form)
        if errors:
            for error in errors:
                flash(error, 'danger')
            return render_template('import_profile_form.html', profile_id=profile_id, values=_import_profile_form_values(profile))
        try:
            with sqlite3.connect(DATABASE) as conn:
                cursor = conn.execute(
                    """
                    UPDATE import_profiles
                    SET name = ?, instrument_type = ?, encoding = ?, header_row = ?, row_filters = ?, mappings = ?, defaults = ?
                    WHERE id = ?
                    """,
                    (
                        profile['name'],
                        profile['instrument_type'],
                        profile.get('encoding') or 'utf-8-sig',
                        int(profile.get('header_row') or 1),
                        json.dumps(profile.get('row_filters') or [], ensure_ascii=False),
                        json.dumps(profile.get('mappings') or {}, ensure_ascii=False),
                        json.dumps(profile.get('defaults') or {}, ensure_ascii=False),
                        profile_id,
                    )
                )
                if cursor.rowcount == 0:
                    abort(404)
            flash('Import profile updated.', 'success')
            return redirect(url_for('import_profiles'))
        except sqlite3.IntegrityError:
            flash('An import profile with that name already exists.', 'danger')
            return render_template('import_profile_form.html', profile_id=profile_id, values=_import_profile_form_values(profile))
    return render_template('import_profile_form.html', profile_id=profile_id, values=_import_profile_form_values(profile))

@app.route('/import_profiles/<int:profile_id>/delete', methods=['POST'])
def delete_import_profile(profile_id):
    _validate_csrf_token()
    with sqlite3.connect(DATABASE) as conn:
        cursor = conn.execute('DELETE FROM import_profiles WHERE id = ?', (profile_id,))
    if cursor.rowcount == 0:
        abort(404)
    flash('Import profile deleted.', 'success')
    return redirect(url_for('import_profiles'))

@app.route('/bulk_upload', methods=['GET', 'POST'])
def bulk_upload():
    profiles = _fetch_import_profiles()
    if request.method == 'POST':
        _validate_csrf_token()
        if 'file' not in request.files:
            flash('No file part in the request.', 'danger')
            return redirect(request.url)
        file = request.files['file']
        if file.filename == '':
            flash('No file selected for uploading.', 'danger')
            return redirect(request.url)
        selected_profile_ids = [int(profile_id) for profile_id in request.form.getlist('profile_ids') if profile_id.isdigit()]
        if not selected_profile_ids:
            flash('Choose at least one import profile.', 'danger')
            return redirect(request.url)
        if file and file.filename.endswith('.csv'):
            total_imported = 0
            profile_summaries = []
            duplicate_details = []
            all_errors = []
            for profile_id in selected_profile_ids:
                profile = _fetch_import_profile(profile_id)
                if not profile:
                    all_errors.append(f"Profile {profile_id} was not found.")
                    continue
                try:
                    result = _import_rows_with_profile(file, profile)
                    total_imported += result['imported']
                    profile_summaries.append(
                        f"{profile['name']}: {result['imported']} imported, {result['duplicates']} duplicates ignored, {result['skipped']} skipped"
                    )
                    duplicate_details.extend(
                        f"{profile['name']}: {detail}"
                        for detail in result['duplicate_details']
                    )
                    all_errors.extend(f"{profile['name']}: {error}" for error in result['errors'])
                except Exception as e:
                    all_errors.append(f"{profile['name']}: {e}")
            if all_errors:
                for error in all_errors[:20]:
                    flash(error, 'danger')
                if len(all_errors) > 20:
                    flash(f"{len(all_errors) - 20} additional import errors were hidden.", 'warning')
            for summary in profile_summaries:
                flash(summary, 'info')
            if duplicate_details:
                for detail in duplicate_details[:50]:
                    flash(f"Duplicate ignored: {detail}", 'secondary')
                if len(duplicate_details) > 50:
                    flash(f"{len(duplicate_details) - 50} additional duplicate records were hidden.", 'warning')
            if total_imported:
                flash(f'Successfully imported {total_imported} records.', 'success')
            return redirect(request.url)
        else:
            flash('Invalid file type. Please upload a CSV file.', 'warning')
            return redirect(request.url)

    return render_template('bulk_upload.html', profiles=profiles)

@app.route('/reconciliation', methods=['GET', 'POST'])
def reconciliation():
    """Reconciliation tool to compare broker files against database records."""
    profiles = _fetch_import_profiles()
    reconciliation_results = None
    selected_profile = None
    
    if request.method == 'POST':
        _validate_csrf_token()
        if 'file' not in request.files:
            flash('No file part in the request.', 'danger')
            return redirect(request.url)
        
        file = request.files['file']
        if file.filename == '':
            flash('No file selected for uploading.', 'danger')
            return redirect(request.url)
        
        profile_id = request.form.get('profile_id')
        if not profile_id or not profile_id.isdigit():
            flash('Please select an import profile.', 'danger')
            return redirect(request.url)
        
        selected_profile = _fetch_import_profile(int(profile_id))
        if not selected_profile:
            flash('Selected import profile not found.', 'danger')
            return redirect(request.url)
        
        if not file.filename.endswith('.csv'):
            flash('Please upload a CSV file.', 'warning')
            return redirect(request.url)
        
        try:
            # Parse broker file using the selected profile
            broker_records = []
            rows = _read_csv_rows_for_import(file, selected_profile)
            for index, row in enumerate(rows, start=int(selected_profile.get('header_row') or 1) + 1):
                if not _row_matches_import_profile(row, selected_profile):
                    continue
                values = _map_import_row(row, selected_profile)
                payload = _json_to_form_payload(values)
                
                try:
                    if selected_profile['instrument_type'] == 'stock':
                        parsed, row_errors = _parse_trade_form(payload)
                        if not row_errors:
                            broker_records.append(parsed)
                    elif selected_profile['instrument_type'] == 'mutual_fund':
                        parsed, row_errors = _parse_mutual_fund_trade_form(payload)
                        if not row_errors:
                            broker_records.append(parsed)
                    elif selected_profile['instrument_type'] == 'dividend':
                        parsed, row_errors = _parse_dividend_form(payload)
                        if not row_errors:
                            broker_records.append(parsed)
                except Exception as e:
                    flash(f"Row {index}: {e}", 'warning')
            
            if not broker_records:
                flash('No valid records found in broker file.', 'warning')
                return redirect(request.url)
            
            # Fetch database records
            if selected_profile['instrument_type'] == 'stock':
                db_records = _get_all_stock_trades()
                key_func = _make_trade_key
            elif selected_profile['instrument_type'] == 'mutual_fund':
                db_records = _get_all_mutual_fund_trades()
                key_func = _make_fund_key
            elif selected_profile['instrument_type'] == 'dividend':
                db_records = _get_all_dividends()
                key_func = _make_dividend_key
            else:
                flash('Unknown instrument type.', 'danger')
                return redirect(request.url)
            
            # Perform reconciliation
            reconciliation_results = _reconcile_records(broker_records, db_records, key_func)
            reconciliation_results['instrument_type'] = selected_profile['instrument_type']
            reconciliation_results['profile_name'] = selected_profile['name']
            
            flash(
                f"Reconciliation complete: {reconciliation_results['matched_count']} matched, "
                f"{reconciliation_results['missing_count']} missing from DB, "
                f"{reconciliation_results['extra_count']} extra in DB.",
                'info'
            )
        
        except Exception as e:
            flash(f'Reconciliation error: {str(e)}', 'danger')
            return redirect(request.url)
    
    return render_template(
        'reconciliation.html',
        profiles=profiles,
        reconciliation_results=reconciliation_results,
        selected_profile=selected_profile
    )

@app.route('/reconciliation/add-missing', methods=['POST'])
def reconciliation_add_missing():
    """Add a missing record from broker file to database."""
    _validate_csrf_token()
    
    record_json = request.form.get('record_json', '{}')
    instrument_type = request.form.get('instrument_type', '')
    
    try:
        record = json.loads(record_json)
        
        if instrument_type == 'stock':
            _insert_stock_trade(record)
            flash(f"Added stock trade: {record.get('symbol')} ({record.get('quantity')} @ {record.get('price')})", 'success')
        elif instrument_type == 'mutual_fund':
            _insert_mutual_fund_trade(record)
            flash(f"Added mutual fund: {record.get('fund_code')} ({record.get('executed_units')} units)", 'success')
        elif instrument_type == 'dividend':
            _insert_dividend(record)
            flash(f"Added dividend: {record.get('symbol')} (¥{record.get('gross_amount')})", 'success')
        else:
            flash('Unknown instrument type.', 'danger')
    
    except Exception as e:
        flash(f'Error adding record: {str(e)}', 'danger')
    
    return redirect(url_for('reconciliation'))

@app.route('/reconciliation/delete-extra', methods=['POST'])
def reconciliation_delete_extra():
    """Delete an extra record from database."""
    _validate_csrf_token()
    
    record_id = request.form.get('record_id')
    instrument_type = request.form.get('instrument_type', '')
    
    if not record_id or not record_id.isdigit():
        flash('Invalid record ID.', 'danger')
        return redirect(url_for('reconciliation'))
    
    try:
        with sqlite3.connect(DATABASE) as conn:
            if instrument_type == 'stock':
                cursor = conn.execute('DELETE FROM trades WHERE id = ?', (int(record_id),))
                flash('Stock trade deleted.', 'success')
            elif instrument_type == 'mutual_fund':
                cursor = conn.execute('DELETE FROM mutual_fund_trades WHERE id = ?', (int(record_id),))
                flash('Mutual fund trade deleted.', 'success')
            elif instrument_type == 'dividend':
                cursor = conn.execute('DELETE FROM dividends WHERE id = ?', (int(record_id),))
                flash('Dividend record deleted.', 'success')
            else:
                flash('Unknown instrument type.', 'danger')
                return redirect(url_for('reconciliation'))
            
            if not cursor.rowcount:
                flash('Record not found.', 'warning')
    
    except Exception as e:
        flash(f'Error deleting record: {str(e)}', 'danger')
    
    return redirect(url_for('reconciliation'))

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
