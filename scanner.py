"""HustleWatch Polymarket US moneyline evaluation; no bets placed."""
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from urllib.parse import quote
import requests

API = 'https://gateway.polymarket.us/v1'

class ScannerError(Exception):
    pass

def required_env(name):
    value = os.environ.get(name, '').strip()
    if not value:
        raise ScannerError(f'Missing secret: {name}')
    return value

def telegram_token(value):
    token = ''.join(value.split()).strip('"\'')
    if token.startswith('bot'):
        token = token[3:]
    if not re.fullmatch(r'[0-9]+:[A-Za-z0-9_-]+', token):
        raise ScannerError('Invalid TELEGRAM_BOT_TOKEN')
    return token

def request_json(method, url, service, **kwargs):
    for attempt in range(3):
        try:
            response = method(url, timeout=20, allow_redirects=False, **kwargs)
        except requests.RequestException:
            raise ScannerError(f'{service} connection failed') from None
        if response.status_code == 429 and attempt < 2:
            time.sleep(10 * (attempt + 1))
            continue
        if not 200 <= response.status_code < 300:
            raise ScannerError(f'{service} returned HTTP {response.status_code}')
        try:
            return response.json()
        except ValueError:
            raise ScannerError(f'{service} returned invalid JSON') from None
    raise ScannerError(f'{service} is rate limited')

def price(amount):
    try:
        if amount.get('currency') != 'USD':
            return None
        value = Decimal(amount['value'])
        return value if value.is_finite() and Decimal(0) < value < Decimal(1) else None
    except (AttributeError, KeyError, InvalidOperation, TypeError):
        return None

def send_telegram(token, chat_id, message):
    # Telegram's 4096-character limit must not silently remove ranked picks.
    chunks, chunk = [], ''
    for line in message.splitlines(keepends=True):
        while len(line) > 3900:
            if chunk:
                chunks.append(chunk); chunk = ''
            chunks.append(line[:3900]); line = line[3900:]
        if len(chunk) + len(line) > 3900:
            chunks.append(chunk); chunk = ''
        chunk += line
    if chunk:
        chunks.append(chunk)
    for chunk in chunks:
        result = request_json(requests.post, f'https://api.telegram.org/bot{token}/sendMessage',
            'Telegram', json={'chat_id':chat_id, 'text':chunk, 'disable_web_page_preview':True})
        if not isinstance(result, dict) or result.get('ok') is not True:
            raise ScannerError('Telegram did not confirm delivery')

def main():
    dry_run = '--dry-run' in sys.argv
    token = telegram_token(required_env('TELEGRAM_BOT_TOKEN')) if not dry_run else None
    chat_id = required_env('TELEGRAM_CHAT_ID') if not dry_run else None
    from sports_engine import run
    body, delivered = run(datetime.now(timezone.utc))
    if dry_run:
        print(body)
    else:
        send_telegram(token, chat_id, body)
        delivered()
    print('Sports evaluation completed')

if __name__ == '__main__':
    try:
        main()
    except ScannerError as exc:
        print(f'Scanner failed: {exc}', file=sys.stderr)
        sys.exit(1)

