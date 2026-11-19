"""Honest validation.

Two things matter here that a season-average backtest hides:

1. Calibration -- when the model says 40%, does it happen ~40% of the time?
2. EARLY-SEASON performance. Today's fixtures are matchday 1-2, where every
   rating comes from last season plus a promoted-team prior and no current
   form exists at all. That is the regime the predictions will actually be
   used in, so it is the regime that has to be measured.
"""

import numpy as np
import dcmodel as M
import backtest as B

XI, REG = 0.0015, 2.0
CACHE = {}


def get(league):
    if league not in CACHE:
        CACHE[league] = B.prep(league)
    return CACHE[league]


def prior_for(league):
    matches, names, index, n_teams, first_seen = get(league)
    k = ("prior", league)
    if k not in CACHE:
        CACHE[k] = M.estimate_promoted_prior(matches, index, n_teams)
    return CACHE[k]


def predict_set(league, selector, refit_days=7):
    """Walk-forward predictions over matches picked by `selector(match)`."""
    matches, names, index, n_teams, first_seen = get(league)
    pp = prior_for(league)
    test = sorted([m for m in matches if selector(m)], key=lambda r: r["date"])
    out = []
    f = None
    last = None
    for m in test:
        d = m["date"]
        if last is None or (d - last).astype(int) >= refit_days:
            nf = M.fit_asof(matches, index, n_teams, d, XI, REG, pp, first_seen)
            if nf is not None:
                f, last = nf, d
        if f is None:
            continue
        lh, la = M.lambdas(f, index, m["home"], m["away"])
        mk = M.markets_from_matrix(M.score_matrix(lh, la, f["rho"]))
        res = 0 if m["hg"] > m["ag"] else (1 if m["hg"] == m["ag"] else 2)
        p = np.clip(np.array([mk["home"], mk["draw"], mk["away"]]), 1e-6, 1)
        p /= p.sum()
        out.append((p, res))
    return out


def metrics(rows):
    if not rows:
        return None
    P = np.vstack([r[0] for r in rows])
    y = np.array([r[1] for r in rows])
    ll = -np.mean(np.log(P[np.arange(len(y)), y]))
    oh = np.zeros_like(P)
    oh[np.arange(len(y)), y] = 1
    brier = np.mean(np.sum((P - oh) ** 2, axis=1))
    return {"n": len(y), "logloss": ll, "brier": brier, "P": P, "y": y}


def matchday_bucket(m, season_start_cache={}):
    """Approximate matchday from round_number if present, else by date rank."""
    return m.get("round_number")


if __name__ == "__main__":
    print("=" * 74)
    print("1. EARLY SEASON vs REST OF SEASON  (walk-forward, seasons 2023-2025)")
    print("=" * 74)

    early_all, late_all = [], []
    for lg in M.LEAGUES:
        matches, names, index, n_teams, first_seen = get(lg)
        # rank matches within each season by date to define matchday buckets
        by_season = {}
        for m in matches:
            by_season.setdefault(m["season"], []).append(m)
        rank = {}
        for s, lst in by_season.items():
            lst.sort(key=lambda r: r["date"])
            # group into matchdays of ~ (n_teams_in_season / 2)
            per_round = max(1, len(set(x["home"] for x in lst)) // 2)
            for i, mm in enumerate(lst):
                rank[mm["id"]] = i // per_round + 1

        early = predict_set(
            lg, lambda m: m["season"] in (2023, 2024, 2025) and rank[m["id"]] <= 3)
        late = predict_set(
            lg, lambda m: m["season"] in (2023, 2024, 2025) and rank[m["id"]] > 3,
            refit_days=14)
        e, l = metrics(early), metrics(late)
        early_all += early
        late_all += late
        print(f"  {lg:8s} early n={e['n']:3d} logloss={e['logloss']:.4f} | "
              f"rest n={l['n']:4d} logloss={l['logloss']:.4f}  "
              f"(early is {e['logloss']-l['logloss']:+.4f})")

    E, L = metrics(early_all), metrics(late_all)
    print(f"\n  POOLED   early n={E['n']:4d} logloss={E['logloss']:.4f}   "
          f"rest n={L['n']:5d} logloss={L['logloss']:.4f}   "
          f"penalty {E['logloss']-L['logloss']:+.4f}")

    print("\n" + "=" * 74)
    print("2. CALIBRATION  (all walk-forward predictions, seasons 2023-2025)")
    print("=" * 74)
    allrows = early_all + late_all
    A = metrics(allrows)
    P, y = A["P"], A["y"]
    flatp = P.reshape(-1)
    flaty = np.zeros_like(P)
    flaty[np.arange(len(y)), y] = 1
    flaty = flaty.reshape(-1)
    edges = [0, .05, .10, .15, .20, .25, .30, .40, .50, .60, .75, 1.01]
    print("  predicted band     n    mean_pred   actual    diff")
    for lo, hi in zip(edges[:-1], edges[1:]):
        sel = (flatp >= lo) & (flatp < hi)
        if sel.sum() < 20:
            continue
        mp, ac = flatp[sel].mean(), flaty[sel].mean()
        print(f"  {lo:.2f}-{hi:.2f}   {sel.sum():6d}    {mp:6.3f}   {ac:6.3f}  "
              f"{ac-mp:+7.3f}")
    print(f"\n  overall logloss={A['logloss']:.4f}  brier={A['brier']:.4f}  n={A['n']}")
