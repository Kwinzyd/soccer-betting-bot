import json
import time
import urllib.request
import urllib.error
import os
import glob

from pathlib import Path

KEY = (os.environ.get("BALLDONTLIE_KEY") or
       (Path(os.environ.get("LOCALAPPDATA", Path.home())) / "soccer-betting-bot" / "bdl.key").read_text().strip())
BASE = os.path.dirname(__file__)

TEAMS_DIR = os.path.join(BASE, "balldontlie_teams")
MATCHES_DIR = os.path.join(BASE, "balldontlie_matches")
STATS_DIR = os.path.join(BASE, "balldontlie_team_match_stats")
STATS_CHECKPOINT = os.path.join(BASE, "balldontlie_team_match_stats_progress.json")

for d in (TEAMS_DIR, MATCHES_DIR, STATS_DIR):
    os.makedirs(d, exist_ok=True)

LEAGUES = ["laliga", "seriea", "ligue1", "epl", "bundesliga"]

# Endpoint version differs per league: laliga/seriea/ligue1/bundesliga expose
# the rich schema at v1 (v2 404s "Route not found"); epl only has the rich
# schema at v2 (its v1 is the old "games" endpoint with a different team-id space).
VERSION = {"laliga": "v1", "seriea": "v1", "ligue1": "v1", "epl": "v2",
           "bundesliga": "v1"}

MATCH_SEASONS = [2022, 2023, 2024, 2025, 2026]
TEAM_SEASONS = [2025, 2026]
STATS_SEASONS = [2025, 2026]

# laliga/seriea/ligue1 were already pulled correctly in an earlier run;
# only re-pull matches for leagues in this set.
MATCHES_FORCE = {"epl", "bundesliga"}

STATS_BATCH_SIZE = 50
SLEEP_SECONDS = 13  # observed x-ratelimit-limit: 5 req/min across all endpoints on this key


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


def pull_teams():
    for league in LEAGUES:
        version = VERSION[league]
        for season in TEAM_SEASONS:
            out_path = os.path.join(TEAMS_DIR, f"{league}_teams_{season}.json")
            url = f"https://api.balldontlie.io/{league}/{version}/teams?season={season}"
            status, body = fetch(url)
            with open(out_path, "w", encoding="utf-8") as f:
                f.write(body if body else "")
            print(f"[teams] {league} {season} -> HTTP {status} -> {out_path}")
            time.sleep(SLEEP_SECONDS)


def pull_matches():
    for league in LEAGUES:
        version = VERSION[league]
        for season in MATCH_SEASONS:
            existing = glob.glob(os.path.join(MATCHES_DIR, f"{league}_{season}_page*.json"))
            if existing and league not in MATCHES_FORCE:
                print(f"[matches] {league} {season} -> skipping, {len(existing)} page(s) already present")
                continue

            for old in existing:
                os.remove(old)

            cursor = None
            page = 1
            while True:
                url = f"https://api.balldontlie.io/{league}/{version}/matches?season={season}&per_page=100"
                if cursor is not None:
                    url += f"&cursor={cursor}"
                status, body = fetch(url)
                out_path = os.path.join(MATCHES_DIR, f"{league}_{season}_page{page}.json")
                with open(out_path, "w", encoding="utf-8") as f:
                    f.write(body if body else "")
                print(f"[matches] {league} {season} page{page} -> HTTP {status} -> {out_path}")

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


def load_finished_match_ids(league, season):
    ids = []
    for path in sorted(glob.glob(os.path.join(MATCHES_DIR, f"{league}_{season}_page*.json"))):
        with open(path, "r", encoding="utf-8") as f:
            try:
                parsed = json.load(f)
            except Exception:
                continue
        for m in parsed.get("data", []):
            if m.get("status_state") == "final":
                ids.append(m["id"])
    return ids


def load_stats_checkpoint():
    if os.path.exists(STATS_CHECKPOINT):
        with open(STATS_CHECKPOINT, "r", encoding="utf-8") as f:
            return {k: set(v) for k, v in json.load(f).items()}
    return {}


def save_stats_checkpoint(done):
    with open(STATS_CHECKPOINT, "w", encoding="utf-8") as f:
        json.dump({k: sorted(v) for k, v in done.items()}, f)


def pull_team_match_stats():
    done = load_stats_checkpoint()

    for league in LEAGUES:
        version = VERSION[league]
        league_done = done.setdefault(league, set())

        all_ids = []
        for season in STATS_SEASONS:
            all_ids.extend(load_finished_match_ids(league, season))

        remaining = [i for i in all_ids if i not in league_done]
        if not remaining:
            print(f"[team_match_stats] {league} -> nothing to do ({len(all_ids)} finished matches, all done)")
            continue

        print(f"[team_match_stats] {league} -> {len(remaining)} of {len(all_ids)} finished matches remaining")

        for batch_start in range(0, len(remaining), STATS_BATCH_SIZE):
            batch = remaining[batch_start:batch_start + STATS_BATCH_SIZE]
            query = "&".join(f"match_ids[]={mid}" for mid in batch)
            url = f"https://api.balldontlie.io/{league}/{version}/team_match_stats?{query}&per_page=100"
            status, body = fetch(url)

            batch_num = batch_start // STATS_BATCH_SIZE + 1
            out_path = os.path.join(STATS_DIR, f"{league}_batch{batch_num}_{batch[0]}-{batch[-1]}.json")
            with open(out_path, "w", encoding="utf-8") as f:
                f.write(body if body else "")
            print(f"[team_match_stats] {league} batch{batch_num} ({len(batch)} matches) -> HTTP {status} -> {out_path}")

            if status == 200:
                league_done.update(batch)
                save_stats_checkpoint(done)
            else:
                print(f"  failed, will retry next run: {body[:150] if body else body}")

            time.sleep(SLEEP_SECONDS)


if __name__ == "__main__":
    import sys
    phases = sys.argv[1:] if len(sys.argv) > 1 else ["teams", "matches", "stats"]

    if "teams" in phases:
        pull_teams()
    if "matches" in phases:
        pull_matches()
    if "stats" in phases:
        pull_team_match_stats()

    print("ALL DONE")
