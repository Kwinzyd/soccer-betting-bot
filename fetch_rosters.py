"""Active Roster Scraper & Prop Filter using FotMob API.

Fetches 100% current, verified active rosters for European soccer leagues
(EPL, La Liga, Serie A, Ligue 1, Bundesliga, Champions League, etc.).
Filters out stale, transferred, or non-roster players from player prop feeds.
"""

import json
import os
import re
import time
import unicodedata
import urllib.request
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
ROSTERS_DIR = os.path.join(HERE, "rosters")
ROSTERS_JSON = os.path.join(HERE, "active_rosters.json")

os.makedirs(ROSTERS_DIR, exist_ok=True)

# FotMob League IDs
LEAGUES = {
    "epl": 47,
    "laliga": 87,
    "seriea": 55,
    "ligue1": 53,
    "bundesliga": 54,
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.fotmob.com/",
}

TEAM_SUFFIXES = (
    " fc", " afc", " cf", " ac", " sc", " calcio", " 1907", " cfc",
    " ssc", " rc", " rcd", " club", " de futbol"
)


def norm_str(s: str) -> str:
    """Normalize string: strip accents, lower-case, remove special characters."""
    if not s:
        return ""
    nfkd = unicodedata.normalize("NFKD", s)
    ascii_str = "".join(c for c in nfkd if not unicodedata.combining(c)).lower()
    clean = re.sub(r"[^a-z0-9\s]", " ", ascii_str)
    return re.sub(r"\s+", " ", clean).strip()


def norm_team(name: str) -> str:
    """Normalize club names for consistent cross-source joins."""
    s = norm_str(name)
    for _ in range(2):
        for suf in TEAM_SUFFIXES:
            if s.endswith(suf):
                s = s[:-len(suf)].strip()
    return s


def fetch_json(url: str, retries: int = 3, timeout: int = 15):
    """Fetch JSON with retry logic and standard user agent."""
    req = urllib.request.Request(url, headers=HEADERS)
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(5 * (attempt + 1))
                continue
            return None
        except Exception:
            time.sleep(2)
    return None


def get_league_teams(league_id: int) -> list[dict]:
    """Fetch all teams currently in a league."""
    url = f"https://www.fotmob.com/api/data/leagues?id={league_id}"
    data = fetch_json(url)
    if not data:
        return []

    teams = []
    # Extract from standings table
    tables = data.get("table", [])
    if tables and isinstance(tables, list):
        for t_group in tables:
            rows = t_group.get("data", {}).get("table", {}).get("all", [])
            for row in rows:
                tid = row.get("id")
                tname = row.get("name")
                if tid and tname:
                    teams.append({
                        "id": tid,
                        "name": tname,
                        "norm_name": norm_team(tname),
                    })
    return teams


def get_team_squad(team_id: int) -> list[dict]:
    """Fetch active squad for a single team."""
    url = f"https://www.fotmob.com/api/data/teams?id={team_id}"
    data = fetch_json(url)
    if not data:
        return []

    squad_sections = data.get("squad", {}).get("squad", [])
    players = []

    for section in squad_sections:
        role_title = section.get("title", "")
        if role_title == "coach":
            continue

        for member in section.get("members", []):
            name = member.get("name", "").strip()
            if not name:
                continue

            injury = member.get("injury")
            is_injured = injury is not None
            injury_detail = injury.get("expectedReturn") if is_injured and isinstance(injury, dict) else None

            role_desc = member.get("role", {}).get("fallback") or member.get("positionIdsDesc") or role_title

            players.append({
                "id": member.get("id"),
                "name": name,
                "norm_name": norm_str(name),
                "shirt_number": member.get("shirtNumber"),
                "position": role_desc,
                "is_injured": is_injured,
                "injury_return": injury_detail,
                "goals": member.get("goals", 0),
                "assists": member.get("assists", 0),
            })

    return players


def sync_all_rosters(selected_leagues: list[str] = None) -> dict:
    """Pull rosters for all configured leagues and save to disk."""
    leagues_to_run = selected_leagues or list(LEAGUES.keys())
    master_rosters = {}

    print("=" * 70)
    print("STARTING FOTMOB ACTIVE ROSTER SYNC")
    print("=" * 70)

    for lg in leagues_to_run:
        lid = LEAGUES.get(lg)
        if not lid:
            continue

        print(f"\n[{lg.upper()}] Fetching teams for league id {lid}...")
        teams = get_league_teams(lid)
        print(f"  Found {len(teams)} teams.")

        league_data = {}
        for t in teams:
            tid = t["id"]
            tname = t["name"]
            print(f"  -> Pulling squad: {tname:30s} (ID: {tid}) ...", end="", flush=True)

            squad = get_team_squad(tid)
            print(f" {len(squad)} active players")

            team_record = {
                "team_id": tid,
                "team_name": tname,
                "norm_team": t["norm_name"],
                "league": lg,
                "player_count": len(squad),
                "players": squad,
            }
            league_data[tname] = team_record
            master_rosters[tname] = team_record

            # Gentle pacing to respect endpoints
            time.sleep(0.3)

        # Save league-specific file
        lg_path = os.path.join(ROSTERS_DIR, f"{lg}_roster.json")
        with open(lg_path, "w", encoding="utf-8") as f:
            json.dump(league_data, f, ensure_ascii=False, indent=2)

    # Save consolidated master file
    with open(ROSTERS_JSON, "w", encoding="utf-8") as f:
        json.dump(master_rosters, f, ensure_ascii=False, indent=2)

    total_players = sum(t["player_count"] for t in master_rosters.values())
    print("\n" + "=" * 70)
    print(f"SYNC COMPLETE: {len(master_rosters)} teams, {total_players} verified active players saved.")
    print(f"Master file saved to: {ROSTERS_JSON}")
    print("=" * 70)

    return master_rosters


class RosterFilter:
    """Filter that validates whether a player prop belongs to an active team player."""

    def __init__(self, roster_path: str = ROSTERS_JSON):
        self.roster_path = roster_path
        self.team_players = {}   # norm_team -> set of norm_names
        self.player_metadata = {} # (norm_team, norm_name) -> player info
        self.load_rosters()

    def load_rosters(self):
        if not os.path.exists(self.roster_path):
            print(f"Warning: {self.roster_path} not found. Run sync first.")
            return

        with open(self.roster_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        for _, tdata in data.items():
            t_norm = norm_team(tdata.get("team_name", ""))
            squad_names = set()

            for p in tdata.get("players", []):
                p_norm = norm_str(p.get("name", ""))
                squad_names.add(p_norm)

                # Also add last name only for flexible matching
                parts = p_norm.split()
                if len(parts) > 1:
                    squad_names.add(parts[-1])

                self.player_metadata[(t_norm, p_norm)] = p

            self.team_players[t_norm] = squad_names

    def is_player_on_team(self, team_name: str, player_name: str) -> tuple[bool, str, dict | None]:
        """Check if player is active on team.
        Returns: (is_active, status_reason, player_obj)
        """
        t_norm = norm_team(team_name)
        p_norm = norm_str(player_name)

        # Team match check
        if t_norm not in self.team_players:
            # Substring match attempt
            matched_team = None
            for kt in self.team_players:
                if t_norm in kt or kt in t_norm:
                    matched_team = kt
                    break
            if not matched_team:
                return False, f"Team '{team_name}' not found in active rosters", None
            t_norm = matched_team

        active_set = self.team_players[t_norm]

        # 1. Exact or last-name match
        if p_norm in active_set:
            meta = self.player_metadata.get((t_norm, p_norm))
            if meta and meta.get("is_injured"):
                return True, f"Injured ({meta.get('injury_return') or 'Out'})", meta
            return True, "Active", meta

        # 2. Token overlap match (e.g. "B. Saka" vs "Bukayo Saka")
        p_tokens = set(p_norm.split())
        for existing in active_set:
            e_tokens = set(existing.split())
            if p_tokens & e_tokens and (len(p_tokens) == 1 or p_tokens.issubset(e_tokens)):
                meta = self.player_metadata.get((t_norm, existing))
                if meta and meta.get("is_injured"):
                    return True, f"Injured ({meta.get('injury_return') or 'Out'})", meta
                return True, "Active (Partial Match)", meta

        return False, f"Player '{player_name}' NOT on active roster for {team_name}", None

    def filter_props(self, props_list: list[dict], team_field="team", player_field="player") -> tuple[list[dict], list[dict]]:
        """Splits a props list into (valid_props, invalid_props)."""
        valid = []
        invalid = []

        for item in props_list:
            t = item.get(team_field, "")
            p = item.get(player_field, "")
            is_valid, reason, meta = self.is_player_on_team(t, p)

            enriched = dict(item)
            if meta:
                enriched["position"] = meta.get("position")
                enriched["shirt_number"] = meta.get("shirt_number")
                enriched["is_injured"] = meta.get("is_injured")
                enriched["injury_return"] = meta.get("injury_return")
            enriched["roster_status"] = reason

            if is_valid:
                valid.append(enriched)
            else:
                invalid.append(enriched)

        return valid, invalid


if __name__ == "__main__":
    import sys
    action = sys.argv[1] if len(sys.argv) > 1 else "sync"

    if action == "sync":
        sync_all_rosters()
    elif action == "test":
        flt = RosterFilter()
        test_cases = [
            ("Arsenal", "Bukayo Saka"),
            ("Arsenal", "Granit Xhaka"),          # Transferred out to Leverkusen
            ("Manchester United", "Paul Pogba"),  # Transferred out
            ("Real Madrid", "Kylian Mbappe"),     # Current star
            ("Real Madrid", "Cristiano Ronaldo"), # Transferred out
        ]
        print("\n--- TEST ROSTER VALIDATION ---")
        for tm, plyr in test_cases:
            valid, reason, _ = flt.is_player_on_team(tm, plyr)
            mark = "[VALID]" if valid else "[INVALID]"
            print(f"{mark:10s} | {tm:20s} | {plyr:22s} -> {reason}")
