"""Persistent $500 local paper simulator. No broker order submission endpoints."""
import json
import math
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path
import requests
from scanner import request_json, ScannerError, required_env

STRATEGY = 'momentum-v1'


def number(value):
    try:
        value = float(value)
        return value if math.isfinite(value) and value > 0 else None
    except (ValueError, TypeError):
        return None


def date(value):
    try:
        t = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return t if t.tzinfo else None
    except (ValueError, TypeError, AttributeError):
        return None


def quotes(snap, now, max_age=120):
    q = snap.get('latestQuote') or {}
    bid, ask, t = number(q.get('bp')), number(q.get('ap')), date(q.get('t'))
    if not bid or not ask or bid > ask or not t or not -15 <= (now-t).total_seconds() <= max_age:
        return None
    if not number(q.get('bs')) or not number(q.get('as')):
        return None
    return bid, ask


def load(path):
    if not path.exists():
        return {'version': 1, 'initial': 500, 'cash': 500, 'realized': 0,
                'positions': [], 'closed': [], 'last_entry': {}}
    state = json.loads(path.read_text())
    if state.get('version') != 1 or not math.isfinite(state['cash']) or state['cash'] < 0:
        raise ScannerError('Invalid paper ledger; refusing to reset')
    return state


def tick(state, snapshots, entries, now, closing=False):
    """Buy at ask+slippage; sell at bid-slippage. No same-day recycled stock cash."""
    notes, held = [], []
    today = now.date().isoformat()
    state.setdefault('unsettled', [])
    for cash in list(state['unsettled']):
        if cash['date'] < today and now.weekday() < 5:
            state['cash'] += cash['amount']
            state['unsettled'].remove(cash)
    closed_symbols = set()
    for p in state['positions']:
        q = quotes(snapshots.get(p['symbol'], {}), now)
        if not q:
            held.append(p)
            continue
        bid = q[0] * .999
        p['mark'] = bid
        reason = 'stop' if bid <= p['stop'] else 'target' if bid >= p['target'] else 'session close' if closing else None
        if reason:
            proceeds = round(bid * p['quantity'] * p['multiplier'], 6)
            pnl = round(proceeds - p['cost'], 6)
            state['unsettled'].append({'date': today, 'amount': proceeds})
            state['realized'] += pnl
            record = dict(p, exit=bid, pnl=pnl, closed_at=now.isoformat(), reason=reason)
            state['closed'].append(record)
            closed_symbols.add(p['symbol'])
            notes.append(f"PAPER EXIT {p['symbol']} | {reason} | P&L ${pnl:+.2f}")
        else:
            held.append(p)
    state['positions'] = held
    for e in entries:
        if closing or len(held) >= 3:
            break
        symbol = e['symbol']
        if symbol in closed_symbols or any(p['symbol'] == symbol or p['underlying'] == e['underlying'] for p in held):
            continue
        if state['last_entry'].get(e['underlying']) == today:
            continue
        q = quotes(snapshots.get(symbol, {}), now)
        if not q:
            continue
        bid, ask = q
        if (ask-bid)/ask > (.10 if e['multiplier'] == 100 else .0035):
            continue
        entry = ask * 1.001
        budget = min(100, state['cash'])
        qty = int(budget/(entry*100)) if e['multiplier'] == 100 else math.floor(budget/entry*1000000)/1000000
        cost = round(qty * entry * e['multiplier'], 6)
        if qty <= 0 or cost < 5 or cost > state['cash']:
            continue
        stop_pct, target_pct = (.20, .30) if e['multiplier'] == 100 else (.02, .04)
        p = dict(e, quantity=qty, entry=entry, cost=cost, mark=bid*.999,
                 stop=entry*(1-stop_pct), target=entry*(1+target_pct),
                 opened_at=now.isoformat(), strategy=STRATEGY)
        held.append(p)
        state['cash'] -= cost
        state['last_entry'][e['underlying']] = today
        notes.append(f"PAPER BUY {symbol} | {qty:g} @ ${entry:.2f}\nStop ${p['stop']:.2f} | target ${p['target']:.2f}")
    return notes


def run(now):
    key, secret = required_env('ALPACA_API_KEY'), required_env('ALPACA_SECRET_KEY')
    headers = {'APCA-API-KEY-ID': key, 'APCA-API-SECRET-KEY': secret}
    def get(path, params=None, trading=False):
        host = 'https://paper-api.alpaca.markets' if trading else 'https://data.alpaca.markets'
        return request_json(requests.get, host + path, 'Alpaca', headers=headers, params=params or {})
    path = Path('paper_state.json')
    state = load(path)
    clock = get('/v2/clock', trading=True)
    notes = []
    if clock.get('is_open') is True:
        symbols = list(dict.fromkeys([s.strip().upper() for s in __import__('os').environ.get('WATCHLIST','AAPL,NVDA,TSLA,SPY,QQQ').split(',') if s.strip()] +
                                    [p['underlying'] for p in state['positions']]))
        snaps = get('/v2/stocks/snapshots', {'symbols': ','.join(symbols), 'feed': 'iex'})
        entries = []
        for symbol in symbols:
            q = quotes(snaps.get(symbol, {}), now)
            prior = number((snaps.get(symbol, {}).get('prevDailyBar') or {}).get('c'))
            if q and prior and .02 <= q[1]/prior-1 <= .08:
                entries.append({'symbol': symbol, 'underlying': symbol, 'multiplier': 1, 'feed': 'iex'})
        option_status = 'Options: no momentum setup'
        # Prefer affordable near-the-money calls for bullish momentum, else stock.
        for e in list(entries):
            symbol = e['symbol']
            try:
                chain = get('/v1beta1/options/snapshots/' + symbol,
                            {'feed': 'opra', 'limit': 1000,
                             'expiration_date_gte': (now + timedelta(days=7)).date().isoformat(),
                             'expiration_date_lte': (now + timedelta(days=21)).date().isoformat(),
                             'type': 'call'})
            except ScannerError as exc:
                if 'HTTP 403' not in str(exc):
                    raise
                option_status = 'Options blocked: OPRA data entitlement required; stock paper remains active'
                break
            choices = []
            underlying = quotes(snaps[symbol], now)[1]
            for contract, snapshot in (chain.get('snapshots') or {}).items():
                tail = contract[-15:]
                if not re.fullmatch(r'\d{6}C\d{8}', tail):
                    continue
                expiry = datetime.strptime(tail[:6], '%y%m%d').date()
                strike = int(tail[7:]) / 1000
                q = quotes(snapshot, now)
                if (7 <= (expiry-now.date()).days <= 21 and abs(strike/underlying-1) <= .05
                        and q and (q[1]-q[0])/q[1] <= .10 and q[1]*100*1.001 <= 100):
                    choices.append((abs(strike/underlying-1), contract, snapshot))
            if choices:
                _, contract, snapshot = min(choices, key=lambda x: x[0])
                snaps[contract] = snapshot
                entries[entries.index(e)] = {'symbol': contract, 'underlying': symbol, 'multiplier':100, 'feed':'opra'}
                option_status = 'Options: affordable OPRA call setup evaluated'
            else:
                option_status = 'Options: no affordable fresh call in first chain page; stock fallback'

        option_symbols = [p['symbol'] for p in state['positions'] if p['multiplier'] == 100]
        if option_symbols:
            opt = get('/v1beta1/options/snapshots', {'symbols': ','.join(option_symbols), 'feed':'opra'})
            snaps.update(opt.get('snapshots') or {})
        close = date(clock.get('next_close'))
        if not close:
            raise ScannerError('Alpaca clock missing next_close')
        closing = (close-now).total_seconds() <= 900
        notes = tick(state, snaps, entries, now, closing)
        status = 'Regular session open' + ('; closing positions' if closing else '') + '\n' + option_status
    else:
        status = 'Market closed; no stale-price fills'
    equity = state['cash'] + sum(x['amount'] for x in state.get('unsettled', [])) + sum(p.get('mark',p['entry'])*p['quantity']*p['multiplier'] for p in state['positions'])
    state['updated_at'] = now.isoformat()
    state['equity'] = round(equity, 6)
    path.write_text(json.dumps(state, indent=2) + '\n')
    wins = sum(p['pnl'] > 0 for p in state['closed'])
    body = ('HUSTLEWATCH | LOCAL PAPER SIMULATOR\n' + status + '\n'
            f"Equity ${equity:.2f} | cash ${state['cash']:.2f}\nRealized ${state['realized']:+.2f} | "
            f"{len(state['positions'])} open | {len(state['closed'])} closed ({wins} wins)\n\n")
    body += '\n'.join(notes) if notes else 'No entries/exits this scan.'
    body += '\nMomentum v1; $100 cap/position. Simulated fills; no broker orders. Options require fresh OPRA quotes. Fixed rules; results logged, no automatic model retraining.'
    print(f"Paper: equity={equity:.2f} open={len(state['positions'])} closed={len(state['closed'])}")
    return body[:3900]
