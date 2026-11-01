"""Send exact Polymarket US sports props without logging credentials."""
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from urllib.parse import quote

import requests


class ScannerError(Exception):
    """An error whose message is safe to include in Actions logs."""


def required_env(name):
    value = os.environ.get(name, "").strip()
    if not value:
        raise ScannerError(f"Missing required GitHub Actions secret: {name}")
    return value


def telegram_token(value):
    # Copy/paste can add whitespace, quotes, or the URL's 'bot' prefix.
    token = "".join(value.split()).strip("\"'").strip()
    if token.startswith("bot"):
        token = token[3:]
    if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", token):
        raise ScannerError("TELEGRAM_BOT_TOKEN has an invalid format. Save only the BotFather token in the repository secret.")
    return token


def request_json(method, url, service, **kwargs):
    try:
        for attempt in range(4):
            response = method(url, timeout=20, allow_redirects=False, **kwargs)
            if response.status_code != 429 or service != "Polymarket US" or attempt == 3:
                break
            time.sleep(10 * (attempt + 1))
    except requests.RequestException:
        # requests exception strings may contain URLs with API credentials.
        raise ScannerError(f"{service} request failed; check connectivity and retry.") from None
    if not 200 <= response.status_code < 300:
        if service == "Telegram" and response.status_code in (401, 404):
            raise ScannerError(f"Telegram HTTP {response.status_code}: check TELEGRAM_BOT_TOKEN in GitHub Actions secrets; the token may be invalid or revoked.")
        if service == "Telegram" and response.status_code in (400, 403):
            raise ScannerError(f"Telegram HTTP {response.status_code}: check TELEGRAM_CHAT_ID and start/unblock the bot in Telegram.")
        raise ScannerError(f"{service} HTTP {response.status_code}; request failed.")
    try:
        return response.json()
    except ValueError:
        raise ScannerError(f"{service} returned invalid JSON.") from None


API = "https://gateway.polymarket.us/v1"


def candidates(now):
    """Bound discovery to 5,000 recent props; never claim full coverage."""
    found = {}
    for offset in range(0, 5000, 500):
        time.sleep(1)
        data = request_json(requests.get, API + "/markets", "Polymarket US",
            params={"active": "true", "closed": "false", "categories": "sports",
                    "marketTypes": "props", "orderBy": "id", "orderDirection": "desc",
                    "limit": 500, "offset": offset})
        markets = data.get("markets") if isinstance(data, dict) else None
        if not isinstance(markets, list):
            raise ScannerError("Polymarket US returned an unexpected response.")
        for market in markets:
            if (market.get("category") != "sports" or market.get("marketType") != "props"
                    or market.get("closed") or not market.get("active")
                    or market.get("hidden") or market.get("status") != "MARKET_STATUS_OPEN"):
                continue
            try:
                start = datetime.fromisoformat(market["gameStartTime"].replace("Z", "+00:00"))
                if start.tzinfo is None:
                    continue
            except (KeyError, TypeError, ValueError):
                continue
            if now - timedelta(hours=6) <= start <= now + timedelta(hours=24):
                found[market["id"]] = market
        if len(markets) < 500:
            break
    return sorted(found.values(), key=lambda m: (m["gameStartTime"], m["id"]))


def price(amount):
    try:
        if amount.get("currency") != "USD":
            return None
        value = Decimal(amount["value"])
        return value if value.is_finite() and 0 < value < 1 else None
    except (AttributeError, KeyError, TypeError, InvalidOperation):
        return None


def prop_message(market, data, now):
    if data.get("state") != "MARKET_STATE_OPEN":
        return None
    bid, ask = price(data.get("bestBid")), price(data.get("bestAsk"))
    if bid is not None and ask is not None and bid > ask:
        return None
    lines = ["HustleWatch AI | Polymarket US PROP", market["question"]]
    if market.get("title"):
        lines.append("Contract: " + market["title"])
    lines.append("Game start (UTC): " + market["gameStartTime"])
    sides = market.get("marketSides", [])
    for side in sides:
        if side.get("tradable") is not True or not isinstance(side.get("long"), bool):
            continue
        cost = ask if side["long"] else (1 - bid if bid is not None else None)
        label = side.get("description") or ("Yes" if side["long"] else "No")
        lines.append(f"{label}: {cost * 100:.2f} cents/contract (best offer)" if cost is not None
                     else f"{label}: no available offer")
    if ask is not None and bid is not None:
        lines.append(f"Bid/ask spread: {(ask - bid) * 100:.2f} cents")
    lines.extend(["Retrieved (UTC): " + now.isoformat(timespec="seconds"),
        "Market ID: " + str(market["id"]),
        "Market data: " + API + "/market/slug/" + quote(market["slug"], safe=""),
        "Quotes exclude fees; availability and prices can change. Market listing, not a recommended bet."])
    return "\n".join(lines)


def main():
    dry_run = "--dry-run" in sys.argv
    if not dry_run:
        token = telegram_token(required_env("TELEGRAM_BOT_TOKEN"))
        chat_id = required_env("TELEGRAM_CHAT_ID")
    now = datetime.now(timezone.utc)
    markets = candidates(now)
    messages, groups = [], {}
    for market in markets[:60]:
        # Avoid filling the digest with alternate lines of one prop.
        group = (market.get("gameStartTime"), market.get("sportsMarketType"),
                 market.get("subjectId"), market["question"].split("above")[0])
        if groups.get(group, 0) >= 1:
            continue
        time.sleep(1)
        data = request_json(requests.get,
            API + "/markets/" + quote(market["slug"], safe="") + "/bbo", "Polymarket US")
        message = prop_message(market, data.get("marketData", {}), datetime.now(timezone.utc))
        if message:
            messages.append(message)
            groups[group] = 1
        if len(messages) >= 12:
            break
    summary = (f"HustleWatch AI | Polymarket US\n{len(messages)} exact prop listings follow. "
        f"{len(markets)} eligible props found among up to 5,000 recent listings. "
        "Window: games started within 6 hours or starting in the next 24 hours. "
        "Up to 12 listings per scan; not ranked by profit. "
        "This bot sends scheduled alerts; chat replies are not monitored.")
    if not messages:
        summary += " No matching open props were found in this scan; no picks invented."
    for message in [summary] + messages:
        if dry_run:
            print(message + "\n")
            continue
        result = request_json(requests.post, f"https://api.telegram.org/bot{token}/sendMessage", "Telegram",
            json={"chat_id": chat_id, "text": message, "disable_web_page_preview": True})
        if not isinstance(result, dict) or result.get("ok") is not True:
            raise ScannerError("Telegram did not confirm delivery.")
        time.sleep(1.1)
    print(f"Delivered {len(messages)} prop listings." if not dry_run else "Dry run complete; nothing sent.")


if __name__ == "__main__":
    try:
        main()
    except ScannerError as error:
        print(f"Scanner failed: {error}", file=sys.stderr)
        sys.exit(1)
