import sys, urllib.request
from datetime import datetime

def decode_raw(raw, header_charset=None):
    try:
        return raw.decode('utf-8')
    except Exception:
        if header_charset:
            try:
                return raw.decode(header_charset)
            except Exception:
                pass
        for enc in ('shift_jis','cp932','euc_jp','iso-2022-jp'):
            try:
                return raw.decode(enc)
            except Exception:
                pass
        return raw.decode('utf-8', errors='replace')

if __name__ == '__main__':
    if len(sys.argv) < 2:
        print('Usage: fetch_murc_sample.py YYYY-MM-DD')
        sys.exit(1)
    date_str = sys.argv[1]
    d = datetime.strptime(date_str, '%Y-%m-%d')
    date_param = d.strftime('%y%m%d')
    url = f'https://www.murc-kawasesouba.jp/fx/past/index.php?id={date_param}'
    headers = {'User-Agent':'Mozilla/5.0'}
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=10) as resp:
        raw = resp.read()
        try:
            header_charset = resp.headers.get_content_charset()
        except Exception:
            header_charset = None
    html = decode_raw(raw, header_charset)
    out = 'murc_sample.html'
    with open(out, 'w', encoding='utf-8') as f:
        f.write(html)
    print('Saved to', out)
    print(html[:2000])
