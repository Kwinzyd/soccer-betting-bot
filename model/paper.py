"""Track the first qualifying price per match and selection at one paper unit."""
from contextlib import closing
import json
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "odds_logger" / "paper.sqlite3"


def record(picks, season=2026):
    with closing(sqlite3.connect(DB)) as db, db:
        db.execute("""CREATE TABLE IF NOT EXISTS picks (
            league TEXT, match_id INTEGER, side TEXT, book TEXT, odds REAL,
            probability REAL, kickoff TEXT, recorded TEXT DEFAULT CURRENT_TIMESTAMP,
            pnl REAL, PRIMARY KEY (league, match_id, side))""")
        db.executemany("""INSERT OR IGNORE INTO picks
            (league, match_id, side, book, odds, probability, kickoff)
            VALUES (?, ?, ?, ?, ?, ?, ?)""", picks)
        for league, in db.execute("SELECT DISTINCT league FROM picks WHERE pnl IS NULL").fetchall():
            path = ROOT / "balldontlie_consolidated" / f"{league}_{season}_matches.json"
            for match in json.loads(path.read_text(encoding="utf-8")):
                h, a = match.get("home_score"), match.get("away_score")
                if match.get("status_state") != "final" or h is None or a is None:
                    continue
                winner = "home" if h > a else "away" if a > h else "draw"
                db.execute("""UPDATE picks SET pnl = CASE WHEN side != ? THEN -1
                    WHEN odds > 0 THEN odds / 100.0 ELSE -100.0 / odds END
                    WHERE league = ? AND match_id = ? AND pnl IS NULL""",
                           (winner, league, match["id"]))
        total, settled, pnl = db.execute("SELECT count(*), count(pnl), coalesce(sum(pnl),0) FROM picks").fetchone()
    print(f"Paper record: {settled} settled, {total-settled} open, {pnl:+.2f} units")
