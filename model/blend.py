"""Evaluate blending the goals-based ratings with xG-based ratings.

xG coverage starts at season 2025, so the blend can only be judged on matches
late enough in 2025 for an xG model to have history to fit on. Everything is
still strictly walk-forward.

Blend is geometric in the rate (arithmetic in log-lambda), which is the
natural scale for a log-link model:

    log lambda = (1 - a) * log lambda_goals + a * log lambda_xg
"""

import numpy as np
import dcmodel as M
import backtest as B

CACHE = {}


def blended_backtest(league, xi, reg, alpha, refit_days=14,
                     season=2025, start_after=120):
    """start_after: skip the first N matches of the season so the xG model has
    something to fit on. Those matches are still used for training."""
    key = league
    if key not in CACHE:
        CACHE[key] = B.prep(league)
    matches, names, index, n_teams, first_seen = CACHE[key]

    pk = ("prior", league)
    if pk not in CACHE:
        CACHE[pk] = M.estimate_promoted_prior(matches, index, n_teams)
    promoted_prior = CACHE[pk]

    test = sorted([m for m in matches if m["season"] == season],
                  key=lambda r: r["date"])[start_after:]
    if not test:
        return None

    rows = []
    fg = fx = None
    last = None
    for m in test:
        d = m["date"]
        if last is None or (d - last).astype(int) >= refit_days:
            g = M.fit_asof(matches, index, n_teams, d, xi, reg,
                           promoted_prior, first_seen, response="goals")
            x = M.fit_asof(matches, index, n_teams, d, xi, reg,
                           promoted_prior, first_seen, response="xg",
                           min_matches=80)
            if g is not None:
                fg, last = g, d
            if x is not None:
                fx = x
        if fg is None:
            continue

        lgh, lga = M.lambdas(fg, index, m["home"], m["away"])
        if fx is not None and alpha > 0:
            xh, xa = M.lambdas(fx, index, m["home"], m["away"])
            lam_h = np.exp((1 - alpha) * np.log(lgh) + alpha * np.log(xh))
            lam_a = np.exp((1 - alpha) * np.log(lga) + alpha * np.log(xa))
        else:
            lam_h, lam_a = lgh, lga

        mk = M.markets_from_matrix(M.score_matrix(lam_h, lam_a, fg["rho"]))
        res = 0 if m["hg"] > m["ag"] else (1 if m["hg"] == m["ag"] else 2)
        p = np.clip(np.array([mk["home"], mk["draw"], mk["away"]]), 1e-6, 1)
        p /= p.sum()
        rows.append((p, res, mk["over25"],
                     1 if (m["hg"] + m["ag"]) >= 3 else 0))

    if not rows:
        return None
    P = np.vstack([r[0] for r in rows])
    y = np.array([r[1] for r in rows])
    ll = -np.mean(np.log(P[np.arange(len(y)), y]))
    po = np.clip(np.array([r[2] for r in rows]), 1e-6, 1 - 1e-6)
    yo = np.array([r[3] for r in rows])
    ll_ou = -np.mean(yo * np.log(po) + (1 - yo) * np.log(1 - po))
    return {"n": len(rows), "logloss": ll, "ll_ou": ll_ou}


if __name__ == "__main__":
    xi, reg = 0.0015, 2.0
    print(f"xG blend sweep (xi={xi}, reg={reg}), season 2025 after match 120")
    print("alpha   " + "  ".join(f"{lg:>9s}" for lg in M.LEAGUES) +
          "     POOLED   POOLED_OU")
    best = None
    for alpha in [0.0, 0.2, 0.35, 0.5, 0.65, 0.8, 1.0]:
        per, num, den, ou_n = [], 0.0, 0, 0.0
        for lg in M.LEAGUES:
            r = blended_backtest(lg, xi, reg, alpha)
            per.append(r["logloss"])
            num += r["logloss"] * r["n"]
            ou_n += r["ll_ou"] * r["n"]
            den += r["n"]
        pooled = num / den
        print(f"{alpha:<7.2f} " + "  ".join(f"{v:9.4f}" for v in per) +
              f"  {pooled:9.4f}  {ou_n/den:9.4f}")
        if best is None or pooled < best[0]:
            best = (pooled, alpha)
    print(f"\nBEST pooled logloss={best[0]:.4f} at alpha={best[1]}")
