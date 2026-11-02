"""Source-linked stock/crypto news alerts. No orders or trade signals."""
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests
from scanner import ScannerError, request_json, required_env, send_telegram, telegram_token

API = 'https://data.alpaca.markets/v1beta1/news'
STATE = Path(__file__).with_name('news_state.json')


def stamp(value):
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return result if result.tzinfo else None
    except (ValueError, AttributeError, TypeError):
        return None


def load(path):
    if not path.exists():
        return {'version': 1, 'seen': {}}
    state = json.loads(path.read_text())
    if (state.get('version') != 1 or not isinstance(state.get('seen'), dict)
            or not all(isinstance(k, str) and stamp(v) for k, v in state['seen'].items())):
        raise ScannerError('Invalid news ledger; refusing to reset')
    return state


def save(path, state):
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + '\n')
    tmp.replace(path)


def fetch(now):
    headers = {'APCA-API-KEY-ID': required_env('ALPACA_API_KEY'),
               'APCA-API-SECRET-KEY': required_env('ALPACA_SECRET_KEY')}
    params = {'start': (now - timedelta(hours=2)).isoformat(),
              'sort': 'asc', 'limit': 50, 'include_content': 'false'}
    # Omit end: Alpaca chooses the latest time permitted by the account's feed.
    # Empty symbols means broad feed, including market-wide news.
    if os.getenv('NEWS_SYMBOLS', '').strip():
        params['symbols'] = os.environ['NEWS_SYMBOLS'].strip().upper()
    articles = []
    tokens = set()
    for _ in range(20):
        data = request_json(requests.get, API, 'Alpaca news', headers=headers, params=params.copy())
        if not isinstance(data, dict) or not isinstance(data.get('news'), list):
            raise ScannerError('Unexpected Alpaca news response')
        articles.extend(data['news'])
        token = data.get('next_page_token')
        if not token:
            return articles
        if not isinstance(token, str) or token in tokens:
            raise ScannerError('Repeated/invalid news pagination token')
        tokens.add(token)
        params['page_token'] = token
    raise ScannerError('News pagination incomplete; refusing to claim full coverage')


def select(articles, state, now):
    pending = {}
    for article in articles:
        if not isinstance(article, dict):
            raise ScannerError('Invalid news article')
        published = stamp(article.get('created_at'))
        updated = stamp(article.get('updated_at')) or published
        identity = article.get('id')
        if not published or not updated or identity is None:
            raise ScannerError('News article missing identity/timestamp')
        age = (now - updated).total_seconds()
        if not -60 <= age <= 3600 or published > now + timedelta(seconds=60):
            continue
        key = f'{identity}:{updated.isoformat()}'
        if key in state['seen']:
            continue
        url = article.get('url', '')
        if not isinstance(url, str):
            raise ScannerError('Invalid news source link')
        parsed = urlparse(url)
        if parsed.scheme not in ('https', 'http') or not parsed.netloc or parsed.username:
            raise ScannerError('Missing/unsafe news source link')
        headline = ' '.join(str(article.get('headline', '')).split())
        if not headline:
            raise ScannerError('Missing news headline')
        symbols = article.get('symbols') or []
        if not isinstance(symbols, list):
            raise ScannerError('Invalid news symbols')
        message = ('HUSTLEWATCH | NEWS, NOT A TRADE SIGNAL\n'
                   + headline[:240] + '\n'
                   + 'Symbols: ' + ', '.join(str(s)[:20] for s in symbols[:12]) + '\n'
                   + 'Source: ' + str(article.get('source', 'Alpaca feed'))[:60] + '\n'
                   + 'Published: ' + published.isoformat() + '\n'
                   + ('Updated: ' + updated.isoformat() + '\n' if updated != published else '')
                   + f'Feed age: {max(0, int(age/60))} min; access may be delayed 15 min.\n'
                   + url[:1800] + '\nReported headline; verify source before acting.')
        pending[key] = (updated, message)
    return sorted(pending.items(), key=lambda pair: pair[1][0])


def run(now, path=STATE, dry_run=False):
    state = load(path)
    pending = select(fetch(now), state, now)
    # Oldest first; excess eligible stories remain unseen for the next run.
    batch = pending[:10]
    token = telegram_token(required_env('TELEGRAM_BOT_TOKEN')) if batch and not dry_run else None
    chat = required_env('TELEGRAM_CHAT_ID') if batch and not dry_run else None
    for key, (_, message) in batch:
        if dry_run:
            print(message)
        else:
            send_telegram(token, chat, message)
            # A failed send is retried next scan; only accepted messages are marked.
            # A crash after send but before save can repeat one alert (at least once).
            state['seen'][key] = now.isoformat()
            save(path, state)
    if not dry_run:
        state['seen'] = {key: value for key, value in state['seen'].items()
                         if now - stamp(value) < timedelta(hours=48)}
        state['last_scan'] = {'time': now.isoformat(), 'sent': len(batch),
                              'pending': len(pending) - len(batch)}
        save(path, state)
    print(f'News: eligible={len(pending)} {"previewed" if dry_run else "accepted"}={len(batch)}; '
          f'pending={len(pending)-len(batch)}; provider coverage/latency not guaranteed')


if __name__ == '__main__':
    try:
        run(datetime.now(timezone.utc), dry_run='--dry-run' in sys.argv)
    except (ScannerError, ValueError, OSError) as exc:
        print(f'News scan failed: {type(exc).__name__}: {str(exc)[:160]}', file=sys.stderr)
        sys.exit(1)
