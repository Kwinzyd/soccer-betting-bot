"""Per-fixture briefs for the counting markets.

For each requested fixture, print both sides' match-by-match series and the
mean/SD of each counting market, split by the venue each team will actually
occupy -- Brighton's home corner rate, not their overall rate.

SD is shown next to every mean on purpose. For a market like corners the SD is
large relative to the mean, so a two-goal difference in season averages is
usually well inside noise at this sample size.
"""

import csv
import os
import sys
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "out")
RICH_FROM = 2025

FIXTURES = [
    ("epl", "Brighton & Hove Albion", "Aston Villa"),
    ("epl", "Manchester City", "AFC Bournemouth"),
    ("epl", "Newcastle United", "Liverpool"),
    ("epl", "West Bromwich Albion", "Burnley"),
    ("laliga", "Atlético Madrid", "Villarreal"),
    ("laliga", "Elche", "Barcelona"),
]

MARKETS = [("expected_goals", "xG"), ("corners", "corners"),
           ("shots", "shots"), ("shots_on_target", "SoT"),
           ("tackles", "tackles"), ("throw_ins", "throw-ins"),
           ("offsides", "offsides"), ("yellow_cards", "yellow"),
           ("fouls", "fouls")]


def num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def mean(v):
    v = [x for x in v if x is not None]
    return sum(v) / len(v) if v else None


def sd(v):
    v = [x for x in v if x is not None]
    if len(v) < 2:
        return None
    mu = sum(v) / len(v)
    return (sum((x - mu) ** 2 for x in v) / (len(v) - 1)) ** 0.5


def fmt(m, s):
    if m is None:
        return "     n/a"
    return f"{m:6.2f}" + (f" +/-{s:5.2f}" if s is not None else "")


def load():
    path = os.path.join(OUT, "team_match_series.csv")
    if not os.path.exists(path):
        sys.exit("run build_series.py first")
    rows = []
    with open(path, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            r["season"] = int(r["season"])
            rows.append(r)
    return rows


def main():
    rows = load()
    by = defaultdict(list)
    for r in rows:
        by[(r["league"], r["team"], r["venue"])].append(r)
    known = {(r["league"], r["team"]) for r in rows}

    for lg, home, away in FIXTURES:
        print("\n" + "=" * 78)
        print(f"{home}  vs  {away}   [{lg}]")
        print("=" * 78)

        missing = [t for t in (home, away) if (lg, t) not in known]
        if missing:
            print(f"  NOT AVAILABLE -- no data for: {', '.join(missing)}")
            print("  These clubs are not in any league this key exposes")
            print("  (balldontlie has no Championship/second-tier endpoint).")
            continue

        for team, venue in ((home, "home"), (away, "away")):
            rs = [r for r in by[(lg, team, venue)] if r["season"] >= RICH_FROM]
            rs.sort(key=lambda r: r["date"])
            print(f"\n  {team}  ({venue}, {len(rs)} matches from {RICH_FROM})")
            if not rs:
                print("    no matches with counting stats yet")
                continue
            print(f"    {'market':<12} {'for':>16}  {'against':>16}")
            for key, lab in MARKETS:
                f_ = [num(r[f"{key}_for"]) for r in rs]
                a_ = [num(r[f"{key}_against"]) for r in rs]
                print(f"    {lab:<12} {fmt(mean(f_), sd(f_)):>16}  "
                      f"{fmt(mean(a_), sd(a_)):>16}")

            recent = rs[-6:]
            print(f"    last {len(recent)}: " + ", ".join(
                f"{r['date'][5:]} {r['goals_for']}-{r['goals_against']} "
                f"v {r['opponent'][:14]}" for r in recent))
            refs = [r["referee"] for r in rs if r["referee"]]
            if refs:
                print(f"    referees seen: {len(set(refs))} distinct over {len(refs)} matches")


if __name__ == "__main__":
    main()
