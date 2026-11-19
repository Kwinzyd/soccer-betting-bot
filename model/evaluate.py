"""The decisive test: does the model beat the closing line?

Pinnacle closing odds are about the sharpest public benchmark there is. If the
model cannot match them on log-loss, it has no business betting into them --
and betting simulations at the closing price will confirm it.

Everything is walk-forward: the rating used for a match is fitted only on
matches strictly before it.
"""

import json
import os
import numpy as np
import dcmodel as M
import validate as V

HERE = os.path.dirname(os.path.abspath(__file__))
ALPHA = 0.3


def walk_predictions(league, seasons, refit_days=14):
    """[(match_id, model_probs, result, total_goals)] walk-forward."""
    matches, names, index, n_teams, first_seen = V.get(league)
    pp = V.prior_for(league)
    test = sorted([m for m in matches if m["season"] in seasons],
                  key=lambda r: r["date"])
    out = []
    fg = fx = None
    last = None
    for m in test:
        d = m["date"]
        if last is None or (d - last).astype(int) >= refit_days:
            g = M.fit_asof(matches, index, n_teams, d, V.XI, V.REG, pp,
                           first_seen, response="goals")
            x = M.fit_asof(matches, index, n_teams, d, V.XI, V.REG, pp,
                           first_seen, response="xg", min_matches=80)
            if g is not None:
                fg, last = g, d
            if x is not None:
                fx = x
        if fg is None:
            continue
        lh, la = M.lambdas(fg, index, m["home"], m["away"])
        if fx is not None and ALPHA > 0:
            xh, xa = M.lambdas(fx, index, m["home"], m["away"])
            lh = np.exp((1 - ALPHA) * np.log(lh) + ALPHA * np.log(xh))
            la = np.exp((1 - ALPHA) * np.log(la) + ALPHA * np.log(xa))
        mk = M.markets_from_matrix(M.score_matrix(lh, la, fg["rho"]))
        p = np.clip(np.array([mk["home"], mk["draw"], mk["away"]]), 1e-9, 1)
        p /= p.sum()
        res = 0 if m["hg"] > m["ag"] else (1 if m["hg"] == m["ag"] else 2)
        out.append((str(m["id"]), p, res, mk["over25"],
                    1 if m["hg"] + m["ag"] >= 3 else 0))
    return out


def ll(P, y):
    return -np.mean(np.log(np.clip(P[np.arange(len(y)), y], 1e-12, 1)))


def run(seasons, tag, odds_key="pinnacle_close"):
    J = json.load(open(os.path.join(HERE, "odds_joined.json"), encoding="utf-8"))

    mp, mk, ys, ids = [], [], [], []
    for lg in M.LEAGUES:
        book = J[lg]
        for mid, p, res, pov, yov in walk_predictions(lg, seasons):
            o = book.get(mid)
            if not o or odds_key not in o:
                continue
            dec = np.array(o[odds_key], dtype=float)
            raw = 1.0 / dec
            fair = M.devig_power(raw)
            mp.append(p)
            mk.append(fair)
            ys.append(res)
            ids.append((lg, mid, dec))

    P = np.vstack(mp)
    Q = np.vstack(mk)
    y = np.array(ys)

    print(f"\n{'='*70}\n{tag}   n={len(y)}   benchmark={odds_key}\n{'='*70}")
    lm, lq = ll(P, y), ll(Q, y)
    print(f"  model  log-loss = {lm:.4f}")
    print(f"  market log-loss = {lq:.4f}    (market is {lm-lq:+.4f} vs model)")

    # 50/50 blend of model and market, a standard way to see if the model adds
    # anything at all on top of the price
    Bp = 0.5 * P + 0.5 * Q
    Bp /= Bp.sum(axis=1, keepdims=True)
    print(f"  50/50 blend     = {ll(Bp, y):.4f}")
    for w in (0.2, 0.3):
        Z = (1 - w) * Q + w * P
        Z /= Z.sum(axis=1, keepdims=True)
        print(f"  {int(w*100):2d}% model + {int((1-w)*100)}% market = {ll(Z, y):.4f}")

    # ---- betting simulation at the closing price
    print(f"\n  flat-stake betting at the closing price, 1 unit per qualifying bet:")
    print("   edge_thr    bets      staked    profit      ROI")
    for thr in (0.02, 0.03, 0.05, 0.08):
        stake = prof = nb = 0.0
        for i in range(len(y)):
            for k in range(3):
                if P[i, k] - Q[i, k] > thr:
                    nb += 1
                    stake += 1.0
                    prof += (ids[i][2][k] - 1.0) if y[i] == k else -1.0
        roi = (prof / stake * 100) if stake else 0.0
        print(f"   {thr:5.2f}    {int(nb):5d}  {stake:10.0f}  {prof:+9.1f}  "
              f"{roi:+6.2f}%")
    return P, Q, y


if __name__ == "__main__":
    run([2025], "SEASON 2025 (100% odds coverage, cleanest test)")
    run([2022, 2023, 2024, 2025], "ALL SEASONS 2022-2025 (partial coverage)")
