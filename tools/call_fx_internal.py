import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
import app

if __name__ == '__main__':
    date = '2024-01-15'
    print('Calling _fetch_jpy_usd_rate for', date)
    res = app._fetch_jpy_usd_rate(date)
    print(res)
