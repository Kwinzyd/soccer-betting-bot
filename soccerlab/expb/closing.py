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
from ..paths import DATA_DIR, ODDS_HIST_DIR, REGISTRY_DIR
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
    """Where user-downloaded current-season football-data CSVs go ({code}_{2627}.csv):
    `fd/` inside the research store (the USB store when one is configured).
    Not odds_hist/: those files are protected by the legacy baseline manifest."""
    from .db import store_dir
    return store_dir() / "fd"


MIN_VOTES, DOMINANCE = 3, 3


def build_aliases(data_dir=DATA_DIR, odds_dirs=None, out=ALIASES, current_season=2026):
    """Learn name -> id from fixtures whose (date +-1 day, exact score) is unique.

    A name is resolved only with >= MIN_VOTES agreeing fixtures and at least
    DOMINANCE x the runner-up's votes, and only if no other name claims the
    same id. Evidence, source file hashes and every unresolved name or
    unmapped current-season team are written alongside the aliases.
    """
    odds_dirs = odds_dirs or [ODDS_HIST_DIR, download_dir()]
    result, evidence, unresolved, unmapped, sources = {}, {}, {}, {}, {}
    for lg, code in FD_CODE.items():
        votes = defaultdict(Counter)
        seen_in = defaultdict(set)
        for season, folder in FD_SEASON.items():
            found = [d / f"{code}_{folder}.csv" for d in odds_dirs if (d / f"{code}_{folder}.csv").exists()]
            p_fd = found[-1] if found else None
            p_m = data_dir / f"{lg}_{season}_matches.json"
            if p_fd is None or not p_m.exists():
                continue
            sources[f"{code}_{folder}.csv"] = sha256_file(p_fd)
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
                    for side in ("Home", "Away"):
                        name = r[f"{side}Team"].strip()
                        votes[name][hits[0][f"{side.lower()}_team_id"]] += 1
                        seen_in[name].add(season)
        amap, ev, bad = {}, {}, []
        for name, c in sorted(votes.items()):
            (tid, n), *rest = c.most_common()
            runner = rest[0][1] if rest else 0
            ev[name] = {"team_id": tid, "votes": n, "runner_up_votes": runner, "seasons": sorted(seen_in[name])}
            if n >= MIN_VOTES and n >= DOMINANCE * runner:
                amap[name] = tid
            else:
                bad.append(name)
        claims = Counter(amap.values())
        for name in [k for k, v in amap.items() if claims[v] > 1]:
            bad.append(name)
            amap.pop(name)
        teams_file = data_dir / f"{lg}_{current_season}_matches.json"
        if teams_file.exists():
            ids = {t for m in json.loads(teams_file.read_text(encoding="utf-8"))
                   for t in (m["home_team_id"], m["away_team_id"])}
            unmapped[lg] = sorted(ids - set(amap.values()))
        result[lg], evidence[lg], unresolved[lg] = dict(sorted(amap.items())), ev, sorted(bad)
    atomic_write_json(out, {
        "comment": ("football-data team name -> BallDontLie team id, learned from fixtures with a unique "
                    f"(date+-1 day, exact score) match; >= {MIN_VOTES} votes and >= {DOMINANCE}x the runner-up; "
                    "one name per id. Do not hand-edit without recording evidence."),
        "aliases": result, "evidence": evidence, "unresolved_names": unresolved,
        "unmapped_current_season_team_ids": unmapped, "source_files_sha256": sources})
    return result


def match_rows(csv_path, league, schedule, aliases=None):
    """Classify every CSV row against a schedule (list of fixture dicts with
    fixture_uid, home_team_id, away_team_id, kickoff, status_state,
    home_score, away_score). Yields (row_key, fixture|None, prices|None, reason).
    reason is 'ok' or why the row is quarantined. Nothing is guessed."""
    if aliases is None:
        aliases = json.loads(ALIASES.read_text(encoding="utf-8"))["aliases"].get(league, {})
    by_pair = defaultdict(list)
    for f in schedule:
        by_pair[(f["home_team_id"], f["away_team_id"])].append(f)
    for r in _read_fd(csv_path):
        key = f"{r.get('Date', '')}|{r['HomeTeam'].strip()}|{r['AwayTeam'].strip()}"
        d = _fd_date(r.get("Date", ""))
        h, a = aliases.get(r["HomeTeam"].strip()), aliases.get(r["AwayTeam"].strip())
        if d is None:
            yield key, None, None, "unparseable_date"
            continue
        if h is None or a is None:
            yield key, None, None, "unmapped_team"
            continue
        cands = [f for f in by_pair.get((h, a), []) if abs((parse_utc(f["kickoff"]).date() - d).days) <= 1]
        if len(cands) != 1:
            yield key, None, None, "no_unique_fixture" if not cands else "ambiguous_fixture"
            continue
        f = cands[0]
        try:
            fh, fa = int(r["FTHG"]), int(r["FTAG"])
        except (KeyError, ValueError, TypeError):
            fh = fa = None
        if f.get("status_state") == "final" and fh is not None and (fh, fa) != (f["home_score"], f["away_score"]):
            yield key, f, None, "score_mismatch"            # same teams/date but different result: wrong join
            continue
        try:
            px = [check_decimal(r[c]) for c in BFE_CLOSE]
        except (KeyError, InvalidOdds):
            yield key, f, None, "missing_or_invalid_price"
            continue
        if validate_1x2(px, max_overround=0.10, min_overround=-0.03):
            yield key, f, px, "incoherent_book"
            continue
        yield key, f, px, "ok"


def import_closing(db, csv_path, league, now=None):
    """Import matched rows into closing_refs; every other row goes to
    closing_quarantine with its reason. Re-imports are idempotent."""
    now = now or utcnow()
    digest = sha256_file(csv_path)
    rep, unmapped = Counter(), Counter()
    aliases = json.loads(ALIASES.read_text(encoding="utf-8"))["aliases"].get(league, {})
    for key, f, px, reason in match_rows(csv_path, league, db.schedule(league), aliases):
        if reason == "unmapped_team":
            _, hn, an = key.split("|")
            unmapped.update(n for n in (hn, an) if n not in aliases)
        with db.tx() as c:
            if reason == "ok":
                cur = c.execute("""INSERT INTO closing_refs (fixture_uid, source, venue, market, home, draw, away,
                    source_file_sha256, imported_at, match_note) VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING""",
                                (f["fixture_uid"], "football-data.co.uk", "betfair_exchange", "1x2_ft90", *px, digest,
                                 iso(now), NOTE))
                rep["imported" if cur.rowcount else "already_present"] += 1
            else:
                c.execute("""INSERT INTO closing_quarantine (league, row_key, fixture_uid, reason, source_file_sha256,
                    recorded_at) VALUES (?,?,?,?,?,?) ON CONFLICT DO NOTHING""",
                          (league, key, f["fixture_uid"] if f else None, reason, digest, iso(now)))
                rep[reason] += 1
    return dict(rep), dict(unmapped)


__all__ = ["build_aliases", "import_closing", "timedelta"]
