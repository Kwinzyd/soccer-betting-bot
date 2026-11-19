"""External closing references for EXP-2026-11-B.

Source: football-data.co.uk season CSVs (downloaded by the user, e.g.
https://www.football-data.co.uk/mmz4281/2627/E0.csv), Betfair Exchange closing
columns BFECH/BFECD/BFECA. Pinnacle columns (PSC*) stop in January 2026 after
Pinnacle closed its public API (2025-07-23), so they cannot serve 2026-27.

Caveats recorded with every row (`match_note`): the capture time of
football-data's "closing" price is not published, and whether BFEC is a back,
lay or mid price is not documented. This makes it an *independent but
untimed* closing reference.

Matching: CSV team names -> BallDontLie team ids via an alias table learned
from historical fixtures where (date +-1 day, exact score) is unique. A CSV
row is imported only if both names map and exactly one logged fixture has
those team ids within +-1 day. Everything else is reported, never guessed.

    python -m soccerlab expb aliases                     (re)build alias table
    python -m soccerlab expb closing-import E0.csv epl
"""

import csv
import json
from collections import Counter, defaultdict
from datetime import datetime, timedelta

from ..hashing import atomic_write_json, sha256_file
from ..odds import InvalidOdds, check_decimal, validate_1x2
from ..paths import DATA_DIR, ODDS_HIST_DIR, REGISTRY_DIR, var_dir
from ..timeutil import iso, parse_utc, utcnow

ALIASES = REGISTRY_DIR / "experiments" / "team_aliases.json"
FD_CODE = {"epl": "E0", "laliga": "SP1", "seriea": "I1", "ligue1": "F1"}
FD_SEASON = {2022: "2223", 2023: "2324", 2024: "2425", 2025: "2526", 2026: "2627"}
BFE_CLOSE = ("BFECH", "BFECD", "BFECA")
NOTE = "football-data BFEC*: closing capture time unpublished; back/lay/mid unspecified"


def _fd_date(s):
    for fmt in ("%d/%m/%Y", "%d/%m/%y"):
        try:
            return datetime.strptime(s.strip(), fmt).date()
        except ValueError:
            continue
    return None


def _read_fd(path):
    with open(path, encoding="utf-8-sig", errors="replace") as f:
        return [r for r in csv.DictReader(f) if r.get("HomeTeam")]


def download_dir():
    """Where user-downloaded current-season football-data CSVs go ({code}_{2627}.csv).
    Not odds_hist/: those files are protected by the legacy baseline manifest."""
    return var_dir() / "expb" / "fd"


def build_aliases(data_dir=DATA_DIR, odds_dirs=None, out=ALIASES):
    odds_dirs = odds_dirs or [ODDS_HIST_DIR, download_dir()]
    result = {}
    for lg, code in FD_CODE.items():
        votes = defaultdict(Counter)
        for season, folder in FD_SEASON.items():
            found = [d / f"{code}_{folder}.csv" for d in odds_dirs if (d / f"{code}_{folder}.csv").exists()]
            p_fd = found[-1] if found else None
            p_m = data_dir / f"{lg}_{season}_matches.json"
            if p_fd is None or not p_m.exists():
                continue
            bdl = [m for m in json.loads(p_m.read_text(encoding="utf-8")) if m.get("status_state") == "final"]
            for r in _read_fd(p_fd):
                d = _fd_date(r.get("Date", ""))
                try:
                    hg, ag = int(r["FTHG"]), int(r["FTAG"])
                except (KeyError, ValueError, TypeError):
                    continue
                hits = [m for m in bdl if d and abs((parse_utc(m["date"]).date() - d).days) <= 1
                        and m["home_score"] == hg and m["away_score"] == ag]
                if len(hits) == 1:
                    votes[r["HomeTeam"].strip()][hits[0]["home_team_id"]] += 1
                    votes[r["AwayTeam"].strip()][hits[0]["away_team_id"]] += 1
        amap = {}
        for name, c in votes.items():
            (tid, n), *rest = c.most_common()
            if n >= 3 and (not rest or n >= 3 * rest[0][1]):
                amap[name] = tid
        result[lg] = dict(sorted(amap.items()))
    atomic_write_json(out, {"comment": "football-data team name -> BallDontLie team id, learned from unique "
                                       "(date+-1, score) matches; edit by hand only with evidence",
                            "aliases": result})
    return result


def import_closing(db, csv_path, league, now=None):
    now = now or utcnow()
    aliases = json.loads(ALIASES.read_text(encoding="utf-8"))["aliases"].get(league, {})
    sched = db.schedule(league)
    by_pair = defaultdict(list)
    for f in sched:
        by_pair[(f["home_team_id"], f["away_team_id"])].append(f)
    digest = sha256_file(csv_path)
    rep = Counter()
    unmapped = Counter()
    for r in _read_fd(csv_path):
        d = _fd_date(r.get("Date", ""))
        h, a = aliases.get(r["HomeTeam"].strip()), aliases.get(r["AwayTeam"].strip())
        if h is None or a is None:
            rep["unmapped_team"] += 1
            unmapped.update(n for n, t in ((r["HomeTeam"].strip(), h), (r["AwayTeam"].strip(), a)) if t is None)
            continue
        cands = [f for f in by_pair.get((h, a), []) if d and abs((parse_utc(f["kickoff"]).date() - d).days) <= 1]
        if len(cands) != 1:
            rep["no_unique_fixture" if not cands else "ambiguous_fixture"] += 1
            continue
        try:
            px = [check_decimal(r[c]) for c in BFE_CLOSE]
        except (KeyError, InvalidOdds):
            rep["missing_or_invalid_price"] += 1
            continue
        if validate_1x2(px, max_overround=0.10, min_overround=-0.03):
            rep["incoherent_book"] += 1
            continue
        f = cands[0]
        with db.tx() as c:
            cur = c.execute("""INSERT INTO closing_refs (fixture_uid, source, venue, market, home, draw, away,
                source_file_sha256, imported_at, match_note) VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING""",
                            (f["fixture_uid"], "football-data.co.uk", "betfair_exchange", "1x2_ft90", *px, digest,
                             iso(now), NOTE))
        rep["imported" if cur.rowcount else "already_present"] += 1
    return dict(rep), dict(unmapped)


__all__ = ["build_aliases", "import_closing", "timedelta"]
