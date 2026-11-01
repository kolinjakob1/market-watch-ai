
import os
import requests

key = os.environ["ODDS_API_KEY"]

url = "https://api.the-odds-api.com/v4/sports/upcoming/odds"

response = requests.get(
    url,
    params={
        "apiKey": key,
        "regions": "us",
        "markets": "h2h",
        "oddsFormat": "american"
    },
    timeout=20
)

response.raise_for_status()

for game in response.json():
    print(f"{game['away_team']} vs {game['home_team']}")
    for book in game.get("bookmakers", [])[:2]:
        for market in book.get("markets", []):
            for pick in market.get("outcomes", []):
                print(pick["name"], pick["price"])
