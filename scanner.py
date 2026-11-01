"""HustleWatch AI: concise Polymarket US sports quote watchlist. No bets placed."""
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from urllib.parse import quote

import requests

API = "https://gateway.polymarket.us/v1"

class ScannerError(Exception):
    pass

def required_env(name):
    value = os.environ.get(name, "").strip()
    if not value:
        raise ScannerError(f"Missing secret: {name}")
    return value

def telegram_token(value):
    token = "".join(value.split()).strip("\"'")
    if token.startswith("bot"):
        token = token[3:]
    if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", token):
        raise ScannerError("Invalid TELEGRAM_BOT_TOKEN")
    return token

def request_json(method, url, service, **kwargs):
    for attempt in range(3):
        try:
            response = method(url, timeout=20, allow_redirects=False, **kwargs)
        except requests.RequestException:
            raise ScannerError(f"{service} connection failed") from None
        if response.status_code == 429 and attempt < 2:
            time.sleep(10 * (attempt + 1))
            continue
        if not response.ok:
            raise ScannerError(f"{service} returned HTTP {response.status_code}")
        try:
            return response.json()
        except ValueError:
            raise ScannerError(f"{service} returned invalid JSON") from None
    raise ScannerError(f"{service} is rate limited")

def price(amount):
    try:
        if amount.get("currency") != "USD":
            return None
        value = Decimal(amount["value"])
        return value if value.is_finite() and Decimal(0) < value < Decimal(1) else None
    except (AttributeError, KeyError, InvalidOperation, TypeError):
        return None

def candidates(now):
    found = {}
    for offset in range(0, 2000, 500):
        data = request_json(requests.get, API + "/markets", "Polymarket US", params={
            "active":"true", "closed":"false", "categories":"sports",
            "orderBy":"id", "orderDirection":"desc",
            "limit":500, "offset":offset})
        markets = data.get("markets") if isinstance(data, dict) else None
        if not isinstance(markets, list):
            raise ScannerError("Unexpected Polymarket market response")
        for market in markets:
            if not isinstance(market, dict):
                continue
            if (market.get("category") != "sports"
                or not market.get("active") or market.get("closed") or market.get("hidden")
                or market.get("status") != "MARKET_STATUS_OPEN"):
                continue
            try:
                start = datetime.fromisoformat(market["gameStartTime"].replace("Z", "+00:00"))
                if start.tzinfo is None or not (now - timedelta(hours=6) <= start <= now + timedelta(hours=48)):
                    continue
                found[market["id"]] = market
            except (KeyError, ValueError, TypeError):
                continue
        if len(markets) < 500:
            break
        time.sleep(0.4)
    # Event start time alone does not verify that an event is currently live.
    return sorted(found.values(), key=lambda m: m["gameStartTime"], reverse=True)

def market_label(market):
    # Use provider metadata when present; never assume every sports market is a prop.
    kind = " ".join(str(market.get(k) or "") for k in
                    ("marketType", "sportsMarketType", "question", "title")).lower()
    if "prop" in kind or "player" in kind:
        return "PLAYER PROP"
    if any(word in kind for word in ("moneyline", "money line", "match winner", "game winner", "to win")):
        return "MONEYLINE"
    return "OTHER SPORTS"

def quote_card(market, data):
    if not isinstance(data, dict) or data.get("state") != "MARKET_STATE_OPEN":
        return None
    bid, ask = price(data.get("bestBid")), price(data.get("bestAsk"))
    if bid is None or ask is None or bid > ask or ask - bid > Decimal("0.05"):
        return None
    yes_cost, no_cost = ask, Decimal(1) - bid
    if not (Decimal("0.15") <= yes_cost <= Decimal("0.85")
            and Decimal("0.15") <= no_cost <= Decimal("0.85")):
        return None
    question = " ".join(str(market.get("question", "Unknown prop")).split())
    if len(question) > 115:
        question = question[:112] + "..."
    label = market_label(market)
    # These are quotes only; event status and independent edge are unverified.
        status = "UPCOMING" if datetime.fromisoformat(market["gameStartTime"].replace("Z", "+00:00")) > datetime.now(timezone.utc) else "STARTED (live unverified)"
        return (f"{label} | {status}\n{question}\n"
            f"YES {yes_cost * 100:.0f}¢ | NO {no_cost * 100:.0f}¢\n"
            "Status: WATCH ONLY — no verified edge")

def send_telegram(token, chat_id, text):
    result = request_json(requests.post, f"https://api.telegram.org/bot{token}/sendMessage",
        "Telegram", json={"chat_id":chat_id,"text":text,"disable_web_page_preview":True})
    if not isinstance(result,dict) or result.get("ok") is not True:
        raise ScannerError("Telegram did not confirm delivery")

def main():
    dry_run = "--dry-run" in sys.argv
    token = telegram_token(required_env("TELEGRAM_BOT_TOKEN")) if not dry_run else None
    chat_id = required_env("TELEGRAM_CHAT_ID") if not dry_run else None
    markets = candidates(datetime.now(timezone.utc))
    cards = []
    seen_groups = set()
    type_counts = {"MONEYLINE": 0, "PLAYER PROP": 0, "OTHER SPORTS": 0}
    for market in markets[:150]:
        group = (market.get("gameStartTime"),market.get("subjectId"),
                 str(market.get("question", "")).split("above")[0])
        if group in seen_groups:
            continue
        kind = market_label(market)
        if type_counts[kind] >= 3:
            continue
        slug = market.get("slug")
        if not slug:
            continue
        data = request_json(requests.get, API + "/markets/" + quote(slug,safe="") + "/bbo", "Polymarket US")
        card = quote_card(market, data.get("marketData")) if isinstance(data,dict) else None
        if card:
            cards.append(card)
            seen_groups.add(group)
            type_counts[kind] += 1
        if len(cards) >= 9:
            break
        time.sleep(0.4)
    # No spam when no eligible market; logs still show what happened.
    if not cards:
        print("NO TRADE: no markets with acceptable quotes. Nothing sent.")
        return
    body = "HustleWatch AI | SPORTS WATCH\n\n" + "\n\n".join(cards)
    body += "\n\nStarted does NOT confirm live play. Quotes only; NO BUY signal without verified status and edge."
    if dry_run:
        print(body)
    else:
        send_telegram(token, chat_id, body)
    print(f"Reported {len(cards)} watchlist markets in one message.")

if __name__ == "__main__":
    try:
        main()
    except ScannerError as exc:
        print(f"Scanner failed: {exc}", file=sys.stderr)
        sys.exit(1)
