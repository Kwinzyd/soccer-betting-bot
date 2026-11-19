"""Point-in-time Dixon-Coles ("corrected DC").

The likelihood, gradient, optimiser and score matrix are the legacy ones
(model/dcmodel.py, gradient-checked). What changes is *what the fit may see*:

1. Training data: final matches with result_at <= cutoff only.
2. Team universe: teams in the training data plus the target season's
   fixture list. Teams from future seasons do not exist.
3. Promoted flag: on the target season's fixture list and absent from every
   earlier season -- decided by the fixture's season, not by the latest season
   present in the training data (the legacy season-boundary bug).
4. Promoted-team prior: estimated only from cohorts in seasons strictly
   before the target season, using only results available at the season's
   first cutoff. With no earlier cohort the prior is 0 (league average).
5. Decay ages are measured from the cutoff instant in fractional days.
"""

from dataclasses import asdict, dataclass, field

import numpy as np

from . import legacy
from .timeutil import days_between


@dataclass(frozen=True)
class DCConfig:
    xi: float = 0.0015
    reg: float = 2.0
    min_matches: int = 60
    xg_alpha: float = 0.0
    min_matches_xg: int = 80
    prior_reg: float = 1.0
    refit_days: int = 7
    max_goals: int = 12
    use_dc: bool = True

    def to_dict(self):
        return asdict(self)


@dataclass
class Fit:
    league: str
    season: int
    cutoff: object
    index: dict
    atk: np.ndarray
    dfn: np.ndarray
    home_adv: float
    mu: float
    rho: float
    converged: bool
    n_train: int
    promoted: frozenset
    prior: tuple
    xg: dict = field(default=None)    # {"atk","dfn","home_adv","mu","converged","n_train"} or None

    def params_json(self):
        inv = {i: t for t, i in self.index.items()}
        return {"home_adv": self.home_adv, "mu": self.mu, "rho": self.rho, "converged": self.converged,
                "n_train": self.n_train, "prior": list(self.prior),
                "atk": {str(inv[i]): float(v) for i, v in enumerate(self.atk)},
                "def": {str(inv[i]): float(v) for i, v in enumerate(self.dfn)}}


def _M():
    return legacy.modules()["dcmodel"]


def _design(matches, index, response):
    hi = np.array([index[m.home] for m in matches], dtype=int)
    ai = np.array([index[m.away] for m in matches], dtype=int)
    if response == "goals":
        yh = np.array([m.hg for m in matches], dtype=float)
        ya = np.array([m.ag for m in matches], dtype=float)
    else:
        yh = np.array([m.xgh for m in matches], dtype=float)
        ya = np.array([m.xga for m in matches], dtype=float)
    return hi, ai, yh, ya


def promoted_prior(ld, season, cutoff, prior_reg=1.0):
    """(atk_offset, def_offset, n_cohorts, n_promoted) from cohorts before `season`."""
    M = _M()
    first = min(ld.seasons)
    avail = ld.available(cutoff)
    atk_off, def_off, cohorts = [], [], 0
    for c in ld.seasons:
        if c >= season or c <= first:
            continue
        sub = [m for m in avail if m.season == c]
        if len(sub) < 100:
            continue
        before = ld.teams_before_season(c)
        teams = sorted({t for m in sub for t in (m.home, m.away)})
        promoted = [t for t in teams if t not in before]
        if not promoted:
            continue
        index = {t: i for i, t in enumerate(teams)}
        hi, ai, yh, ya = _design(sub, index, "goals")
        z = np.zeros(len(teams))
        f = M.fit(len(teams), hi, ai, yh, ya, np.ones(len(hi)), z, z, reg=prior_reg, use_dc=False)
        cohorts += 1
        for t in promoted:
            atk_off.append(f["atk"][index[t]])
            def_off.append(f["def"][index[t]])
    if not atk_off:
        return (0.0, 0.0, 0, 0)
    return (float(np.mean(atk_off)), float(np.mean(def_off)), cohorts, len(atk_off))


def fit_pit(ld, season, cutoff, cfg: DCConfig, prior=None):
    M = _M()
    train = ld.available(cutoff)
    if len(train) < cfg.min_matches:
        return None
    teams = sorted({t for m in train for t in (m.home, m.away)} | ld.participants(season))
    index = {t: i for i, t in enumerate(teams)}
    n = len(teams)
    before = ld.teams_before_season(season)
    promoted = frozenset(t for t in ld.participants(season) if t not in before)
    if prior is None:
        prior = promoted_prior(ld, season, cutoff, cfg.prior_reg)
    pa, pd_ = prior[0], prior[1]
    prior_atk = np.array([pa if t in promoted else 0.0 for t in teams])
    prior_def = np.array([pd_ if t in promoted else 0.0 for t in teams])

    def weights(ms):
        return np.exp(-cfg.xi * np.array([days_between(cutoff, m.kickoff) for m in ms]))

    hi, ai, yh, ya = _design(train, index, "goals")
    g = M.fit(n, hi, ai, yh, ya, weights(train), prior_atk, prior_def, reg=cfg.reg, use_dc=cfg.use_dc)

    xgfit = None
    if cfg.xg_alpha > 0:
        xtrain = ld.xg_available(cutoff)
        if len(xtrain) >= cfg.min_matches_xg:
            hx, ax, yhx, yax = _design(xtrain, index, "xg")
            x = M.fit(n, hx, ax, yhx, yax, weights(xtrain), prior_atk, prior_def, reg=cfg.reg, use_dc=False)
            xgfit = {"atk": x["atk"], "dfn": x["def"], "home_adv": x["home_adv"], "mu": x["mu"],
                     "converged": x["ok"], "n_train": len(xtrain)}

    return Fit(league=ld.league, season=season, cutoff=cutoff, index=index, atk=g["atk"], dfn=g["def"],
               home_adv=float(g["home_adv"]), mu=float(g["mu"]), rho=float(g["rho"]),
               converged=bool(g["ok"]), n_train=len(train), promoted=promoted, prior=tuple(prior), xg=xgfit)


class UnknownTeam(KeyError):
    pass


def predict(fit: Fit, home, away, cfg: DCConfig):
    """1X2 + goal-market probabilities. Unknown teams raise (fail closed)."""
    M = _M()
    if home not in fit.index or away not in fit.index:
        raise UnknownTeam(f"team not in fit universe: {home if home not in fit.index else away}")
    ih, ia = fit.index[home], fit.index[away]
    lh = np.exp(fit.mu + fit.home_adv + fit.atk[ih] - fit.dfn[ia])
    la = np.exp(fit.mu + fit.atk[ia] - fit.dfn[ih])
    used_xg = False
    if fit.xg is not None and cfg.xg_alpha > 0:
        x = fit.xg
        xh = np.exp(x["mu"] + x["home_adv"] + x["atk"][ih] - x["dfn"][ia])
        xa = np.exp(x["mu"] + x["atk"][ia] - x["dfn"][ih])
        a = cfg.xg_alpha
        lh = np.exp((1 - a) * np.log(lh) + a * np.log(xh))
        la = np.exp((1 - a) * np.log(la) + a * np.log(xa))
        used_xg = True
    mat = M.score_matrix(float(lh), float(la), fit.rho, max_goals=cfg.max_goals)
    mk = M.markets_from_matrix(mat)
    p = np.array([mk["home"], mk["draw"], mk["away"]], dtype=float)
    p = p / p.sum()
    return {"p": p, "over25": float(mk["over25"]), "btts": float(mk["btts_yes"]),
            "lam_h": float(lh), "lam_a": float(la), "used_xg": used_xg,
            "promoted_home": home in fit.promoted, "promoted_away": away in fit.promoted,
            "model_ok": fit.converged and (fit.xg is None or fit.xg["converged"])}
