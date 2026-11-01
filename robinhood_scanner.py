"""HustleWatch stock, options and crypto observations. Does not place trades."""
import os
import re
import sys
from datetime import datetime, timezone
import requests

API = 'https://data.alpaca.markets'
KEY = os.environ.get('ALPACA_API_KEY', '').strip()
SECRET = os.environ.get('ALPACA_SECRET_KEY', '').strip()
STOCKS = [x.strip().upper() for x in os.environ.get('WATCHLIST', 'AAPL,NVDA,TSLA,SPY,QQQ').split(',') if x.strip()]
CRYPTOS = ['BTC/USD', 'ETH/USD', 'SOL/USD']
TIMEOUT = 20


def get_json(path, params):
    response = requests.get(API + path, params=params, headers={
        'APCA-API-KEY-ID': KEY, 'APCA-API-SECRET-KEY': SECRET
    }, timeout=TIMEOUT)
    response.raise_for_status()
    return response.json()


def fresh(timestamp, max_age=240):
    if not timestamp:
        return False
    try:
        when = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
        age = (datetime.now(timezone.utc) - when).total_seconds()
        return -15 <= age <= max_age
    except (ValueError, TypeError, AttributeError):
        return False


def stock_alerts():
    data = get_json('/v2/stocks/snapshots', {'symbols': ','.join(STOCKS), 'feed': 'iex'})
    alerts = []
    for symbol in STOCKS:
        snap = data.get(symbol) or {}
        quote = snap.get('latestQuote') or {}
        prev = snap.get('prevDailyBar') or {}
        bid, ask, prior = quote.get('bp') or 0, quote.get('ap') or 0, prev.get('c') or 0
        if bid <= 0 or ask < bid or prior <= 0 or not fresh(quote.get('t')):
            continue
        move = (ask / prior - 1) * 100
        spread = (ask - bid) / ask * 100
        if abs(move) >= 2 and spread <= 0.35:
            alerts.append((abs(move), f'STOCK | {symbol}: ${ask:.2f} | {move:+.1f}% vs prior close | spread {spread:.2f}%'))
    return [message for _, message in sorted(alerts, reverse=True)[:3]]


def options_alerts():
    alerts = []
    for symbol in STOCKS[:3]:
        try:
            data = get_json(f'/v1beta1/options/snapshots/{symbol}', {'feed': 'indicative', 'limit': 100})
        except requests.HTTPError as exc:
            print(f'Options {symbol} unavailable: HTTP {exc.response.status_code}')
            continue
        picks = []
        for contract, snap in (data.get('snapshots') or {}).items():
            quote = snap.get('latestQuote') or {}
            bid, ask = quote.get('bp') or 0, quote.get('ap') or 0
            if bid <= 0 or ask < bid or ask <= 0:
                continue
            spread = (ask - bid) / ask * 100
            if spread > 10:
                continue
            tail = contract[-15:]
            if len(tail) != 15 or tail[6] not in 'CP' or not re.fullmatch(r'\d{6}[CP]\d{8}', tail):
                continue
            expiry = f'20{tail[:2]}-{tail[2:4]}-{tail[4:6]}'
            strike = int(tail[7:]) / 1000
            kind = 'CALL' if tail[6] == 'C' else 'PUT'
            picks.append((spread, f'OPTION | {symbol} {kind} ${strike:g} exp {expiry} | bid ${bid:.2f} ask ${ask:.2f} | spread {spread:.1f}%'))
        if picks:
            alerts.append(min(picks, key=lambda x: x[0])[1])
    return alerts


def crypto_alerts():
    try:
        data = get_json('/v1beta3/crypto/us/snapshots', {'symbols': ','.join(CRYPTOS)})
    except requests.HTTPError as exc:
        print(f'Crypto unavailable: HTTP {exc.response.status_code}')
        return []
    snapshots = data.get('snapshots') or {}
    alerts = []
    for symbol in CRYPTOS:
        quote = (snapshots.get(symbol) or {}).get('latestQuote') or {}
        bid, ask = quote.get('bp') or 0, quote.get('ap') or 0
        if bid > 0 and ask >= bid and fresh(quote.get('t'), max_age=600):
            alerts.append(f'CRYPTO | {symbol} | bid ${bid:,.2f} ask ${ask:,.2f}')
    return alerts


def telegram(message):
    token = ''.join(os.environ['TELEGRAM_BOT_TOKEN'].split()).strip('"\'')
    if token.startswith('bot'):
        token = token[3:]
    chat_id = os.environ['TELEGRAM_CHAT_ID'].strip()
    if os.environ.get('DRY_RUN') == '1':
        print(message)
        return
    response = requests.post(f'https://api.telegram.org/bot{token}/sendMessage', json={
        'chat_id': chat_id, 'text': message, 'disable_web_page_preview': True
    }, timeout=TIMEOUT)
    if not response.ok:
        # Do not print Telegram's full response: it could contain identifying data.
        print(f'Telegram HTTP status: {response.status_code}')
        try:
            print(f'Telegram error: {response.json().get("description", "unknown error")[:120]}')
        except (ValueError, AttributeError):
            print('Telegram returned a non-JSON error')
        response.raise_for_status()
    if not response.json().get('ok'):
        raise RuntimeError('Telegram rejected message')


def main():
    if not KEY or not SECRET:
        raise RuntimeError('Alpaca credentials missing')
    sections = []
    for name, scanner in [('stocks', stock_alerts), ('options', options_alerts), ('crypto', crypto_alerts)]:
        try:
            results = scanner()
            sections.extend(results)
            print(f'{name}: {len(results)} observations')
        except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
            print(f'{name} feed error: {type(exc).__name__}')
            sections.append(f'{name.upper()} | feed unavailable this scan')
    stamp = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
    message = 'HUSTLEWATCH | ' + stamp + '\nPaper reference: $500 | No orders placed\n\n'
    message += '\n'.join(sections) if sections else 'NO QUALIFYING OBSERVATIONS'
    message += '\n\nWATCH ONLY. Indicative option quotes are not executable; verify in Robinhood. No proven trading edge.'
    telegram(message[:3900])
    print('Telegram report sent')


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(f'Scanner failed: {type(exc).__name__}', file=sys.stderr)
        sys.exit(1)
