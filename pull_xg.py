import json
import time
import urllib.request
import urllib.error
import os
import subprocess
from pathlib import Path

TASK_NAME = "api-football-xg-backfill"

KEY = os.environ.get("API_FOOTBALL_KEY") or (
    Path(os.environ.get("LOCALAPPDATA", Path.home())) / "soccer-betting-bot" / "api-football.key"
).read_text().strip()
BASE = os.path.dirname(__file__)
FIXTURES_DIR = os.path.join(BASE, "fixtures")
XG_DIR = os.path.join(BASE, "xg")
CHECKPOINT = os.path.join(BASE, "xg_progress.json")
os.makedirs(XG_DIR, exist_ok=True)

TARGET_FILES = [
    "la-liga_140_2022.json", "la-liga_140_2023.json", "la-liga_140_2024.json",
    "serie-a_135_2022.json", "serie-a_135_2023.json", "serie-a_135_2024.json",
]

DAILY_BUDGET = 90  # leave headroom under the 100/day cap for a /status check etc.
SLEEP_SECONDS = 6.5  # free tier: 10 req/min -> stay under it


def load_checkpoint():
    if os.path.exists(CHECKPOINT):
        with open(CHECKPOINT, "r", encoding="utf-8") as f:
            return set(json.load(f))
    return set()


def save_checkpoint(done):
    with open(CHECKPOINT, "w", encoding="utf-8") as f:
        json.dump(sorted(done), f)


def target_fixture_ids():
    ids = []
    for fname in TARGET_FILES:
        path = os.path.join(FIXTURES_DIR, fname)
        if not os.path.exists(path):
            continue
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        for item in data.get("response", []):
            if item["fixture"]["status"]["short"] == "FT":
                ids.append(item["fixture"]["id"])
    return ids


def fetch_stats(fixture_id):
    url = f"https://v3.football.api-sports.io/fixtures/statistics?fixture={fixture_id}"
    req = urllib.request.Request(url, headers={"x-apisports-key": KEY})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")


def main():
    done = load_checkpoint()
    all_ids = target_fixture_ids()
    remaining = [i for i in all_ids if i not in done]

    print(f"Total target fixtures: {len(all_ids)} | already done: {len(done)} | remaining: {len(remaining)}")

    if not remaining:
        print("ALL XG DATA COLLECTED")
        return

    pulled_this_run = 0
    for fixture_id in remaining:
        if pulled_this_run >= DAILY_BUDGET:
            print(f"Hit daily budget ({DAILY_BUDGET}). Stopping for today.")
            break

        status, body = fetch_stats(fixture_id)
        out_path = os.path.join(XG_DIR, f"fixture_{fixture_id}.json")
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(body)

        if status == 200 and '"rateLimit"' not in body:
            done.add(fixture_id)
            save_checkpoint(done)
            pulled_this_run += 1
            print(f"[{pulled_this_run}/{DAILY_BUDGET}] fixture {fixture_id} -> HTTP {status} OK")
        else:
            print(f"fixture {fixture_id} -> HTTP {status} FAILED, will retry next run: {body[:150]}")
            if '"rateLimit"' in body:
                print("Rate limited, stopping this run early.")
                break

        time.sleep(SLEEP_SECONDS)

    still_remaining = len(all_ids) - len(done)
    print(f"Run complete. Total done: {len(done)}/{len(all_ids)}. Still remaining: {still_remaining}")
    if still_remaining == 0:
        print("ALL XG DATA COLLECTED")
        subprocess.run(
            ["schtasks", "/Delete", "/TN", TASK_NAME, "/F"],
            capture_output=True, text=True,
        )
        print(f"Removed scheduled task '{TASK_NAME}'.")


if __name__ == "__main__":
    main()
