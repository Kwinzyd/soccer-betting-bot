"""Builds odds_today.json dynamically from real, live odds captured in odds_logger/log/.

Extracts fixtures kicking off within the next 48-72 hours across
EPL, La Liga, Serie A, and Ligue 1, using the best available market prices
from DraftKings, FanDuel, and Polymarket.
"""

import json
import os
from datetime import datetime, timezone, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
LOG_DIR = os.path.join(ROOT, "odds_logger", "log")
SCHEDULE_DIR = os.path.join(ROOT, "balldontlie_consolidated")
OUT_FILE = os.path.join(HERE, "odds_today.json")

LEAGUES = ["epl", "laliga", "seriea", "ligue1"]
LOOKAHEAD_HOURS = 72


def build_live_card(lookahead_hours: int = LOOKAHEAD_HOURS) -> list[dict]:
    now = datetime.now(timezone.utc)
    cutoff = now + timedelta(hours=lookahead_hours)
    with open(os.path.join(ROOT, "odds_logger", "latest_poll.json"), encoding="utf-8") as f:
        poll = json.load(f)
    age = now - datetime.fromisoformat(poll["observed_at"])
    if not timedelta(0) <= age <= timedelta(minutes=30):
        raise RuntimeError("Odds poll expired. Run bot.py to refresh it.")

    card = []

    for lg in LEAGUES:
        m_file = os.path.join(SCHEDULE_DIR, f"{lg}_2026_matches.json")
        t_file = os.path.join(SCHEDULE_DIR, f"{lg}_2026_teams.json")
        rows = poll["leagues"].get(lg, [])
        if not rows:
            continue

        with open(t_file, "r", encoding="utf-8") as f:
            teams = {t["id"]: t["name"] for t in json.load(f)}

        with open(m_file, "r", encoding="utf-8") as f:
            matches = {m["id"]: m for m in json.load(f)}

        # match_id -> vendor -> latest quote row
        latest_quotes = {}
        for row in rows:
            vendor = row.get("vendor")
            if vendor:
                latest_quotes.setdefault(row["match_id"], {})[vendor] = {
                    side: row.get(f"moneyline_{side}_odds") for side in ("home", "draw", "away")
                }

        for mid, vendor_dict in latest_quotes.items():
            m = matches.get(mid)
            if not m:
                continue

            try:
                kickoff = datetime.fromisoformat(m["date"].replace("Z", "+00:00"))
            except Exception:
                continue

            # Only pre-match fixtures kicking off in the upcoming window
            if now <= kickoff <= cutoff:
                home_team = teams.get(m.get("home_team_id"))
                away_team = teams.get(m.get("away_team_id"))
                if not (home_team and away_team):
                    continue

                books = {
                    book: prices for book, prices in vendor_dict.items()
                    if all(isinstance(v, (int, float)) and 100 <= abs(v) < float("inf")
                           for v in prices.values())
                }
                if not books or m.get("status_state") != "scheduled":
                    continue
                sides = ("home", "draw", "away")
                best = {s: max(books, key=lambda book: books[book][s]) for s in sides}

                card.append({
                    "league": lg,
                    "match_id": mid,
                    "home": home_team,
                    "away": away_team,
                    "kickoff": m["date"],
                    "vendors": list(books),
                    "books": books,
                    "best_book": best,
                    "odds": {s: books[best[s]][s] for s in sides},
                })

    # Sort chronologically by kickoff
    card.sort(key=lambda x: x["kickoff"])

    with open(OUT_FILE, "w", encoding="utf-8") as f:
        json.dump(card, f, indent=2, ensure_ascii=False)

    print(f"Generated {OUT_FILE} with {len(card)} live upcoming matches across {len(LEAGUES)} leagues.")
    return card


if __name__ == "__main__":
    card = build_live_card()
    for g in card:
        ko = g["kickoff"][:16].replace("T", " ")
        print(f"  [{g['league'].upper()}] {ko} UTC | {g['home']} vs {g['away']} -> Best: H {g['odds']['home']} / D {g['odds']['draw']} / A {g['odds']['away']}")
