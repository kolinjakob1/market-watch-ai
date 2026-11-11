"""Cash-only stock and listed-option simulator using Alpaca market data."""
import json
import math
import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from time import monotonic
from zoneinfo import ZoneInfo

import requests

from scanner import ScannerError, request_json, required_env

STRATEGY = 'intraday-momentum-v2'
EASTERN = ZoneInfo('America/New_York')
BAR = timedelta(minutes=5)
MAX_ALLOCATION = 100
MAX_RISK = 10
DAILY_RISK = 30
MAX_POSITIONS = 2
MAX_ENTRIES = 3
SLIPPAGE = .001


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


def quotes(snap, now, *, session_open=None):
    q = snap.get('latestQuote') or {}
    bid, ask, stamp = number(q.get('bp')), number(q.get('ap')), date(q.get('t'))
    if not bid or not ask or bid > ask or not stamp:
        return None
    if not -2 <= (now - stamp).total_seconds() <= 120 or (session_open and stamp < session_open):
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


def signal(bars, now, session_open):
    """Evaluate consecutive, completed regular-session bars on the same feed."""
    expected = session_open + ((now - session_open) // BAR - 1) * BAR
    rows = {}
    for row in bars:
        stamp = date(row.get('t'))
        if stamp and session_open <= stamp <= expected and (stamp - session_open) % BAR == timedelta(0):
            close, volume, vwap = (number(row.get(field)) for field in ('c', 'v', 'vw'))
            if not (close and volume and vwap) or stamp in rows:
                return None, 'invalid or duplicate bars'
            rows[stamp] = (close, volume, vwap)
    if len(rows) < 21:
        return None, 'needs 21 completed session bars'
    if expected not in rows or len(rows) != (expected - session_open) // BAR + 1:
        return None, 'stale or missing session bars'

    closes, volumes, vwaps = zip(*(rows[stamp] for stamp in sorted(rows)))
    fast = slow = closes[0]
    for close in closes[1:]:
        fast += (close - fast) * (2 / 10)
        slow += (close - slow) * (2 / 22)
    changes = [b - a for a, b in zip(closes[-15:-1], closes[-14:])]
    gain = sum(max(change, 0) for change in changes)
    loss = sum(max(-change, 0) for change in changes)
    rsi = 100 * gain / (gain + loss) if gain + loss else 50
    relative_volume = volumes[-1] / (sum(volumes[-21:-1]) / 20)
    vwap = sum(price * volume for price, volume in zip(vwaps, volumes)) / sum(volumes)
    close = closes[-1]
    metrics = f'RSI {rsi:.0f} | RVOL {relative_volume:.2f}x'
    if relative_volume > 1.5:
        if fast > slow and rsi > 55 and close > max(vwap, closes[-2]):
            return ('call', close, vwap, expected), metrics
        if fast < slow and rsi < 45 and close < min(vwap, closes[-2]):
            return ('put', close, vwap, expected), metrics
    return None, metrics + ' | trend/volume confirmation missing'


def entry_size(bid, ask, multiplier, cash, risk_budget):
    option = multiplier == 100
    if (ask - bid) / ask > (.10 if option else .0035):
        return None
    entry = ask * (1 + SLIPPAGE)
    stop_pct, target_pct = (.20, .30) if option else (.02, .04)
    max_debit = min(MAX_ALLOCATION, cash, risk_budget / stop_pct)
    units = max_debit / (entry * multiplier)
    quantity = math.floor(units) if option else math.floor(units * 1_000_000) / 1_000_000
    cost = round(quantity * entry * multiplier, 6)
    if not 5 <= cost <= cash:
        return None
    return quantity, entry, cost, entry * (1 - stop_pct), entry * (1 + target_pct)


def tick(state, snapshots, entries, now, closing=False, *, session_open=None, settled_dates=()):
    """Mark and fill against executable sides. Entries use settled cash only."""
    notes, held = [], []
    today = now.astimezone(EASTERN).date().isoformat()
    pending = state.setdefault('unsettled', [])
    state['cash'] += sum(item['amount'] for item in pending if item['date'] in settled_dates and item['date'] < today)
    pending[:] = [item for item in pending if item['date'] not in settled_dates or item['date'] >= today]

    for p in state['positions']:
        q = quotes(snapshots.get(p['symbol'], {}), now, session_open=session_open)
        if not q:
            held.append(p)
            notes.append(f"HOLD {p['symbol']} | fresh exit quote unavailable; mark carried forward")
            continue
        bid = q[0] * (1 - SLIPPAGE)
        p['mark'] = bid
        overdue = date(p['opened_at']).astimezone(EASTERN).date().isoformat() < today
        reason = ('stop' if bid <= p['stop'] else 'target' if bid >= p['target']
                  else 'overnight carry' if overdue else 'session close' if closing else None)
        if reason:
            proceeds = round(bid * p['quantity'] * p['multiplier'], 6)
            pnl = round(proceeds - p['cost'], 6)
            pending.append({'date': today, 'amount': proceeds})
            state['realized'] += pnl
            state['closed'].append(dict(p, exit=bid, pnl=pnl, closed_at=now.isoformat(), reason=reason))
            notes.append(f"PAPER EXIT {p['symbol']} | {reason} | P&L ${pnl:+.2f}")
        else:
            held.append(p)
    state['positions'] = held

    return notes + enter(state, snapshots, entries, now, closing, session_open=session_open)


def enter(state, snapshots, entries, now, closing=False, *, session_open=None):
    notes, held = [], state['positions']
    today = now.astimezone(EASTERN).date().isoformat()
    losses = -sum(min(0, p['pnl']) for p in state['closed']
                  if date(p['closed_at']).astimezone(EASTERN).date().isoformat() == today)
    open_risk = sum(max(0, p['entry'] - p['stop']) * p['quantity'] * p['multiplier'] for p in held)
    entry_count = sum(day == today for day in state['last_entry'].values())
    for e in entries:
        risk_budget = min(MAX_RISK, DAILY_RISK - losses - open_risk)
        if closing or len(held) >= MAX_POSITIONS or entry_count >= MAX_ENTRIES or risk_budget <= 0:
            notes.append('WAIT | session or portfolio risk limit reached')
            break
        symbol, underlying = e['symbol'], e['underlying']
        if state['last_entry'].get(underlying) == today or any(p['underlying'] == underlying for p in held):
            continue
        q = quotes(snapshots.get(symbol, {}), now, session_open=session_open)
        size = entry_size(*q, e['multiplier'], state['cash'], risk_budget) if q else None
        if not size:
            notes.append(f'WAIT {symbol} | quote, spread, settled cash or risk limit')
            continue
        qty, entry, cost, stop, target = size
        risk = (entry - stop) * qty * e['multiplier']
        held.append(dict(e, quantity=qty, entry=entry, cost=cost, mark=q[0] * (1 - SLIPPAGE),
                         stop=stop, target=target, planned_risk=risk,
                         opened_at=now.isoformat(), strategy=STRATEGY))
        state['cash'] -= cost
        state['last_entry'][underlying] = today
        open_risk += risk
        entry_count += 1
        notes.append(f"PAPER BUY {symbol} | {qty:g} @ ${entry:.3f} | debit ${cost:.2f}\n"
                     f"Stop ${stop:.3f} | target ${target:.3f} | planned risk ${risk:.2f}")
    return notes


def option_entry(get, symbol, side, underlying, as_of, session_open, close, cash):
    now = as_of()
    today = now.astimezone(EASTERN).date()
    deadline = min(now + timedelta(seconds=25), close - 3 * BAR)
    params = {'feed': 'opra', 'limit': 1000,
              'expiration_date_gte': (today + timedelta(days=7)).isoformat(),
              'expiration_date_lte': (today + timedelta(days=30)).isoformat(),
              'type': side, 'strike_price_gte': round(underlying * .95, 2),
              'strike_price_lte': round(underlying * 1.05, 2)}
    choices = []
    tokens = set()
    while as_of() < deadline:
        chain = get('/v1beta1/options/snapshots/' + symbol, params)
        now = as_of()
        for contract, snap in (chain.get('snapshots') or {}).items():
            match = re.fullmatch(re.escape(symbol) + r'(\d{6})' + side[0].upper() + r'(\d{8})', contract)
            if not match:
                continue
            try:
                expiry = datetime.strptime(match[1], '%y%m%d').date()
            except ValueError:
                continue
            distance = abs((int(match[2]) / 1000) / underlying - 1)
            q = quotes(snap, now, session_open=session_open)
            if 7 <= (expiry - today).days <= 30 and distance <= .05 and q and entry_size(*q, 100, cash, MAX_RISK):
                choices.append((distance, (q[1] - q[0]) / q[1], contract, snap))
        token = chain.get('next_page_token')
        if not token:
            break
        if token in tokens:
            raise ScannerError('Option chain pagination stalled')
        tokens.add(token)
        params['page_token'] = token
    for _, _, contract, snap in sorted(choices, key=lambda x: x[:3]):
        if as_of() >= deadline:
            break
        if not quotes(snap, as_of(), session_open=session_open):
            continue
        meta = get('/v2/options/contracts/' + contract, trading=True)
        deliverables = meta.get('deliverables') or []
        if (meta.get('symbol') == contract and meta.get('underlying_symbol') == symbol
                and meta.get('status') == 'active' and meta.get('tradable') is True
                and number(meta.get('size')) == 100 and number(meta.get('multiplier', 100)) == 100
                and len(deliverables) <= 1):
            return {'symbol': contract, 'underlying': symbol, 'multiplier': 100, 'feed': 'opra'}, snap
    return None, None


def run(now, *, persist=True):
    started = monotonic()

    def as_of():
        return now + timedelta(seconds=monotonic() - started)

    headers = {'APCA-API-KEY-ID': required_env('ALPACA_API_KEY'),
               'APCA-API-SECRET-KEY': required_env('ALPACA_SECRET_KEY')}

    def get(path, params=None, trading=False):
        host = 'https://paper-api.alpaca.markets' if trading else 'https://data.alpaca.markets'
        return request_json(requests.get, host + path, 'Alpaca ' + path, headers=headers, params=params or {})

    path = Path('paper_state.json')
    state = load(path)
    clock = get('/v2/clock', trading=True)
    notes, candidates = [], []
    if clock.get('is_open') is True:
        close = date(clock.get('next_close'))
        if not close:
            raise ScannerError('Alpaca clock missing next_close')
        market_day = close.astimezone(EASTERN).date()
        session_open = close.astimezone(EASTERN).replace(hour=9, minute=30, second=0, microsecond=0)
        symbols = list(dict.fromkeys([s.strip().upper() for s in
                       os.getenv('WATCHLIST', 'AAPL,NVDA,TSLA,SPY,QQQ').split(',') if s.strip()] +
                       [p['underlying'] for p in state['positions']]))
        snaps = {}
        try:
            snaps.update(get('/v2/stocks/snapshots', {'symbols': ','.join(symbols), 'feed': 'iex'}))
        except ScannerError as exc:
            notes.append(f'Stock quotes blocked: {exc}')
        options = [p['symbol'] for p in state['positions'] if p['multiplier'] == 100]
        option_status = 'Options: no confirmed setup evaluated'
        if options:
            try:
                data = get('/v1beta1/options/snapshots', {'symbols': ','.join(options), 'feed': 'opra'})
                snaps.update(data.get('snapshots') or {})
            except ScannerError as exc:
                option_status = f'Options blocked: {exc}'
        settled = set()
        outstanding = [x['date'] for x in state.get('unsettled', []) if x['date'] < market_day.isoformat()]
        if outstanding:
            try:
                rows = get('/v2/calendar', {'start': min(outstanding), 'end': market_day.isoformat()}, trading=True)
                settled = {row['date'] for row in rows
                           if row.get('settlement_date') and row['settlement_date'] <= market_day.isoformat()}
                if not all(row.get('settlement_date') for row in rows):
                    notes.append('Settlement dates unavailable for some trades; affected proceeds held')
            except ScannerError as exc:
                notes.append(f'Settlement data blocked: {exc}')
        checked = as_of()
        entries = []
        if session_open <= checked < close:
            notes += tick(state, snaps, [], checked, checked >= close - 3 * BAR,
                          session_open=session_open, settled_dates=settled)
        if session_open + 21 * BAR <= checked < close - 3 * BAR and snaps:
            bars = {}
            params = {'symbols': ','.join(symbols), 'timeframe': '5Min', 'feed': 'iex', 'limit': 10000,
                      'start': session_open.isoformat(), 'end': (checked - BAR).isoformat()}
            tokens = set()
            try:
                while True:
                    page = get('/v2/stocks/bars', params)
                    for symbol, rows in (page.get('bars') or {}).items():
                        bars.setdefault(symbol, []).extend(rows)
                    token = page.get('next_page_token')
                    if not token:
                        break
                    if token in tokens:
                        raise ScannerError('Stock bars pagination stalled')
                    tokens.add(token)
                    params['page_token'] = token
            except ScannerError as exc:
                notes.append(f'Signals blocked: {exc}')
                bars.clear()
            cash = state['cash']
            today = market_day.isoformat()
            held_underlyings = {p['underlying'] for p in state['positions']}
            for symbol in symbols:
                q = quotes(snaps.get(symbol, {}), as_of(), session_open=session_open)
                setup, metrics = signal(bars.get(symbol, []), checked, session_open)
                if not q:
                    candidates.append(f'BLOCKED {symbol} | missing fresh IEX quote')
                    continue
                if not setup:
                    candidates.append(f'WAIT {symbol} | {metrics}')
                    continue
                side, bar_close, vwap, stamp = setup
                underlying = sum(q) / 2
                if (side == 'call' and q[0] <= vwap) or (side == 'put' and q[1] >= vwap) or abs(underlying / bar_close - 1) > .005:
                    candidates.insert(0, f'WAIT {symbol} | quote moved away from setup')
                    continue
                candidates.insert(0, f'SETUP {symbol} {side.upper()} | {metrics}')
                if cash < 5 or len(held_underlyings) >= MAX_POSITIONS or symbol in held_underlyings or state['last_entry'].get(symbol) == today:
                    continue
                option = None
                if not option_status.startswith('Options blocked:'):
                    try:
                        option, snap = option_entry(get, symbol, side, underlying, as_of, session_open, close, cash)
                        option_status = 'Options: OPRA chain evaluated; fills require an eligible quote'
                    except ScannerError as exc:
                        option_status = f'Options blocked: {exc}'
                if option:
                    snaps[option['symbol']] = snap
                    entries.append(dict(option, signal_bar=stamp.isoformat()))
                if side == 'call':
                    entries.append({'symbol': symbol, 'underlying': symbol, 'multiplier': 1,
                                    'feed': 'iex', 'signal_bar': stamp.isoformat()})
        if (option_status == 'Options: no confirmed setup evaluated'
                and session_open + 21 * BAR <= as_of() < close - 3 * BAR):
            try:
                get('/v1beta1/options/snapshots/SPY', {'feed': 'opra', 'limit': 1})
                option_status = 'Options: OPRA API reachable | no eligible setup evaluated'
            except ScannerError as exc:
                option_status = f'Options blocked: {exc}'
        evaluated = as_of()
        if session_open <= evaluated < close:
            # A delayed scan cannot trade a signal once a newer bar has closed.
            entries = [e for e in entries if evaluated - date(e['signal_bar']) < 2 * BAR]
            notes += enter(state, snaps, entries, evaluated, evaluated >= close - 3 * BAR,
                           session_open=session_open)
        else:
            notes.append('Scan overran the session; fills blocked')
        fresh = sum(bool(quotes(snaps.get(s, {}), evaluated, session_open=session_open)) for s in symbols)
        status = f'{len(symbols)} symbols | {fresh} fresh IEX quotes | {len(entries)} eligible instruments\n{option_status}'
        if not fresh:
            status += '\nStock fills blocked: no fresh regular-session IEX quotes'
        if evaluated < session_open + 21 * BAR:
            status += '\nWaiting for 21 completed regular-session bars'
        if evaluated >= close - 3 * BAR:
            status += '\nSession close window | exits only'
    elif clock.get('is_open') is False:
        next_open = date(clock.get('next_open'))
        next_text = (next_open.astimezone(ZoneInfo('America/Los_Angeles')).strftime('%a %-I:%M %p PT')
                     if next_open else 'check exchange calendar')
        status = 'Market closed | next open ' + next_text
    else:
        raise ScannerError('Alpaca clock missing market status')

    equity = (state['cash'] + sum(x['amount'] for x in state.get('unsettled', []))
              + sum(p.get('mark', p['entry']) * p['quantity'] * p['multiplier'] for p in state['positions']))
    if persist:
        state.update(updated_at=now.isoformat(), equity=round(equity, 6))
        temp = path.with_suffix('.tmp')
        temp.write_text(json.dumps(state, indent=2, allow_nan=False) + '\n')
        temp.replace(path)
    wins = sum(p['pnl'] > 0 for p in state['closed'])
    body = (f'HUSTLEWATCH | PAPER STOCKS & OPTIONS\n{status}\n'
            f"Marked equity ${equity:.2f} | settled cash ${state['cash']:.2f}\n"
            f"Realized ${state['realized']:+.2f} | {len(state['positions'])} open | "
            f"{len(state['closed'])} closed ({wins} wins)\n\n")
    body += '\n'.join(notes) if notes else 'No paper fills this scan.'
    if candidates:
        body += '\n\n' + '\n'.join(candidates[:10])
    print(status)
    print(f"Paper: equity={equity:.2f} open={len(state['positions'])} closed={len(state['closed'])}")
    return body
