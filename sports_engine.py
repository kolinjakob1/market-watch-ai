"""Pregame two-way moneylines: conservative book consensus versus Polymarket US."""
import json
import math
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo
import requests
from scanner import API, ScannerError, request_json, price, required_env


def stamp(value):
    try:
        d = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return d if d.tzinfo else None
    except (ValueError, TypeError, AttributeError):
        return None


def norm(value):
    return ''.join(c for c in value.lower() if c.isalnum())


def discover(now):
    found = {}
    for offset in range(0, 20000, 500):
        data = request_json(requests.get, API + '/markets', 'Polymarket US', params={
            'active': 'true', 'closed': 'false', 'categories': 'sports',
            'marketTypes': 'moneyline',
            'orderBy': 'id', 'orderDirection': 'desc', 'limit': 500, 'offset': offset})
        rows = data.get('markets') if isinstance(data, dict) else None
        if not isinstance(rows, list):
            raise ScannerError('Unexpected market discovery response')
        for m in rows:
            start = stamp(m.get('gameStartTime'))
            if (start and now + timedelta(minutes=5) < start < now + timedelta(hours=36)
                    and m.get('marketType') == 'moneyline' and m.get('active')
                    and not m.get('closed') and not m.get('hidden')
                    and m.get('status') == 'MARKET_STATUS_OPEN'):
                found[m['id']] = m
        if len(rows) < 500:
            return list(found.values())
    raise ScannerError('Market discovery pagination limit reached; coverage incomplete')


def consensus(event, now):
    """Remove each book's margin; require three independent fresh two-way books."""
    home, away = norm(event['home_team']), norm(event['away_team'])
    books = {}
    for book in event.get('bookmakers', []):
        for market in book.get('markets', []):
            if market.get('key') != 'h2h':
                continue
            updated = stamp(market.get('last_update') or book.get('last_update'))
            if not updated or not -15 <= (now - updated).total_seconds() <= 300:
                continue
            outcomes = market.get('outcomes', [])
            if len(outcomes) != 2:
                continue  # Three-way soccer and draw rules need separate matching.
            try:
                odds = {norm(o['name']): float(o['price']) for o in outcomes}
                if set(odds) != {home, away} or not all(math.isfinite(p) and p > 1 for p in odds.values()):
                    continue
                raw = {k: 1 / v for k, v in odds.items()}
                total = sum(raw.values())
                if not .98 <= total <= 1.20:
                    continue
                books[book['key']] = {k: v / total for k, v in raw.items()}
            except (KeyError, ValueError, TypeError):
                continue
    if len(books) < 3:
        return None
    values = [v[home] for v in books.values()]
    if max(values) - min(values) > .08:
        return None
    p = statistics.median(values)
    return {home: p, away: 1 - p}, len(books)


def match(market, events):
    sides = market.get('marketSides', [])
    if len(sides) != 2 or not all(s.get('tradable') for s in sides):
        return None
    # Explicit team identity only. No substring or fuzzy matching.
    names = [norm((s.get('team') or {}).get('name', '')) for s in sides]
    if not all(names) or names[0] == names[1] or {s.get('long') for s in sides} != {True, False}:
        return None
    start = stamp(market.get('gameStartTime'))
    matches = [e for e in events if {norm(e['home_team']), norm(e['away_team'])} == set(names)
               and stamp(e.get('commence_time'))
               and abs((stamp(e['commence_time']) - start).total_seconds()) <= 900]
    return matches[0] if len(matches) == 1 else None


def evaluate(m, bbo, event, now, min_edge=.03):
    fair = consensus(event, now)
    if not fair or bbo.get('state') != 'MARKET_STATE_OPEN':
        return []
    bid, ask = price(bbo.get('bestBid')), price(bbo.get('bestAsk'))
    if bid is None or ask is None or bid > ask or ask - bid > .04:
        return []
    try:
        theta = float(m['feeCoefficient'])
        if not math.isfinite(theta) or not 0 <= theta <= .15:
            return []
    except (KeyError, ValueError, TypeError):
        return []
    picks = []
    for side in m['marketSides']:
        cost = float(ask if side['long'] else 1 - bid)
        name = side['team']['name']
        p = fair[0][norm(name)]
        fee = theta * cost * (1 - cost)
        # Reserve a further cent per contract for slippage/fee rounding.
        edge = p - cost - fee - .01
        if .10 <= cost <= .90 and edge >= min_edge:
            low, high = 0., 1.
            for _ in range(32):
                mid = (low + high) / 2
                if p - mid - theta * mid * (1-mid) - .01 >= .03:
                    low = mid
                else:
                    high = mid
            limit = math.floor(low * 1000) / 1000
            picks.append({'id': m['id'] + ':' + str(side['long']), 'slug': m['slug'],
                          'team': name, 'opponent': event['away_team'] if norm(name) == norm(event['home_team']) else event['home_team'],
                          'start': event['commence_time'], 'cost': cost, 'limit': limit,
                          'fair': p, 'edge': edge, 'books': fair[1]})
    return picks


def run(now):
    markets = discover(now)
    # Only fetch leagues represented on the exchange, one request per league per scan.
    leagues = {'nfl': 'americanfootball_nfl', 'nba': 'basketball_nba', 'nhl': 'icehockey_nhl',
               'mlb': 'baseball_mlb', 'ncaaf': 'americanfootball_ncaaf', 'ncaab': 'basketball_ncaab', 'cbb': 'basketball_ncaab', 'cfb': 'americanfootball_ncaaf'}
    present = {str((s.get('team') or {}).get('league', '')).lower() for m in markets for s in m.get('marketSides', [])}
    events = []
    for sport in sorted({leagues[k] for k in present & leagues.keys()}):
        rows = request_json(requests.get, 'https://api.the-odds-api.com/v4/sports/' + sport + '/odds',
                            'Odds API', params={'apiKey': required_env('ODDS_API_KEY'), 'regions': 'us',
                                                'markets': 'h2h', 'oddsFormat': 'decimal'})
        if not isinstance(rows, list):
            raise ScannerError('Unexpected Odds API response')
        events.extend(rows)
    picks, targets, matched = [], [], 0
    for m in markets:
        e = match(m, events)
        if not e:
            continue
        matched += 1
        data = request_json(requests.get, API + '/markets/' + quote(m['slug'], safe='') + '/bbo', 'Polymarket US')
        assessed = evaluate(m, data.get('marketData') or {}, e, now, min_edge=-1)
        targets.extend(assessed)
        picks.extend(p for p in assessed if p['edge'] >= .03)
    picks.sort(key=lambda p: p['edge'], reverse=True)
    state_path = Path('sports_state.json')
    state = json.loads(state_path.read_text()) if state_path.exists() else {'alerts': {}}
    fresh = []
    for p in picks:
        old = state['alerts'].get(p['id'])
        if old and stamp(old['time']) and (now - stamp(old['time'])).total_seconds() < 7200 and p['cost'] >= old['cost'] - .02:
            continue
        fresh.append(p)
        if len(fresh) == 3:
            break
    lines = []
    for p in fresh:
        when = stamp(p['start']).astimezone(ZoneInfo('America/Los_Angeles')).strftime('%a %-I:%M %p PT')
        lines.append(f"{p['team']} ML vs {p['opponent']} | {when}\nBuy {p['cost']*100:.1f}¢; limit {p['limit']*100:.1f}¢\n"
                     f"Consensus {p['fair']:.0%} ({p['books']} books); net model edge {p['edge']*100:.1f}¢\n"
                     f"https://polymarket.us/market/{p['slug']}")
    body = 'HUSTLEWATCH | MONEYLINE SIGNALS\n\n' + '\n\n'.join(lines) if lines else (
        f'HUSTLEWATCH SPORTS | NO NEW QUALIFYING PICKS\n{len(markets)} pregame moneylines; {matched} exact book matches.\n'
        f'{len(picks)} qualifying signals before duplicate filtering. No forced picks.')
    if not lines and not picks and targets:
        best = sorted(targets, key=lambda p: p['edge'], reverse=True)[:2]
        body += '\n\nClosest entry targets (PASS at current price):\n' + '\n'.join(
            f"{p['team']} ML vs {p['opponent']}: now {p['cost']*100:.1f}¢; enter only at/below {p['limit']*100:.1f}¢"
            for p in best)
    body += '\nModel estimate, not a proven edge. Pregame only; verify price and size in app.'
    print(f'Sports: discovered={len(markets)} matched={matched} qualifying={len(picks)} new={len(fresh)}')
    def delivered():
        for p in fresh:
            state['alerts'][p['id']] = {'cost': p['cost'], 'time': now.isoformat()}
        state['alerts'] = {k:v for k,v in state['alerts'].items() if stamp(v['time']) and now - stamp(v['time']) < timedelta(days=3)}
        state_path.write_text(json.dumps(state, indent=2) + '\n')
    return body[:3900], delivered
