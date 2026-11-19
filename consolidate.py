import json
import glob
import os

BASE = os.path.dirname(__file__)
MATCHES_DIR = os.path.join(BASE, "balldontlie_matches")
TEAMS_DIR = os.path.join(BASE, "balldontlie_teams")
STATS_DIR = os.path.join(BASE, "balldontlie_team_match_stats")
OUT_DIR = os.path.join(BASE, "balldontlie_consolidated")
os.makedirs(OUT_DIR, exist_ok=True)

LEAGUES = ["laliga", "seriea", "ligue1", "epl"]
MATCH_SEASONS = [2022, 2023, 2024, 2025, 2026]
TEAM_SEASONS = [2025, 2026]
STATS_SEASONS = [2025, 2026]


def write(path, records):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False)
    print(f"{path} -> {len(records)} records")


def consolidate_matches():
    match_id_to_season = {}  # (league, match_id) -> season
    for league in LEAGUES:
        for season in MATCH_SEASONS:
            pages = sorted(glob.glob(os.path.join(MATCHES_DIR, f"{league}_{season}_page*.json")))
            records = []
            for p in pages:
                with open(p, "r", encoding="utf-8") as f:
                    parsed = json.load(f)
                records.extend(parsed.get("data", []))
            for r in records:
                match_id_to_season[(league, r["id"])] = season
            out_path = os.path.join(OUT_DIR, f"{league}_{season}_matches.json")
            write(out_path, records)
    return match_id_to_season


def consolidate_teams():
    for league in LEAGUES:
        for season in TEAM_SEASONS:
            src = os.path.join(TEAMS_DIR, f"{league}_teams_{season}.json")
            if not os.path.exists(src):
                continue
            with open(src, "r", encoding="utf-8") as f:
                parsed = json.load(f)
            records = parsed.get("data", [])
            out_path = os.path.join(OUT_DIR, f"{league}_{season}_teams.json")
            write(out_path, records)


def consolidate_stats(match_id_to_season):
    for league in LEAGUES:
        by_season = {s: [] for s in STATS_SEASONS}
        unmatched = []
        batches = sorted(glob.glob(os.path.join(STATS_DIR, f"{league}_batch*.json")))
        for b in batches:
            with open(b, "r", encoding="utf-8") as f:
                parsed = json.load(f)
            for rec in parsed.get("data", []):
                season = match_id_to_season.get((league, rec["match_id"]))
                if season in by_season:
                    by_season[season].append(rec)
                else:
                    unmatched.append(rec)
        for season in STATS_SEASONS:
            out_path = os.path.join(OUT_DIR, f"{league}_{season}_team_match_stats.json")
            write(out_path, by_season[season])
        if unmatched:
            print(f"  {league}: {len(unmatched)} stat records with no matching match id/season")


if __name__ == "__main__":
    match_id_to_season = consolidate_matches()
    consolidate_teams()
    consolidate_stats(match_id_to_season)
    print("DONE")
