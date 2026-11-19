"""Deterministic event-driven walk-forward.

For each fixture and each decision policy:
  1. decision_time = kickoff - policy lead
  2. take the latest refit cutoff <= decision_time (weekly grid, 00:00 UTC)
  3. fit on data available at that cutoff only (cached per (season, cutoff))
  4. predict; record once, with the cutoff used
Settlement is not done here: outcomes are attached only for evaluation and
are never visible to a fit whose cutoff precedes result_at.
"""

from dataclasses import dataclass
from datetime import timedelta

from . import dcpit
from .timeutil import floor_day

POLICIES = {"close": timedelta(hours=1), "early": timedelta(days=4)}


@dataclass(frozen=True)
class Prediction:
    league: str
    season: int
    mid: int
    kickoff: object
    policy: str
    decision_time: object
    info_cutoff: object
    p: tuple
    over25: float
    lam_h: float
    lam_a: float
    used_xg: bool
    model_ok: bool
    promoted_home: bool
    promoted_away: bool
    n_train: int


def refit_grid(ld, season, refit_days):
    fx = [m for m in ld.matches if m.season == season]
    if not fx:
        return []
    start = floor_day(fx[0].kickoff) - timedelta(days=refit_days)
    end = fx[-1].kickoff
    grid, t = [], start
    while t <= end:
        grid.append(t)
        t += timedelta(days=refit_days)
    return grid


def walk_forward(ld, seasons, cfg: dcpit.DCConfig, policies=None, fixture_filter=None):
    policies = policies or POLICIES
    out, fits = [], {}
    for season in seasons:
        grid = refit_grid(ld, season, cfg.refit_days)
        if not grid:
            continue
        prior = dcpit.promoted_prior(ld, season, grid[0], cfg.prior_reg)
        fixtures = [m for m in ld.matches if m.season == season and m.final]
        if fixture_filter is not None:
            fixtures = [m for m in fixtures if fixture_filter(m)]
        for m in fixtures:
            for pol, lead in sorted(policies.items()):
                decision = m.kickoff - lead
                cands = [g for g in grid if g <= decision]
                if not cands:
                    continue
                cutoff = cands[-1]
                key = (season, cutoff)
                if key not in fits:
                    fits[key] = dcpit.fit_pit(ld, season, cutoff, cfg, prior=prior)
                f = fits[key]
                if f is None:
                    continue
                r = dcpit.predict(f, m.home, m.away, cfg)
                out.append(Prediction(
                    league=ld.league, season=season, mid=m.mid, kickoff=m.kickoff, policy=pol,
                    decision_time=decision, info_cutoff=cutoff, p=tuple(float(v) for v in r["p"]),
                    over25=r["over25"], lam_h=r["lam_h"], lam_a=r["lam_a"], used_xg=r["used_xg"],
                    model_ok=r["model_ok"], promoted_home=r["promoted_home"],
                    promoted_away=r["promoted_away"], n_train=f.n_train))
    return out
