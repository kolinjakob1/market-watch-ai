"""Live and upcoming two-way moneylines: conservative book consensus versus Polymarket US."""
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
            if (start and now - timedelta(hours=8) < start < now + timedelta(hours=48)
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
    live = bool(stamp(event.get('commence_time')) and stamp(event['commence_time']) <= now)
    max_age = 60 if live else 300
    books = {}
    for book in event.get('bookmakers', []):
        for market in book.get('markets', []):
            if market.get('key') != 'h2h':
                continue
            updated = stamp(market.get('last_update') or book.get('last_update'))
            if not updated or not -15 <= (now - updated).total_seconds() <= max_age:
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


def evaluate(m, bbo, event, now, min_edge=.03, diagnostics=None):
    def reject(reason):
        if diagnostics is not None:
            diagnostics[reason] = diagnostics.get(reason, 0) + 1
        return []
    live = bool(stamp(event.get('commence_time')) and stamp(event['commence_time']) <= now)
    if live:
        sample = stamp((bbo.get('lastPriceSample') or {}).get('ts'))
        if not sample or not -15 <= (now-sample).total_seconds() <= 30:
            return reject('stale live exchange sample')
    fair = consensus(event, now)
    if not fair:
        return reject('unverified book consensus')
    if bbo.get('state') != 'MARKET_STATE_OPEN':
        return reject('exchange market closed')
    bid, ask = price(bbo.get('bestBid')), price(bbo.get('bestAsk'))
    if bid is None or ask is None or bid > ask or ask - bid > .04:
        return reject('invalid or wide exchange quote')
    try:
        theta = float(m['feeCoefficient'])
        if not math.isfinite(theta) or not 0 <= theta <= .15:
            return reject('unknown or invalid fees')
    except (KeyError, ValueError, TypeError):
        return reject('unknown or invalid fees')
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
            picks.append({'id': str(m['id']) + ':' + str(side['long']), 'slug': m['slug'],
                          'team': name, 'opponent': event['away_team'] if norm(name) == norm(event['home_team']) else event['home_team'],
                          'start': event['commence_time'], 'cost': cost, 'limit': limit,
                          'fair': p, 'edge': edge, 'books': fair[1]})
    if not picks:
        return reject('price band or edge rules')
    return picks


def run(now):
    markets = discover(now)
    diagnostics = {}
    # Only fetch leagues represented on the exchange, one request per league per scan.
    leagues = {'nfl': 'americanfootball_nfl', 'nba': 'basketball_nba', 'nhl': 'icehockey_nhl',
               'mlb': 'baseball_mlb', 'ncaaf': 'americanfootball_ncaaf', 'ncaab': 'basketball_ncaab',
               'cbb': 'basketball_ncaab', 'cfb': 'americanfootball_ncaaf', 'wcbb': 'basketball_wncaab',
               'wnba': 'basketball_wnba', 'ufc': 'mma_mixed_martial_arts', 'boxing': 'boxing_boxing',
               'eurolg': 'basketball_euroleague', 'nbl': 'basketball_nbl',
               'epl': 'soccer_epl', 'mls': 'soccer_usa_mls', 'laliga': 'soccer_spain_la_liga',
               'bundesliga': 'soccer_germany_bundesliga', 'seriea': 'soccer_italy_serie_a',
               'ligue1': 'soccer_france_ligue_one', 'ucl': 'soccer_uefa_champs_league'}
    present = {str((s.get('team') or {}).get('league', '')).lower() for m in markets for s in m.get('marketSides', [])}
    catalog = request_json(requests.get, 'https://api.the-odds-api.com/v4/sports', 'Odds API',
                           params={'apiKey': required_env('ODDS_API_KEY')})
    if not isinstance(catalog, list):
        raise ScannerError('Unexpected sports catalog response')
    sport_keys = {leagues[k] for k in present & leagues.keys()}
    # Include each available tour/event for tennis, esports and cricket families.
    families = {'atp': 'tennis_atp_', 'wta': 'tennis_wta_', 'cs2': 'esports_cs',
                'dota2': 'esports_dota', 'lol': 'esports_lol', 't20icr': 'cricket_',
                'odicr': 'cricket_'}
    for item in catalog:
        key = item.get('key', '')
        if item.get('active') and (any(k in present and key.startswith(prefix) for k, prefix in families.items())
                or any(norm(k) == norm(item.get('title', '')) or norm(k) == norm(key) for k in present)):
            sport_keys.add(key)
    active_keys = {i['key'] for i in catalog if i.get('active')}
    sport_keys &= active_keys
    events = []
    for sport in sorted(sport_keys):
        rows = request_json(requests.get, 'https://api.the-odds-api.com/v4/sports/' + sport + '/odds',
                            'Odds API', params={'apiKey': required_env('ODDS_API_KEY'), 'regions': 'us',
                                                'markets': 'h2h', 'oddsFormat': 'decimal'})
        if not isinstance(rows, list):
            raise ScannerError('Unexpected Odds API response')
        events.extend(rows)
    targets, matched, errors = [], 0, []
    live_markets = 0
    checked_ids = set()
    for m in markets:
        e = match(m, events)
        if not e:
            continue
        checked_ids.add(str(m['id']))
        matched += 1
        live = stamp(e['commence_time']) <= now
        live_markets += int(live)
        # Recompute time after network requests, so older odds cannot appear fresh.
        checked = max(now, datetime.now(timezone.utc))
        try:
            data = request_json(requests.get, API + '/markets/' + quote(m['slug'], safe='') + '/bbo', 'Polymarket US')
        except ScannerError as exc:
            errors.append(str(exc))
            continue
        assessed = evaluate(m, data.get('marketData') or {}, e, checked, min_edge=-1, diagnostics=diagnostics)
        if not assessed:
            continue
        # One direction per game. Never pad the report with both opposing teams.
        p = max(assessed, key=lambda p: p['edge'])
        p['league'] = str((m['marketSides'][0].get('team') or {}).get('league', '')).upper()
        p['live'] = live
        p['checked'] = checked.isoformat()
        targets.append(p)
    targets.sort(key=lambda p: p['edge'], reverse=True)
    picks = [p for p in targets if p['edge'] >= .03]
    # Explain why the leading assessed candidates are not actionable.
    # Never manufacture BUY selections from a fixed report quota.
    if targets:
        best = targets[0]
        print(f"Sports best net edge={best['edge']*100:+.2f} cents; "
              f"short of BUY cutoff by {max(0, .03-best['edge'])*100:.2f} cents")
    else:
        print('Sports: no book-verified, quote-valid candidate available for evaluation')
    # Preserve live coverage alongside upcoming games when both exist.
    selected = targets[:10]
    for live in (True, False):
        if not any(p['live'] == live for p in selected):
            other = next((p for p in targets if p['live'] == live), None)
            if other and len(selected) == 10:
                selected[-1] = other
    # Show unmatched sports as UNRATED, never invent book probabilities or edges.
    # Rotate through leagues to avoid letting a large college/tennis slate hide others.
    extras = []
    seen_leagues = {p['league'].lower() for p in selected}
    unmatched = [m for m in markets if str(m['id']) not in checked_ids]
    unmatched.sort(key=lambda m: (str((m.get('marketSides', [{}])[0].get('team') or {}).get('league','')) in seen_leagues,
                                  abs((stamp(m['gameStartTime'])-now).total_seconds())))
    attempted = 0
    for m in unmatched:
        if attempted >= 30 or len(extras) >= max(3, 10-len(selected)):
            break
        sides = m.get('marketSides', [])
        if len(sides) != 2 or {x.get('long') for x in sides} != {True,False} or not all(x.get('tradable') and (x.get('team') or {}).get('name') for x in sides):
            continue
        league = str((sides[0].get('team') or {}).get('league','')).lower()
        if league in {p['league'].lower() for p in extras}:
            continue
        attempted += 1
        try:
            d = request_json(requests.get, API + '/markets/' + quote(m['slug'], safe='') + '/bbo', 'Polymarket US').get('marketData') or {}
        except ScannerError as exc:
            errors.append(str(exc)); continue
        bid, ask = price(d.get('bestBid')), price(d.get('bestAsk'))
        if d.get('state') != 'MARKET_STATE_OPEN' or bid is None or ask is None or bid > ask or ask-bid > .04:
            continue
        side = next(x for x in sides if x['long'] == (float(bid+ask)/2 >= .5))
        other = next(x for x in sides if x is not side)
        extras.append({'team':side['team']['name'], 'opponent':other['team']['name'],
                       'cost':float(ask if side['long'] else 1-bid), 'slug':m['slug'],
                       'league':league.upper(), 'start':m['gameStartTime']})
    # Ranked assessed candidates remain primary; additional sports are clearly separate.
    state_path = Path('sports_state.json')
    state = json.loads(state_path.read_text()) if state_path.exists() else {'alerts': {}}
    # Repeat the ranked report each scan, as requested; state is history, not suppression.
    lines = [f'HUSTLEWATCH SPORTS | {len(selected)} RANKED CANDIDATES',
             now.astimezone(ZoneInfo('America/Los_Angeles')).strftime('%a %-I:%M %p PT'),
             f'{len(picks)} qualify | {matched} matched games ({live_markets} live)',
             'BUY = meets model rules. PASS = wait; do not buy now.']
    for i, p in enumerate(selected, 1):
        when = stamp(p['start']).astimezone(ZoneInfo('America/Los_Angeles')).strftime('%a %-I:%M %p PT')
        label = 'LIVE' if p['live'] else 'UPCOMING'
        action = 'BUY' if p['edge'] >= .03 else 'PASS'
        lines.append(f"{i}. {p['league']} | {label} | {action} {p['team']} ML\nvs {p['opponent']} | {when}\n"
                     f"Select {p['team']} to WIN. Now {p['cost']*100:.1f}¢; max entry {p['limit']*100:.1f}¢\n"
                     f"Book estimate {p['fair']:.0%}; net edge {p['edge']*100:+.1f}¢ ({p['books']} books)\n"
                     f"https://polymarket.us/market/{p['slug']}")
    if len(selected) < 10:
        lines.append(f'Only {len(selected)} games have verified comparison data. Other candidates below are UNRATED/PASS.')
    lines.append(f'Coverage: {len(markets)-matched} markets without an exact book match; '
                 f'{len(targets)-len(picks)} assessed games below BUY edge.')
    if diagnostics:
        lines.append('Matched-market blocks: ' + '; '.join(f'{reason}: {count}' for reason, count in sorted(diagnostics.items())))
    if errors:
        lines.append(f'Degraded coverage: {len(errors)} market quote requests failed.')
    if extras:
        lines.append('OTHER SPORTS | UNRATED / PASS: no verified book edge.\n' + '\n'.join(
            f"{p['league']} | {'STARTED (status unverified)' if stamp(p['start']) <= now else 'UPCOMING'} | {p['team']} ML vs {p['opponent']} | {p['cost']*100:.1f}¢\nhttps://polymarket.us/market/{p['slug']}" for p in extras))
    lines.append('Discovered leagues (all open sports): ' + ', '.join(sorted(present)))
    lines.append('Model estimates; verify live price in app. No bets placed.')
    body = '\n\n'.join(lines)
    print(f'Sports: discovered={len(markets)} matched={matched} live={live_markets} qualifying={len(picks)} reported={len(selected)} errors={len(errors)}')
    def delivered():
        for p in selected:
            state['alerts'][p['id']] = {'cost': p['cost'], 'time': now.isoformat()}
        state['alerts'] = {k:v for k,v in state['alerts'].items() if stamp(v['time']) and now - stamp(v['time']) < timedelta(days=3)}
        state_path.write_text(json.dumps(state, indent=2) + '\n')
    return body, delivered
