"""HustleWatch crypto-first paper signal engine. No real orders or guarantees.

Uses Alpaca US 5-minute OHLCV bars plus latest executable-side quotes.
GitHub Actions persists paper state by committing crypto_state.json.
"""
import json
import math
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

API = 'https://data.alpaca.markets/v1beta3/crypto/us'
SYMBOLS = ['BTC/USD', 'ETH/USD', 'SOL/USD', 'AVAX/USD', 'LINK/USD',
           'LTC/USD', 'BCH/USD', 'DOGE/USD', 'UNI/USD', 'AAVE/USD']
STATE_FILE = Path(__file__).with_name('crypto_state.json')
STARTING_CASH = 500.0
RISK_PER_TRADE = 5.0
MAX_ALLOCATION = 125.0
MAX_OPEN = 2
FEE_RATE = 0.0015  # conservative paper estimate per side, not Robinhood fee quote
QUOTE_MAX_AGE = 180
SCAN_TIME = datetime.now(timezone.utc)


def iso(dt):
    return dt.astimezone(timezone.utc).isoformat(timespec='seconds')


def parse_time(s):
    return datetime.fromisoformat(s.replace('Z', '+00:00'))


def api(path, params):
    headers = {'APCA-API-KEY-ID': os.environ['ALPACA_API_KEY'],
               'APCA-API-SECRET-KEY': os.environ['ALPACA_SECRET_KEY']}
    r = requests.get(API + path, params=params, headers=headers, timeout=25)
    if not r.ok:
        raise RuntimeError(f'Alpaca {path} HTTP {r.status_code}')
    return r.json()


def notify(msg):
    if os.getenv('DRY_RUN') == '1':
        print('DRY RUN TELEGRAM:\n' + msg)
        return
    token = ''.join(os.environ['TELEGRAM_BOT_TOKEN'].split()).strip('"\'')
    if token.startswith('bot'):
        token = token[3:]
    response = requests.post(f'https://api.telegram.org/bot{token}/sendMessage',
        json={'chat_id': os.environ['TELEGRAM_CHAT_ID'].strip(),
              'text': msg[:3900], 'disable_web_page_preview': True}, timeout=20)
    if not response.ok:
        raise RuntimeError(f'Telegram HTTP {response.status_code}')
    if not response.json().get('ok'):
        raise RuntimeError('Telegram did not accept message')


def load_state():
    if not STATE_FILE.exists():
        return {'version': 1, 'cash': STARTING_CASH, 'positions': {},
                'closed': [], 'cooldown': {}, 'last_heartbeat': ''}
    data = json.loads(STATE_FILE.read_text())
    if data.get('version') != 1:
        raise RuntimeError('Unexpected paper ledger version')
    return data


def save_state(state):
    tmp = STATE_FILE.with_suffix('.tmp')
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + '\n')
    tmp.replace(STATE_FILE)


def ema(vals, window):
    alpha = 2 / (window + 1)
    result = vals[0]
    for v in vals[1:]:
        result = alpha * v + (1 - alpha) * result
    return result


def rsi(vals, period=14):
    changes = [vals[i] - vals[i-1] for i in range(1, len(vals))]
    gains = [max(v, 0) for v in changes[-period:]]
    losses = [max(-v, 0) for v in changes[-period:]]
    avg_gain, avg_loss = sum(gains)/period, sum(losses)/period
    return 100 if avg_loss == 0 else 100 - 100/(1 + avg_gain/avg_loss)


def atr(bars, period=14):
    tr = []
    for i in range(1, len(bars)):
        b = bars[i]
        prev = float(bars[i-1]['c'])
        tr.append(max(float(b['h']) - float(b['l']),
                      abs(float(b['h']) - prev), abs(float(b['l']) - prev)))
    return sum(tr[-period:])/period


def market_data():
    # A single batched 16-hour request is generally <1000 five-minute bars for 3 pairs.
    start = iso(SCAN_TIME - timedelta(hours=16))
    params = {'symbols': ','.join(SYMBOLS), 'timeframe': '5Min',
              'start': start, 'limit': 1000, 'sort': 'asc'}
    all_bars = {s: [] for s in SYMBOLS}
    token = None
    for _ in range(10):
        if token:
            params['page_token'] = token
        data = api('/bars', params)
        for sym in SYMBOLS:
            all_bars[sym].extend(data.get('bars', {}).get(sym, []))
        token = data.get('next_page_token')
        if not token:
            break
    if token:
        raise RuntimeError('Historical bar pagination incomplete; refusing signals')
    # Use only fully closed bars, never an incomplete candle.
    for sym in SYMBOLS:
        all_bars[sym] = [b for b in all_bars[sym]
            if parse_time(b['t']) + timedelta(minutes=5) <= SCAN_TIME - timedelta(seconds=10)]
        all_bars[sym].sort(key=lambda b: b['t'])
    quotes = api('/latest/quotes', {'symbols': ','.join(SYMBOLS)}).get('quotes', {})
    return all_bars, quotes


def quote_prices(q):
    if not q or not q.get('t'):
        return None
    age = (SCAN_TIME - parse_time(q['t'])).total_seconds()
    bid, ask = float(q.get('bp') or 0), float(q.get('ap') or 0)
    if not (math.isfinite(bid) and math.isfinite(ask) and -10 <= age <= QUOTE_MAX_AGE and 0 < bid <= ask):
        return None
    if (ask - bid)/ask > 0.008:
        return None
    return bid, ask


def setup(bars, bid, ask):
    if len(bars) < 70:
        return None
    if (SCAN_TIME - (parse_time(bars[-1]['t']) + timedelta(minutes=5))).total_seconds() > 20*60:
        return None
    closes = [float(b['c']) for b in bars]
    volumes = [float(b['v']) for b in bars]
    e20 = ema(closes[-60:], 20)
    e50 = ema(closes[-100:], 50)
    strength = rsi(closes)
    volatility = atr(bars)
    # GitHub schedules can be late: consider recent completed breakout candles,
    # while refusing to chase a price more than 0.8% above that breakout close.
    breakout = None
    for i in range(len(bars)-1, max(20, len(bars)-4)-1, -1):
        prior = bars[i-20:i]
        resistance = max(float(b['h']) for b in prior)
        recent_volume = sum(float(b['v']) for b in prior)/20
        bar = bars[i]
        age = (SCAN_TIME - (parse_time(bar['t']) + timedelta(minutes=5))).total_seconds()
        if (0 <= age <= 15*60 and float(bar['c']) > resistance
                and recent_volume > 0 and float(bar['v']) >= recent_volume*1.3
                and 52 <= strength <= 72 and e20 > e50
                and float(bar['c'])*0.995 <= ask <= float(bar['c'])*1.008
                and closes[-1] > resistance):
            breakout = bar, recent_volume
            break
    if breakout is None:
        return None
    bar, recent_volume = breakout
    if volatility <= 0:
        return None
    risk_per_unit = max(volatility * 1.5, ask * 0.003)
    stop = ask - risk_per_unit
    target = ask + risk_per_unit * 2.2
    return {'entry': ask, 'stop': stop, 'target': target,
            'rsi': strength, 'volume_multiple': float(bar['v'])/recent_volume,
            'bar': bar['t']}


def process(state, bars, quotes):
    events = []
    # Exits assessed before entries; paper exits at bid with conservative fill assumption.
    for symbol, pos in list(state['positions'].items()):
        prices = quote_prices(quotes.get(symbol))
        if prices is None:
            print(f'{symbol}: stale quote; exit cannot be assessed')
            continue
        bid, _ = prices
        reason = ('STOP' if bid <= pos['stop'] else
                  'TARGET' if bid >= pos['target'] else None)
        if reason is None:
            continue
        proceeds = pos['qty'] * bid * (1 - FEE_RATE)
        pnl = proceeds - pos['spent']
        state['cash'] += proceeds
        state['closed'].append({'symbol': symbol, 'entry': pos['entry'], 'exit': bid,
                                'pnl': round(pnl, 2), 'time': iso(SCAN_TIME), 'reason': reason})
        state['closed'] = state['closed'][-300:]
        del state['positions'][symbol]
        state['cooldown'][symbol] = iso(SCAN_TIME)
        events.append(f'PAPER EXIT | {symbol} | {reason}\n'
                      f'Exit bid ${bid:,.2f} | P/L ${pnl:+.2f} | cash ${state["cash"]:.2f}')
    for symbol in SYMBOLS:
        if symbol in state['positions'] or len(state['positions']) >= MAX_OPEN:
            continue
        last_exit = state['cooldown'].get(symbol)
        if last_exit and (SCAN_TIME - parse_time(last_exit)).total_seconds() < 3600:
            continue
        prices = quote_prices(quotes.get(symbol))
        if prices is None:
            print(f'{symbol}: quote missing or stale')
            continue
        bid, ask = prices
        candidate = setup(bars[symbol], bid, ask)
        if candidate is None:
            continue
        per_unit_risk = (ask - candidate['stop']) + ask * 2 * FEE_RATE
        budget = min(MAX_ALLOCATION, state['cash'])
        qty = min(RISK_PER_TRADE / per_unit_risk, budget / (ask*(1+FEE_RATE)))
        if qty * ask < 10:
            continue
        spent = qty * ask * (1 + FEE_RATE)
        if spent > state['cash']:
            continue
        state['cash'] -= spent
        state['positions'][symbol] = {'qty': qty, 'entry': ask,
            'stop': candidate['stop'], 'target': candidate['target'],
            'spent': spent, 'time': iso(SCAN_TIME), 'bar': candidate['bar']}
        events.append(f'PAPER BUY | {symbol}\n'
                      f'Simulated buy ${ask:,.2f} | target ${candidate["target"]:,.2f}\n'
                      f'Stop ${candidate["stop"]:,.2f} | allocation ${spent:.2f}\n'
                      f'RSI {candidate["rsi"]:.0f} | volume {candidate["volume_multiple"]:.1f}x | 5m breakout\n'
                      f'Max planned risk ~${RISK_PER_TRADE:.2f}; slips/gaps may exceed this')
    return events


def main():
    for key in ('ALPACA_API_KEY', 'ALPACA_SECRET_KEY'):
        if not os.getenv(key):
            raise RuntimeError(f'Missing {key}')
    state = load_state()
    bars, quotes = market_data()
    if not any(quote_prices(quotes.get(s)) for s in SYMBOLS):
        raise RuntimeError('No fresh valid crypto quotes; paper trading blocked')
    events = process(state, bars, quotes)
    # State is saved before notification to preserve simulated positions on rerun.
    save_state(state)
    for event in events:
        notify('HUSTLEWATCH CRYPTO | ' + iso(SCAN_TIME) + '\n' + event + '\nResearch/paper only; no trades placed.')
    # Every scan reports what prevented an entry, including unavailable data.
    mid = {s: (float(q['bp']) + float(q['ap'])) / 2
           for s,q in quotes.items() if s in SYMBOLS and quote_prices(q)}
    total = state['cash'] + sum(pos['qty'] * mid.get(s, pos['entry'])
                                for s, pos in state['positions'].items())
    rows = []
    for symbol in SYMBOLS:
        q = quote_prices(quotes.get(symbol))
        history = bars.get(symbol, [])
        if symbol in state['positions']:
            p = state['positions'][symbol]
            status = f"HOLD | stop ${p['stop']:,.2f} | target ${p['target']:,.2f}"
        elif q is None:
            status = 'BLOCKED: quote missing, stale, invalid or spread too wide'
        elif len(history) < 70:
            status = 'BLOCKED: insufficient closed candles'
        elif (SCAN_TIME - (parse_time(history[-1]['t']) + timedelta(minutes=5))).total_seconds() > 1200:
            status = 'BLOCKED: stale candles'
        elif symbol in state['cooldown'] and (SCAN_TIME-parse_time(state['cooldown'][symbol])).total_seconds() < 3600:
            status = 'WAIT: exit cooldown'
        elif setup(history, *q):
            status = 'SETUP: capacity/cash/risk checks apply'
        else:
            status = 'WAIT: breakout/trend/RSI/volume rules not met'
        px = f"${q[1]:,.4f}" if q else 'no fresh price'
        rows.append(f"{symbol} | {px} | {status}")
    message = ('HUSTLEWATCH CRYPTO | PAPER SCAN\n' + iso(SCAN_TIME)
               + f"\nCash ${state['cash']:.2f} | estimated equity ${total:.2f}"
               + f" | open {len(state['positions'])} | closed {len(state['closed'])}\n\n"
               + '\n'.join(rows) + '\nSimulated entries only when rules qualify; no real orders.')
    notify(message)
    state['last_heartbeat'] = iso(SCAN_TIME)
    state['last_scan'] = {'time': iso(SCAN_TIME), 'fresh_quotes': len(mid),
                          'events': len(events), 'symbols': len(SYMBOLS)}
    save_state(state)
    print(f'Completed: {len(events)} signal(s), {len(state["positions"])} open paper position(s).')


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, requests.RequestException, ValueError, KeyError) as exc:
        print(f'FAILED: {type(exc).__name__}: {str(exc)[:160]}', file=sys.stderr)
        sys.exit(1)
