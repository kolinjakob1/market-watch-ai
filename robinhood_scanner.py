"""Stock + options watchlist scanner. Research/paper only; never places orders."""
import os
import sys
from datetime import datetime, timezone

import requests

API = "https://data.alpaca.markets"
SYMBOLS = [s.strip().upper() for s in os.getenv("WATCHLIST", "AAPL,NVDA,TSLA,SPY,QQQ").split(",") if s.strip()]
ALERT_MOVE_PCT = float(os.getenv("ALERT_MOVE_PCT", "2"))
MAX_QUOTE_AGE = 180  # seconds
PAPER_BALANCE = 500.0  # reference bankroll, not persistent cash accounting


def age_seconds(timestamp):
    try:
        dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - dt).total_seconds()
    except (TypeError, ValueError, AttributeError):
        return float("inf")


def get_json(path, params):
    response = requests.get(
        API + path,
        headers={"APCA-API-KEY-ID": os.environ["ALPACA_API_KEY"],
                 "APCA-API-SECRET-KEY": os.environ["ALPACA_SECRET_KEY"]},
        params=params, timeout=20)
    response.raise_for_status()
    return response.json()


def send_alert(message):
    if os.getenv("DRY_RUN") == "1":
        print(message)
        return
    response = requests.post(
        "https://api.telegram.org/bot" + os.environ["TELEGRAM_BOT_TOKEN"] + "/sendMessage",
        json={"chat_id": os.environ["TELEGRAM_CHAT_ID"],
              "text": message, "disable_web_page_preview": True}, timeout=20)
    response.raise_for_status()
    if not response.json().get("ok"):
        raise RuntimeError("Telegram rejected the message")


def scan_stocks():
    data = get_json("/v2/stocks/snapshots", {"symbols": ",".join(SYMBOLS), "feed": "iex"})
    signals = []
    for symbol in SYMBOLS:
        snap = data.get(symbol) or {}
        quote, today, prev = snap.get("latestQuote") or {}, snap.get("dailyBar") or {}, snap.get("prevDailyBar") or {}
        bid, ask = quote.get("bp") or 0, quote.get("ap") or 0
        prev_close = prev.get("c") or 0
        if not (bid > 0 and ask >= bid and prev_close > 0):
            continue
        age = age_seconds(quote.get("t"))
        if age < -10 or age > MAX_QUOTE_AGE:
            continue
        spread = (ask - bid) / ask * 100
        move = (ask / prev_close - 1) * 100
        if spread > 0.35 or abs(move) < ALERT_MOVE_PCT:
            continue
        paper_budget = min(25.0, PAPER_BALANCE * 0.05)
        shares = int(paper_budget // ask)
        signals.append((symbol, move, f"STOCK WATCH | {symbol}\n"
            f"IEX ask ${ask:.2f} | bid ${bid:.2f} | quote {age:.0f}s old\n"
            f"Change vs prior close: {move:+.1f}% | spread {spread:.2f}%\n"
            f"Paper budget ${paper_budget:.2f} ({shares} whole shares at ask)\n"
            "Momentum alert only, not a verified profitable trade."))
    return sorted(signals, key=lambda x: -abs(x[1]))[:3]


def scan_options():
    results = []
    for symbol in SYMBOLS[:3]:
        try:
            data = get_json(f"/v1beta1/options/snapshots/{symbol}", {"feed": "indicative", "limit": 100})
        except requests.HTTPError:
            print(f"Options feed unavailable for {symbol}; continuing stock scan.")
            continue
        snapshots = data.get("snapshots") or {}
        count = 0
        for contract, snap in snapshots.items():
            quote = snap.get("latestQuote") or {}
            bid, ask = quote.get("bp") or 0, quote.get("ap") or 0
            if not (bid > 0 and ask >= bid):
                continue
            spread = (ask - bid) / ask * 100
            if spread > 10:
                continue
            count += 1
        if count:
            results.append(f"OPTIONS WATCH | {symbol}: {count} indicative contracts with displayed spread <=10%. "
                "Indicative quotes are modified/not executable; confirm real broker quotes before any trade.")
    return results


def main():
    if not os.getenv("ALPACA_API_KEY") or not os.getenv("ALPACA_SECRET_KEY"):
        raise RuntimeError("Set ALPACA_API_KEY and ALPACA_SECRET_KEY secrets")
    stocks = scan_stocks()
    for _, _, message in stocks:
        send_alert(message)
    # Options are research-only; avoid recommending contracts based on indicative prices.
    options = scan_options()
    print(f"Scan complete: {len(stocks)} stock signals; options coverage: {len(options)} symbols")
    for message in options:
        print(message)
    if not stocks:
        print("NO TRADE SIGNAL: no fresh stock setups met screening rules")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # Do not print API credentials or full request URLs.
        print(f"Scanner failed ({type(exc).__name__}). Check Actions logs and secrets.", file=sys.stderr)
        sys.exit(1)
