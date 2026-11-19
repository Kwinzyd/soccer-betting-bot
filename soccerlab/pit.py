"""Point-in-time match data.

Every observation carries the instant it became *available*, not just the
instant it happened:

* result_at  = kickoff + RESULT_LAG. The feed has no settlement timestamp, so
               this is a conservative assumption (90' + half-time + stoppage
               + reporting delay < 3h for league matches).
* xg_at      = when an xG value may be used. Two modes, because the source
               has no publication timestamps:
                 'provider_lag'  kickoff + XG_PROVIDER_LAG (what a live
                                 system polling the provider could have had)
                 'ingestion'     XG_INGESTED_AT, the moment this repository
                                 actually pulled the stats (2026-08-22). Strict.
               Revised values cannot be detected: the feed keeps no history.

A view at cutoff C contains only final matches with result_at <= C, and xG
only where xg_at <= C. Scheduled fixtures carry home_score = away_score = 0
in this feed; they are never treated as results.

League membership for season S is the set of teams on S's fixture list,
which is published before the season starts and therefore known at any
decision time inside S. Teams first appearing in later seasons are invisible.
"""

import glob
import json
import os
import re
from dataclasses import dataclass, field
from datetime import timedelta

from .hashing import sha256_file
from .timeutil import parse_utc

RESULT_LAG = timedelta(hours=3)
XG_PROVIDER_LAG = timedelta(hours=24)
# balldontlie_team_match_stats_progress.json mtime: 2026-08-22 00:44 EDT.
XG_INGESTED_AT = parse_utc("2026-08-22T04:44:00Z")


class DataError(ValueError):
    pass


@dataclass(frozen=True)
class Match:
    league: str
    season: int
    mid: int
    home: int
    away: int
    kickoff: object          # aware UTC datetime
    status: str
    hg: object = None        # int or None (None unless final)
    ag: object = None
    xgh: object = None
    xga: object = None
    result_at: object = None
    xg_at: object = None
    round_number: object = None

    @property
    def uid(self) -> str:
        return f"bdl:{self.league}:{self.mid}"

    @property
    def final(self) -> bool:
        return self.result_at is not None

    @property
    def outcome(self) -> int:
        if not self.final:
            raise DataError(f"{self.uid} has no result")
        return 0 if self.hg > self.ag else (1 if self.hg == self.ag else 2)


@dataclass
class LeagueData:
    league: str
    matches: list                     # all fixtures, any status, sorted by (kickoff, mid)
    names: dict = field(default_factory=dict)
    fingerprint: dict = field(default_factory=dict)

    def __post_init__(self):
        self.matches = sorted(self.matches, key=lambda m: (m.kickoff, m.mid))
        seen = {}
        for m in self.matches:
            if m.mid in seen:
                raise DataError(f"duplicate match id {m.uid}")
            seen[m.mid] = m
        self._by_id = seen
        self._participants = {}
        for m in self.matches:
            self._participants.setdefault(m.season, set()).update((m.home, m.away))

    @property
    def seasons(self):
        return sorted(self._participants)

    def get(self, mid):
        return self._by_id[mid]

    def participants(self, season) -> frozenset:
        return frozenset(self._participants.get(season, ()))

    def teams_before_season(self, season) -> frozenset:
        out = set()
        for s, teams in self._participants.items():
            if s < season:
                out |= teams
        return frozenset(out)

    def available(self, cutoff) -> list:
        """Final matches whose result was available at `cutoff`."""
        return [m for m in self.matches if m.result_at is not None and m.result_at <= cutoff]

    def xg_available(self, cutoff) -> list:
        return [m for m in self.available(cutoff)
                if m.xgh is not None and m.xga is not None and m.xg_at is not None and m.xg_at <= cutoff]

    def replace(self, matches):
        return LeagueData(self.league, list(matches), dict(self.names), dict(self.fingerprint))


def load_league(league, data_dir, xg_mode="provider_lag", result_lag=RESULT_LAG,
                xg_lag=XG_PROVIDER_LAG, xg_ingested_at=XG_INGESTED_AT) -> LeagueData:
    if xg_mode not in ("provider_lag", "ingestion", "none"):
        raise ValueError(f"unknown xg_mode {xg_mode!r}")
    data_dir = str(data_dir)
    fingerprint, names = {}, {}

    for path in sorted(glob.glob(os.path.join(data_dir, f"{league}_*_teams.json"))):
        fingerprint[os.path.basename(path)] = sha256_file(path)
        with open(path, encoding="utf-8") as f:
            for t in json.load(f):
                names[t["id"]] = t["name"]

    xg = {}
    if xg_mode != "none":
        for path in sorted(glob.glob(os.path.join(data_dir, f"{league}_*_team_match_stats.json"))):
            fingerprint[os.path.basename(path)] = sha256_file(path)
            with open(path, encoding="utf-8") as f:
                for r in json.load(f):
                    if r.get("expected_goals") is not None:
                        xg.setdefault(r["match_id"], {})[r["team_id"]] = float(r["expected_goals"])

    matches = []
    pat = re.compile(rf"^{re.escape(league)}_(\d{{4}})_matches\.json$")
    for path in sorted(glob.glob(os.path.join(data_dir, f"{league}_*_matches.json"))):
        mt = pat.match(os.path.basename(path))
        if not mt:
            continue
        season = int(mt.group(1))
        fingerprint[os.path.basename(path)] = sha256_file(path)
        with open(path, encoding="utf-8") as f:
            for r in json.load(f):
                matches.append(_to_match(league, season, r, xg, xg_mode, result_lag, xg_lag, xg_ingested_at))
    if not matches:
        raise DataError(f"no match files for {league} in {data_dir}")
    return LeagueData(league, matches, names, fingerprint)


def _to_match(league, season, r, xg, xg_mode, result_lag, xg_lag, xg_ingested_at):
    if r.get("season") not in (None, season):
        raise DataError(f"{league} match {r.get('id')} season {r.get('season')} in {season} file")
    kickoff = parse_utc(r["date"])
    status = r.get("status_state") or "unknown"
    hg = ag = xgh = xga = result_at = xg_at = None
    if status == "final":
        if r.get("home_score") is None or r.get("away_score") is None:
            raise DataError(f"{league} match {r['id']} final without score")
        hg, ag = int(r["home_score"]), int(r["away_score"])
        if hg < 0 or ag < 0:
            raise DataError(f"{league} match {r['id']} negative score")
        result_at = kickoff + result_lag
        pair = xg.get(r["id"])
        h, a = r["home_team_id"], r["away_team_id"]
        if pair and h in pair and a in pair:
            xgh, xga = pair[h], pair[a]
            xg_at = kickoff + xg_lag if xg_mode == "provider_lag" else max(xg_ingested_at, result_at)
    return Match(league=league, season=season, mid=int(r["id"]), home=int(r["home_team_id"]),
                 away=int(r["away_team_id"]), kickoff=kickoff, status=status, hg=hg, ag=ag,
                 xgh=xgh, xga=xga, result_at=result_at, xg_at=xg_at,
                 round_number=r.get("round_number"))
