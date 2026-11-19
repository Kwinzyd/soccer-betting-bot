"""Per-team, per-match series for the counting markets, plus home/away splits.

Field availability is the binding constraint here and is not uniform:

    shots, shots_on_target, fouls, yellow/red cards, possession, passes
        -> seasons 2022 onward
    expected_goals, corners, tackles, throw_ins, offsides, duels, crosses,
    interceptions, clearances, big_chances
        -> season 2025 onward ONLY. Older seasons return the rows with these
           fields present but null, which is easy to mistake for "no data" or,
           worse, to average as zero.

So anything built on xG or corners rests on ~38 matches per team (19 home,
19 away) plus whatever the new season has produced. That is enough for a mean
and a rough variance; it is not enough to trust a team-specific corner edge.

  python build_series.py --refresh    re-pull 2026 first (early-season games)
  python build_series.py              build from local data
"""

import json
import os
import sys
import time
import urllib.request
import urllib.error
import csv
from collections import defaultdict

from pathlib import Path

KEY = (os.environ.get("BALLDONTLIE_KEY") or
       (Path(os.environ.get("LOCALAPPDATA", Path.home())) / "soccer-betting-bot" / "bdl.key").read_text().strip())

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CONS = os.path.join(ROOT, "balldontlie_consolidated")
OUT = os.path.join(HERE, "out")
os.makedirs(OUT, exist_ok=True)

VERSION = {"laliga": "v1", "seriea": "v1", "ligue1": "v1", "epl": "v2"}
SEASONS = [2022, 2023, 2024, 2025, 2026]
RICH_FROM = 2025          # season at which xG/corners/counting stats begin
SLEEP = 13.0

# fields carried through to the per-match series
RICH = ["expected_goals", "corners", "tackles", "throw_ins", "offsides",
        "big_chances", "big_chances_missed", "shots_inside_box",
        "shots_outside_box", "shots_blocked", "interceptions", "clearances",
        "crosses_total", "ground_duels_won", "aerial_duels_won", "saves"]
BASIC = ["shots", "shots_on_target", "fouls", "yellow_cards", "red_cards",
         "possession_pct", "passes"]

FIXTURES = [
    ("epl", "Brighton & Hove Albion", "Aston Villa"),
    ("epl", "Manchester City", "AFC Bournemouth"),
    ("epl", "Newcastle United", "Liverpool"),
    ("epl", "West Bromwich Albion", "Burnley"),      # expected to be missing
    ("laliga", "Atlético Madrid", "Villarreal"),
    ("laliga", "Elche", "Barcelona"),
]


# ------------------------------------------------------------------ fetching

def fetch(url):
    req = urllib.request.Request(url, headers={"Authorization": KEY})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 429:
                print("  429, waiting 25s")
                time.sleep(25)
                continue
            print(f"  HTTP {e.code}")
            return None
        except Exception as e:                                   # noqa: BLE001
            print(f"  {type(e).__name__}: {e}")
            time.sleep(5)
    return None


def refresh_2026(leagues):
    """Re-pull the current season's matches and stats for finished games."""
    for lg in leagues:
        v = VERSION[lg]
        cursor, page, rows = None, 1, []
        while True:
            url = (f"https://api.balldontlie.io/{lg}/{v}/matches"
                   f"?season=2026&per_page=100")
            if cursor:
                url += f"&cursor={cursor}"
            d = fetch(url)
            time.sleep(SLEEP)
            if not d or "data" not in d:
                break
            rows.extend(d["data"])
            nxt = (d.get("meta") or {}).get("next_cursor")
            if not nxt or nxt == cursor or len(d["data"]) < 100:
                break
            cursor, page = nxt, page + 1
        if rows:
            with open(os.path.join(CONS, f"{lg}_2026_matches.json"), "w",
                      encoding="utf-8") as f:
                json.dump(rows, f, ensure_ascii=False)
            done = [m["id"] for m in rows if m.get("status_state") == "final"
                    and m.get("home_score") is not None]
            print(f"  {lg}: {len(rows)} fixtures, {len(done)} finished")

            stats = []
            for i in range(0, len(done), 50):
                q = "&".join(f"match_ids[]={m}" for m in done[i:i + 50])
                d = fetch(f"https://api.balldontlie.io/{lg}/{v}"
                          f"/team_match_stats?{q}&per_page=100")
                time.sleep(SLEEP)
                if d and "data" in d:
                    stats.extend(d["data"])
            if stats:
                with open(os.path.join(CONS, f"{lg}_2026_team_match_stats.json"),
                          "w", encoding="utf-8") as f:
                    json.dump(stats, f)
                print(f"  {lg}: {len(stats)} stat rows for 2026")


# ------------------------------------------------------------------- loading

def load(lg):
    teams, matches, stats = {}, [], {}
    for s in SEASONS:
        p = os.path.join(CONS, f"{lg}_{s}_teams.json")
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                for t in json.load(f):
                    teams[t["id"]] = t["name"]
        p = os.path.join(CONS, f"{lg}_{s}_matches.json")
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                for m in json.load(f):
                    if (m.get("status_state") == "final"
                            and m.get("home_score") is not None):
                        m["_season"] = s
                        matches.append(m)
        p = os.path.join(CONS, f"{lg}_{s}_team_match_stats.json")
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                for r in json.load(f):
                    stats[(r["match_id"], r["team_id"])] = r
    matches.sort(key=lambda m: m["date"])
    return teams, matches, stats


def series_rows(lg, teams, matches, stats):
    """One row per (match, team) with for/against on every counting field."""
    out = []
    for m in matches:
        for side in ("home", "away"):
            tid = m[f"{side}_team_id"]
            oid = m["away_team_id"] if side == "home" else m["home_team_id"]
            gf = m["home_score"] if side == "home" else m["away_score"]
            ga = m["away_score"] if side == "home" else m["home_score"]
            s = stats.get((m["id"], tid), {})
            o = stats.get((m["id"], oid), {})
            row = {
                "league": lg, "season": m["_season"], "match_id": m["id"],
                # str() because a club that has since left the league may not
                # appear in any teams file, leaving the raw int id as fallback;
                # mixing int and str keys blows up every downstream sort.
                "date": m["date"][:10], "team": str(teams.get(tid, tid)),
                "opponent": str(teams.get(oid, oid)), "venue": side,
                "referee": m.get("referee_name") or "",
                "round": m.get("round_number") or "",
                "goals_for": gf, "goals_against": ga,
            }
            for k in BASIC + RICH:
                row[f"{k}_for"] = s.get(k)
                row[f"{k}_against"] = o.get(k)
            out.append(row)
    return out


def write_csv(path, rows):
    if not rows:
        return
    cols = list(rows[0].keys())
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)
    print(f"  wrote {os.path.basename(path)}  ({len(rows)} rows)")


def mean(vals):
    v = [x for x in vals if x is not None]
    return sum(v) / len(v) if v else None


def stdev(vals):
    v = [x for x in vals if x is not None]
    if len(v) < 2:
        return None
    mu = sum(v) / len(v)
    return (sum((x - mu) ** 2 for x in v) / (len(v) - 1)) ** 0.5


def splits(rows):
    """Per team, per venue: goals and xG for/against, plus counting means."""
    acc = defaultdict(list)
    for r in rows:
        acc[(r["league"], r["team"], r["venue"])].append(r)
    out = []
    for (lg, team, venue), rs in sorted(acc.items()):
        rich = [r for r in rs if r["season"] >= RICH_FROM]
        rec = {
            "league": lg, "team": team, "venue": venue,
            "matches_all": len(rs),
            "goals_for_pg": round(mean([r["goals_for"] for r in rs]), 3),
            "goals_against_pg": round(mean([r["goals_against"] for r in rs]), 3),
            "matches_rich": len(rich),
        }
        for k, lab in (("expected_goals", "xg"), ("corners", "corners"),
                       ("yellow_cards", "yc"), ("shots", "shots"),
                       ("tackles", "tackles"), ("throw_ins", "throw_ins"),
                       ("offsides", "offsides")):
            for d in ("for", "against"):
                m_ = mean([r[f"{k}_{d}"] for r in rich])
                s_ = stdev([r[f"{k}_{d}"] for r in rich])
                rec[f"{lab}_{d}_pg"] = round(m_, 3) if m_ is not None else ""
                rec[f"{lab}_{d}_sd"] = round(s_, 3) if s_ is not None else ""
        out.append(rec)
    return out


def referee_table(rows):
    acc = defaultdict(list)
    for r in rows:
        if r["referee"] and r["season"] >= RICH_FROM:
            acc[(r["league"], r["referee"])].append(r)
    out = []
    for (lg, ref), rs in sorted(acc.items()):
        # each match appears twice (once per team), so /2 for match count
        out.append({
            "league": lg, "referee": ref, "matches": len(rs) // 2,
            "yc_per_team_game": round(mean([r["yellow_cards_for"] for r in rs]) or 0, 3),
            "rc_per_team_game": round(mean([r["red_cards_for"] for r in rs]) or 0, 3),
            "fouls_per_team_game": round(mean([r["fouls_for"] for r in rs]) or 0, 3),
        })
    out.sort(key=lambda r: -r["matches"])
    return out


if __name__ == "__main__":
    if "--refresh" in sys.argv:
        print("refreshing 2026 ...")
        refresh_2026(["epl", "laliga"])

    all_rows = []
    for lg in ["epl", "laliga", "seriea", "ligue1"]:
        teams, matches, stats = load(lg)
        rows = series_rows(lg, teams, matches, stats)
        all_rows += rows
        print(f"{lg:8s} {len(matches):5d} matches -> {len(rows)} team-match rows")

    print()
    write_csv(os.path.join(OUT, "team_match_series.csv"), all_rows)
    write_csv(os.path.join(OUT, "home_away_splits.csv"), splits(all_rows))
    write_csv(os.path.join(OUT, "referee_effects.csv"), referee_table(all_rows))
