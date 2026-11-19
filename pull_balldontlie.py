import json
import time
import urllib.request
import urllib.error
import os

from pathlib import Path

KEY = (os.environ.get("BALLDONTLIE_KEY") or
       (Path(os.environ.get("LOCALAPPDATA", Path.home())) / "soccer-betting-bot" / "bdl.key").read_text().strip())
OUT_DIR = os.path.join(os.path.dirname(__file__), "balldontlie_matches")
os.makedirs(OUT_DIR, exist_ok=True)

LEAGUES = ["laliga", "seriea", "ligue1", "epl"]
SEASONS = [2022, 2023, 2024, 2025, 2026]
ENDPOINT = {"laliga": "matches", "seriea": "matches", "ligue1": "matches", "epl": "games"}

SLEEP_SECONDS = 13  # free tier: 5 req/min -> stay under it


def fetch(url):
    req = urllib.request.Request(url, headers={"Authorization": KEY})
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.status, resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8")
            if e.code == 429:
                print(f"  rate limited, waiting 20s (attempt {attempt+1})")
                time.sleep(20)
                continue
            return e.code, body
    return None, None


def pull_league_season(league, season):
    cursor = None
    page = 1
    while True:
        url = f"https://api.balldontlie.io/{league}/v1/{ENDPOINT[league]}?season={season}&per_page=100"
        if cursor is not None:
            url += f"&cursor={cursor}"
        status, body = fetch(url)
        out_path = os.path.join(OUT_DIR, f"{league}_{season}_page{page}.json")
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(body if body else "")
        print(f"{league} {season} page{page} -> HTTP {status} -> {out_path}")

        if status != 200:
            break

        try:
            parsed = json.loads(body)
        except Exception as e:
            print(f"  failed to parse JSON: {e}")
            break

        data = parsed.get("data", [])
        meta = parsed.get("meta", {})
        next_cursor = meta.get("next_cursor")

        if not data or next_cursor is None or next_cursor == cursor or len(data) < 100:
            break

        cursor = next_cursor
        page += 1
        time.sleep(SLEEP_SECONDS)

    time.sleep(SLEEP_SECONDS)


if __name__ == "__main__":
    import sys
    leagues = sys.argv[1:] if len(sys.argv) > 1 else LEAGUES
    for league in leagues:
        for season in SEASONS:
            pull_league_season(league, season)
    print("ALL DONE")
