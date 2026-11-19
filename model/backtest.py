"""Walk-forward backtest: tune xi/reg and check calibration honestly.

At each refit date the model sees ONLY matches strictly before it, so nothing
downstream of a prediction leaks into the rating that made it.
"""

import sys
import numpy as np
import dcmodel as M


def prep(league):
    matches, names = M.load_league(league)
    ids = {t for m in matches for t in (m["home"], m["away"])}
    # Include current-season entrants that have not played yet (a side promoted
    # this summer has no match rows at all). They carry no likelihood term, so
    # the ridge leaves them sitting exactly on the promoted-team prior, which
    # is the correct estimate for a team with no top-flight history.
    ids |= set(M.current_season_team_ids(league))
    ids = sorted(ids)
    index = {t: i for i, t in enumerate(ids)}
    first_seen = M.first_season_seen(matches)
    return matches, names, index, len(ids), first_seen


def walk_forward(league, xi, reg, seasons, refit_days=14, response="goals",
                 promoted_prior=None, cache={}):
    matches, names, index, n_teams, first_seen = cache.setdefault(
        league, prep(league))
    if promoted_prior is None:
        key = ("prior", league)
        if key not in cache:
            cache[key] = M.estimate_promoted_prior(matches, index, n_teams)
        promoted_prior = cache[key]

    test = [m for m in matches if m["season"] in seasons]
    if not test:
        return None
    test.sort(key=lambda r: r["date"])

    rows = []
    f = None
    last_fit = None
    for m in test:
        d = m["date"]
        if last_fit is None or (d - last_fit).astype(int) >= refit_days:
            nf = M.fit_asof(matches, index, n_teams, d, xi, reg,
                            promoted_prior, first_seen, response=response)
            if nf is not None:
                f, last_fit = nf, d
        if f is None:
            continue
        lam_h, lam_a = M.lambdas(f, index, m["home"], m["away"])
        mk = M.markets_from_matrix(
            M.score_matrix(lam_h, lam_a, f.get("rho", 0.0)))
        res = 0 if m["hg"] > m["ag"] else (1 if m["hg"] == m["ag"] else 2)
        p = np.array([mk["home"], mk["draw"], mk["away"]])
        p = np.clip(p, 1e-6, 1)
        p /= p.sum()
        rows.append({"p": p, "res": res,
                     "over": mk["over25"],
                     "over_actual": 1 if (m["hg"] + m["ag"]) >= 3 else 0})
    if not rows:
        return None

    P = np.vstack([r["p"] for r in rows])
    y = np.array([r["res"] for r in rows])
    logloss = -np.mean(np.log(P[np.arange(len(y)), y]))
    onehot = np.zeros_like(P)
    onehot[np.arange(len(y)), y] = 1
    brier = np.mean(np.sum((P - onehot) ** 2, axis=1))

    po = np.clip(np.array([r["over"] for r in rows]), 1e-6, 1 - 1e-6)
    yo = np.array([r["over_actual"] for r in rows])
    ll_ou = -np.mean(yo * np.log(po) + (1 - yo) * np.log(1 - po))

    return {"n": len(rows), "logloss": logloss, "brier": brier,
            "ll_ou": ll_ou, "P": P, "y": y, "po": po, "yo": yo}


def baselines(league, seasons, cache={}):
    matches, names, index, n_teams, first_seen = cache.setdefault(
        league, prep(league))
    test = [m for m in matches if m["season"] in seasons]
    train = [m for m in matches if m["season"] not in seasons]
    if not train:
        train = test
    c = np.zeros(3)
    for m in train:
        c[0 if m["hg"] > m["ag"] else (1 if m["hg"] == m["ag"] else 2)] += 1
    c /= c.sum()
    y = np.array([0 if m["hg"] > m["ag"] else (1 if m["hg"] == m["ag"] else 2)
                  for m in test])
    ll = -np.mean(np.log(c[y]))
    return {"n": len(y), "logloss": ll, "rates": c}


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "tune"
    seasons = [2024, 2025]

    if mode == "base":
        for lg in M.LEAGUES:
            b = baselines(lg, seasons)
            print(f"{lg:8s} baseline(home/draw/away rates) n={b['n']:4d} "
                  f"logloss={b['logloss']:.4f}  rates={np.round(b['rates'],3)}")

    elif mode == "tune":
        # half-lives: ln2/xi days
        grid_xi = [0.0015, 0.003, 0.005, 0.008]
        grid_reg = [0.5, 2.0, 8.0]
        print("xi      halflife  reg    " +
              "  ".join(f"{lg:>9s}" for lg in M.LEAGUES) + "     POOLED")
        best = None
        for xi in grid_xi:
            for reg in grid_reg:
                lls, ns = [], []
                per = []
                for lg in M.LEAGUES:
                    r = walk_forward(lg, xi, reg, seasons)
                    per.append(r["logloss"])
                    lls.append(r["logloss"] * r["n"])
                    ns.append(r["n"])
                pooled = sum(lls) / sum(ns)
                print(f"{xi:<7.4f} {np.log(2)/xi:7.0f}d {reg:<5.1f} " +
                      "  ".join(f"{v:9.4f}" for v in per) +
                      f"  {pooled:9.4f}")
                if best is None or pooled < best[0]:
                    best = (pooled, xi, reg)
        print(f"\nBEST pooled logloss={best[0]:.4f} at xi={best[1]} "
              f"(half-life {np.log(2)/best[1]:.0f}d) reg={best[2]}")
