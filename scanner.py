"""Fetch sports odds and send a completion alert without logging credentials."""
import os
import re
import sys

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
    token = value.strip().strip("\"'").strip()
    if token.startswith("bot"):
        token = token[3:]
    if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", token):
        raise ScannerError("TELEGRAM_BOT_TOKEN has an invalid format. Save only the BotFather token in the repository secret.")
    return token


def request_json(method, url, service, **kwargs):
    try:
        response = method(url, timeout=20, allow_redirects=False, **kwargs)
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


def main():
    # Validate everything before consuming an odds API request.
    key = required_env("ODDS_API_KEY")
    token = telegram_token(required_env("TELEGRAM_BOT_TOKEN"))
    chat_id = required_env("TELEGRAM_CHAT_ID")
    games = request_json(
        requests.get, "https://api.the-odds-api.com/v4/sports/upcoming/odds", "Odds API",
        params={"apiKey": key, "regions": "us", "markets": "h2h", "oddsFormat": "american"},
    )
    if not isinstance(games, list):
        raise ScannerError("Odds API returned an unexpected response.")
    for game in games:
        print(f"{game['away_team']} vs {game['home_team']}")
        for book in game.get("bookmakers", [])[:2]:
            for market in book.get("markets", []):
                for pick in market.get("outcomes", []):
                    print(pick["name"], pick["price"])
    result = request_json(
        requests.post, f"https://api.telegram.org/bot{token}/sendMessage", "Telegram",
        json={"chat_id": chat_id, "text": f"Market Watch AI\nSports odds scan completed!\nGames found: {len(games)}"},
    )
    if not isinstance(result, dict) or result.get("ok") is not True:
        raise ScannerError("Telegram did not confirm delivery; check the bot token and chat ID.")
    print("Telegram notification sent.")


if __name__ == "__main__":
    try:
        main()
    except ScannerError as error:
        print(f"Scanner failed: {error}", file=sys.stderr)
        sys.exit(1)
