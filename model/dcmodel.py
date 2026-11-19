"""
Dixon-Coles style bivariate Poisson rating model for the four leagues pulled
into balldontlie_consolidated/.

Structure
---------
    log lambda_home = home_adv + atk[home] - def[away]
    log lambda_away =           atk[away] - def[home]

fitted by penalised maximum likelihood with

  * Dixon-Coles low-score correlation term rho (goals model only -- the
    correction is about the discrete 0-0/1-0/0-1/1-1 cells)
  * exponential time decay  w = exp(-xi * days_before_asof)
  * ridge penalty pulling each team toward a prior mean; the prior is 0
    (league average) for established teams and an empirically estimated
    "promoted team" offset for sides in their first top-flight season.

The same machinery is fitted twice per league: once on actual goals, once on
expected goals (quasi-Poisson -- the Poisson score equations are valid for any
non-negative continuous response under a log link). The two lambda estimates
are blended.
"""

import json
import os
import numpy as np
from scipy.optimize import minimize

BASE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "balldontlie_consolidated")

LEAGUES = ["epl", "laliga", "seriea", "ligue1"]
SEASONS = [2022, 2023, 2024, 2025, 2026]
XG_SEASONS = [2025, 2026]

MAX_GOALS = 12


# --------------------------------------------------------------------------
# data loading
# --------------------------------------------------------------------------

def load_league(league):
    """Return (matches, team_names). Matches sorted by date, finished only.

    Each match dict: home, away (team ids), hg, ag, date (np.datetime64),
    season, plus xgh/xga when the stats endpoint covered it.
    """
    names = {}
    for season in SEASONS:
        path = os.path.join(BASE, f"{league}_{season}_teams.json")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                for t in json.load(f):
                    names[t["id"]] = t["name"]

    # xG lookup: match_id -> (home_xg, away_xg) needs the match record to know
    # which team was home, so collect raw per-team rows first.
    xg_rows = {}
    for season in XG_SEASONS:
        path = os.path.join(BASE, f"{league}_{season}_team_match_stats.json")
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as f:
            for r in json.load(f):
                if r.get("expected_goals") is not None:
                    xg_rows.setdefault(r["match_id"], {})[r["team_id"]] = r["expected_goals"]

    matches = []
    for season in SEASONS:
        path = os.path.join(BASE, f"{league}_{season}_matches.json")
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as f:
            for m in json.load(f):
                if m.get("status_state") != "final":
                    continue
                if m.get("home_score") is None or m.get("away_score") is None:
                    continue
                h, a = m["home_team_id"], m["away_team_id"]
                rec = {
                    "id": m["id"], "home": h, "away": a,
                    "hg": int(m["home_score"]), "ag": int(m["away_score"]),
                    "date": np.datetime64(m["date"][:10]), "season": season,
                    "xgh": None, "xga": None,
                }
                pair = xg_rows.get(m["id"])
                if pair and h in pair and a in pair:
                    rec["xgh"], rec["xga"] = pair[h], pair[a]
                matches.append(rec)

    matches.sort(key=lambda r: r["date"])
    return matches, names


def current_season_team_ids(league, season=2026):
    """Team ids listed for `season`, including sides yet to play a match."""
    path = os.path.join(BASE, f"{league}_{season}_teams.json")
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return [t["id"] for t in json.load(f)]


def first_season_seen(matches):
    """team id -> earliest season the team appears in this league's data."""
    first = {}
    for m in matches:
        for t in (m["home"], m["away"]):
            if t not in first or m["season"] < first[t]:
                first[t] = m["season"]
    return first


# --------------------------------------------------------------------------
# likelihood
# --------------------------------------------------------------------------

def _dc_logtau(hg, ag, lam_h, lam_a, rho):
    """log of the Dixon-Coles correction, vectorised over matches."""
    tau = np.ones_like(lam_h)
    m00 = (hg == 0) & (ag == 0)
    m01 = (hg == 0) & (ag == 1)
    m10 = (hg == 1) & (ag == 0)
    m11 = (hg == 1) & (ag == 1)
    tau = np.where(m00, 1.0 - lam_h * lam_a * rho, tau)
    tau = np.where(m01, 1.0 + lam_h * rho, tau)
    tau = np.where(m10, 1.0 + lam_a * rho, tau)
    tau = np.where(m11, 1.0 - rho, tau)
    # keep the optimiser inside the region where tau stays positive
    return np.log(np.clip(tau, 1e-9, None))


def _obj_grad(p, n_teams, hi, ai, y_h, y_a, w, prior_atk, prior_def, reg):
    """Penalised negative log-likelihood of the plain Poisson part, and its
    exact gradient. rho is handled separately (see fit)."""
    atk_r = p[:n_teams]
    dfn_r = p[n_teams:2 * n_teams]
    home_adv = p[2 * n_teams]
    mu = p[2 * n_teams + 1]

    atk = atk_r - atk_r.mean()
    dfn = dfn_r - dfn_r.mean()

    # mu is the global log scoring rate. Without it, mean(atk)=mean(def)=0
    # forces mean(log lambda_away) to 0 i.e. ~1.0 away goals per game, and the
    # ridge gets bent out of shape absorbing the difference from the real rate.
    log_lh = mu + home_adv + atk[hi] - dfn[ai]
    log_la = mu + atk[ai] - dfn[hi]
    lam_h = np.exp(np.clip(log_lh, -6, 3))
    lam_a = np.exp(np.clip(log_la, -6, 3))

    ll = w * (y_h * log_lh - lam_h + y_a * log_la - lam_a)
    obj = -np.sum(ll)
    obj += 0.5 * reg * np.sum((atk - prior_atk) ** 2)
    obj += 0.5 * reg * np.sum((dfn - prior_def) ** 2)

    # Nothing above depends on the mean of the RAW parameters (they are centred
    # before use), which leaves a flat ray the optimiser will wander along until
    # it hits the box bounds -- silently corrupting any team whose rating is
    # determined by the ridge alone. Pinning the raw mean removes the ray; since
    # the rest of the objective is invariant to it, any K > 0 puts the mean at
    # exactly 0, so raw and centred values coincide.
    K = 1.0
    m_atk, m_dfn = atk_r.mean(), dfn_r.mean()
    obj += 0.5 * K * (m_atk ** 2 + m_dfn ** 2)

    # d(-ll)/d(log_lh) = -w*(y_h - lam_h), likewise for away
    rh = -w * (y_h - lam_h)
    ra = -w * (y_a - lam_a)

    g_atk = (np.bincount(hi, weights=rh, minlength=n_teams) +
             np.bincount(ai, weights=ra, minlength=n_teams))
    g_def = (-np.bincount(ai, weights=rh, minlength=n_teams) +
             -np.bincount(hi, weights=ra, minlength=n_teams))
    g_home = np.sum(rh)
    g_mu = np.sum(rh) + np.sum(ra)

    g_atk += reg * (atk - prior_atk)
    g_def += reg * (dfn - prior_def)

    # chain through the centring projection: d(x - mean(x))/dx_i = I - 1/n
    g_atk = g_atk - g_atk.mean()
    g_def = g_def - g_def.mean()

    # gradient of the mean-pinning term (acts on the raw parameters directly)
    n = len(atk_r)
    g_atk = g_atk + K * m_atk / n
    g_def = g_def + K * m_dfn / n

    return obj, np.concatenate([g_atk, g_def, [g_home, g_mu]])


def fit(n_teams, hi, ai, y_h, y_a, w, prior_atk, prior_def,
        reg=1.0, use_dc=True, x0=None):
    """Penalised MLE. Returns dict of fitted parameters.

    hi/ai are integer team indices; y_h/y_a the responses (goals or xG);
    w the per-match weights.

    Fitted in two stages: the rating parameters under a plain Poisson
    likelihood (exact gradient, fast), then the Dixon-Coles low-score
    parameter rho by 1-D search holding the ratings fixed. rho is very nearly
    orthogonal to the rating block, so the split costs almost nothing and
    keeps the big optimisation clean.
    """
    if x0 is None:
        x0 = np.zeros(2 * n_teams + 2)
        x0[2 * n_teams] = 0.25              # home advantage
        x0[2 * n_teams + 1] = np.log(1.2)   # global scoring rate

    res = minimize(_obj_grad, x0, jac=True,
                   args=(n_teams, hi, ai, y_h, y_a, w, prior_atk, prior_def, reg),
                   method="L-BFGS-B", options={"maxiter": 500, "ftol": 1e-11})

    p = res.x
    atk = p[:n_teams] - p[:n_teams].mean()
    dfn = p[n_teams:2 * n_teams] - p[n_teams:2 * n_teams].mean()
    home_adv = float(p[2 * n_teams])
    mu = float(p[2 * n_teams + 1])

    rho = 0.0
    if use_dc:
        lam_h = np.exp(mu + home_adv + atk[hi] - dfn[ai])
        lam_a = np.exp(mu + atk[ai] - dfn[hi])

        def nll_rho(r):
            r = float(r)
            val = -np.sum(w * _dc_logtau(y_h, y_a, lam_h, lam_a, r))
            return val + 0.5 * 50.0 * r ** 2

        grid = np.linspace(-0.25, 0.25, 101)
        vals = [nll_rho(g) for g in grid]
        rho = float(grid[int(np.argmin(vals))])

    return {"atk": atk, "def": dfn, "home_adv": home_adv, "mu": mu,
            "rho": rho, "x": p, "ok": bool(res.success)}


# --------------------------------------------------------------------------
# assembling a fit as of a given date
# --------------------------------------------------------------------------

def build_design(matches, index, response="goals"):
    """Vectors for the subset of matches that have the requested response."""
    hi, ai, yh, ya, dates = [], [], [], [], []
    for m in matches:
        if response == "goals":
            a, b = m["hg"], m["ag"]
        else:
            a, b = m["xgh"], m["xga"]
            if a is None or b is None:
                continue
        hi.append(index[m["home"]])
        ai.append(index[m["away"]])
        yh.append(a)
        ya.append(b)
        dates.append(m["date"])
    return (np.array(hi, dtype=int), np.array(ai, dtype=int),
            np.array(yh, dtype=float), np.array(ya, dtype=float),
            np.array(dates, dtype="datetime64[D]"))


def estimate_promoted_prior(matches, index, n_teams):
    """Average atk/def of teams during their FIRST season in this league.

    Fitted per-season with no decay so each cohort is measured on its own
    season, then averaged over cohorts. This is what a newly promoted side
    with no top-flight history should be assumed to be worth.
    """
    first = first_season_seen(matches)
    atk_off, def_off = [], []
    for season in SEASONS:
        sub = [m for m in matches if m["season"] == season]
        if len(sub) < 100:
            continue
        # a team counts as promoted only if this league's data actually starts
        # for it here AND we have an earlier season to contrast with
        promoted = [t for t, s in first.items() if s == season and season > SEASONS[0]]
        if not promoted:
            continue
        hi, ai, yh, ya, _ = build_design(sub, index, "goals")
        w = np.ones(len(hi))
        z = np.zeros(n_teams)
        f = fit(n_teams, hi, ai, yh, ya, w, z, z, reg=1.0, use_dc=False)
        for t in promoted:
            atk_off.append(f["atk"][index[t]])
            def_off.append(f["def"][index[t]])
    if not atk_off:
        return 0.0, 0.0
    return float(np.mean(atk_off)), float(np.mean(def_off))


def fit_asof(matches, index, n_teams, asof, xi, reg, promoted_prior,
             first_seen, response="goals", min_matches=60):
    """Fit using only matches strictly before `asof`, time-decayed."""
    sub = [m for m in matches if m["date"] < asof]
    if len(sub) < min_matches:
        return None
    hi, ai, yh, ya, dates = build_design(sub, index, response)
    if len(hi) < min_matches:
        return None
    age_days = (np.datetime64(asof, "D") - dates).astype("timedelta64[D]").astype(float)
    w = np.exp(-xi * age_days)

    # prior: promoted offset for teams whose first season is the season in play
    prior_atk = np.zeros(n_teams)
    prior_def = np.zeros(n_teams)
    asof_season = max([m["season"] for m in sub])
    pa, pd_ = promoted_prior
    for t, i in index.items():
        # 9999 => never appears in any match, i.e. promoted and yet to play
        if first_seen.get(t, 9999) >= asof_season:
            prior_atk[i] = pa
            prior_def[i] = pd_

    return fit(n_teams, hi, ai, yh, ya, w, prior_atk, prior_def,
               reg=reg, use_dc=(response == "goals"))


# --------------------------------------------------------------------------
# prediction
# --------------------------------------------------------------------------

def lambdas(f, index, home, away):
    ih, ia = index[home], index[away]
    mu = f.get("mu", 0.0)
    lam_h = np.exp(mu + f["home_adv"] + f["atk"][ih] - f["def"][ia])
    lam_a = np.exp(mu + f["atk"][ia] - f["def"][ih])
    return float(lam_h), float(lam_a)


def score_matrix(lam_h, lam_a, rho=0.0, max_goals=MAX_GOALS):
    k = np.arange(max_goals)
    # Poisson pmf without scipy.stats, via log-gamma-free cumulative product
    logfact = np.cumsum(np.log(np.maximum(k, 1)))
    logfact[0] = 0.0
    ph = np.exp(k * np.log(lam_h) - lam_h - logfact)
    pa = np.exp(k * np.log(lam_a) - lam_a - logfact)
    M = np.outer(ph, pa)
    if rho:
        M[0, 0] *= 1.0 - lam_h * lam_a * rho
        M[0, 1] *= 1.0 + lam_h * rho
        M[1, 0] *= 1.0 + lam_a * rho
        M[1, 1] *= 1.0 - rho
        M = np.clip(M, 0.0, None)
    return M / M.sum()


def markets_from_matrix(M):
    n = M.shape[0]
    idx = np.arange(n)
    home = M[idx[:, None] > idx[None, :]].sum()
    draw = np.trace(M)
    away = M[idx[:, None] < idx[None, :]].sum()
    total = idx[:, None] + idx[None, :]
    over25 = M[total >= 3].sum()
    btts = M[1:, 1:].sum()
    return {"home": home, "draw": draw, "away": away,
            "over25": over25, "under25": 1 - over25,
            "btts_yes": btts, "btts_no": 1 - btts}


# --------------------------------------------------------------------------
# odds helpers
# --------------------------------------------------------------------------

def american_to_prob(o):
    return 100.0 / (o + 100.0) if o > 0 else (-o) / ((-o) + 100.0)


def american_to_decimal(o):
    return o / 100.0 + 1.0 if o > 0 else 100.0 / (-o) + 1.0


def devig_power(probs, tol=1e-12):
    """Power (Shin-like) de-vig: find k with sum(p_i**k) = 1.

    Better than proportional de-vigging for 1X2 because it does not assume the
    bookmaker's margin is spread evenly across a heavy favourite and a
    longshot -- proportional de-vig systematically overstates longshot fair
    probability, which is exactly where spurious "edges" show up.
    """
    p = np.asarray(probs, dtype=float)
    lo, hi = 0.5, 3.0
    for _ in range(200):
        k = 0.5 * (lo + hi)
        s = np.sum(p ** k)
        if abs(s - 1.0) < tol:
            break
        if s > 1.0:
            lo = k
        else:
            hi = k
    return p ** k / np.sum(p ** k)
