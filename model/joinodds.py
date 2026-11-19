"""Join football-data.co.uk closing odds onto the balldontlie match records.

The two sources name teams differently ("Man United" vs "Manchester United",
"Ath Bilbao" vs "Athletic Club", "Espanol" vs "Espanyol"), so the mapping is
learned rather than hand-written:

  pass 1  match on (date +/- 1 day, exact scoreline). Where that is unique it
          is almost certainly the same fixture, which yields name pairs.
  pass 2  keep the most frequent balldontlie name for each football-data name,
          then re-join on (date +/- 1 day, mapped names) to pick up fixtures
          pass 1 could not disambiguate.

Writes odds_joined.json: {league: {match_id: {...odds...}}}
"""

import csv
import json
import os
from collections import Counter, defaultdict
import numpy as np
import dcmodel as M
import backtest as B

HERE = os.path.dirname(os.path.abspath(__file__))
ODDS_DIR = os.path.join(os.path.dirname(HERE), "odds_hist")

# football-data league code -> our league key
LG = {"E0": "epl", "SP1": "laliga", "I1": "seriea", "F1": "ligue1"}
# football-data season folder -> our season label (2223 == our season 2022)
SEASON = {"2223": 2022, "2324": 2023, "2425": 2024, "2526": 2025, "2627": 2026}

# closing-odds columns, best first: Pinnacle closing, then market-average
# closing, then Bet365 closing.
ODDS_SETS = [
    # closing
    ("PSCH", "PSCD", "PSCA", "pinnacle_close"),
    ("AvgCH", "AvgCD", "AvgCA", "avg_close"),
    ("B365CH", "B365CD", "B365CA", "b365_close"),
    ("MaxCH", "MaxCD", "MaxCA", "max_close"),
    # opening / early (taken days before kickoff)
    ("PSH", "PSD", "PSA", "pinnacle_open"),
    ("AvgH", "AvgD", "AvgA", "avg_open"),
    ("MaxH", "MaxD", "MaxA", "max_open"),
]
OU_SETS = [
    ("P>2.5", "P<2.5", "pinnacle_ou"),
    ("Avg>2.5", "Avg<2.5", "avg_ou"),
]


def parse_date(s):
    s = s.strip()
    for fmt in ("%d/%m/%Y", "%d/%m/%y"):
        try:
            import datetime
            return np.datetime64(datetime.datetime.strptime(s, fmt).date())
        except ValueError:
            continue
    return None


def load_fd():
    """league -> season -> list of row dicts"""
    out = defaultdict(lambda: defaultdict(list))
    for fn in sorted(os.listdir(ODDS_DIR)):
        if not fn.endswith(".csv"):
            continue
        code, yr = fn[:-4].split("_")
        if code not in LG or yr not in SEASON:
            continue
        with open(os.path.join(ODDS_DIR, fn), encoding="utf-8-sig",
                  errors="replace") as f:
            for r in csv.DictReader(f):
                d = parse_date(r.get("Date", ""))
                if d is None or not r.get("HomeTeam"):
                    continue
                try:
                    hg, ag = int(r["FTHG"]), int(r["FTAG"])
                except (ValueError, KeyError, TypeError):
                    continue
                rec = {"date": d, "home": r["HomeTeam"].strip(),
                       "away": r["AwayTeam"].strip(), "hg": hg, "ag": ag,
                       "raw": r}
                out[LG[code]][SEASON[yr]].append(rec)
    return out


def get_odds(raw):
    o = {}
    for h, d, a, tag in ODDS_SETS:
        try:
            vh, vd, va = float(raw[h]), float(raw[d]), float(raw[a])
            if vh > 1 and vd > 1 and va > 1:
                o[tag] = [vh, vd, va]
        except (KeyError, ValueError, TypeError):
            pass
    for ov, un, tag in OU_SETS:
        try:
            a, b = float(raw[ov]), float(raw[un])
            if a > 1 and b > 1:
                o[tag] = [a, b]
        except (KeyError, ValueError, TypeError):
            pass
    return o


def join_league(league, fd_by_season, bdl_matches, bdl_names):
    by_season = defaultdict(list)
    for m in bdl_matches:
        by_season[m["season"]].append(m)

    pairs_h, pairs_a = Counter(), Counter()
    confident = []

    # ---- pass 1: unique (date +/-1, scoreline)
    for season, rows in fd_by_season.items():
        cand = by_season.get(season, [])
        for r in rows:
            hits = [m for m in cand
                    if abs((m["date"] - r["date"]).astype(int)) <= 1
                    and m["hg"] == r["hg"] and m["ag"] == r["ag"]]
            if len(hits) == 1:
                m = hits[0]
                confident.append((r, m))
                pairs_h[(r["home"], bdl_names.get(m["home"], ""))] += 1
                pairs_a[(r["away"], bdl_names.get(m["away"], ""))] += 1

    # most frequent bdl name for each fd name (home and away evidence pooled)
    votes = defaultdict(Counter)
    for (fd, bd), c in list(pairs_h.items()) + list(pairs_a.items()):
        if bd:
            votes[fd][bd] += c
    name_map = {fd: cnt.most_common(1)[0][0] for fd, cnt in votes.items()}

    # ---- pass 2: join on mapped names
    joined = {}
    used = set()
    for season, rows in fd_by_season.items():
        cand = by_season.get(season, [])
        for r in rows:
            hn, an = name_map.get(r["home"]), name_map.get(r["away"])
            if hn is None or an is None:
                continue
            hits = [m for m in cand
                    if abs((m["date"] - r["date"]).astype(int)) <= 1
                    and bdl_names.get(m["home"]) == hn
                    and bdl_names.get(m["away"]) == an
                    and m["id"] not in used]
            if len(hits) == 1:
                m = hits[0]
                used.add(m["id"])
                o = get_odds(r["raw"])
                if o:
                    o["season"] = season
                    joined[m["id"]] = o
    return joined, name_map


def main():
    fd = load_fd()
    allout = {}
    print("league    fd_rows  bdl_matches  joined  with_pinnacle")
    for league in M.LEAGUES:
        matches, names, index, n_teams, first_seen = B.prep(league)
        joined, name_map = join_league(league, fd[league], matches, names)
        npin = sum(1 for v in joined.values() if "pinnacle_close" in v)
        nfd = sum(len(v) for v in fd[league].values())
        print(f"{league:9s} {nfd:7d} {len(matches):12d} {len(joined):7d} "
              f"{npin:14d}")
        allout[league] = {str(k): v for k, v in joined.items()}

    with open(os.path.join(HERE, "odds_joined.json"), "w", encoding="utf-8") as f:
        json.dump(allout, f)
    print("\nwrote odds_joined.json")


if __name__ == "__main__":
    main()
