"""Test the routes that could actually make money, as opposed to the one that
already failed (model vs closing price, bet at closing price).

S1  model edge vs sharp CLOSE, bet at close            -- already known to lose
S2  model edge vs sharp OPEN,  bet at best open price  -- is the model early?
S3  no model at all: bet the best price across books whenever it beats the
    sharp price at the same moment. Pure line shopping, fully implementable.

Plus the diagnostic that decides whether any model-based strategy can work:
CLOSING LINE VALUE. If the model's picks at the opening price are good, the
market should drift toward them by kickoff. If it drifts away, the model is
simply wrong and no amount of staking cleverness rescues it.
"""

import json
import os
import numpy as np
import dcmodel as M
import evaluate as E

HERE = os.path.dirname(os.path.abspath(__file__))
SEASONS = [2022, 2023, 2024, 2025]


def load():
    J = json.load(open(os.path.join(HERE, "odds_joined.json"), encoding="utf-8"))
    rows = []
    for lg in M.LEAGUES:
        book = J[lg]
        for mid, p, res, pov, yov in E.walk_predictions(lg, SEASONS):
            o = book.get(mid)
            if not o:
                continue
            rows.append({"lg": lg, "p": p, "y": res, "o": o})
    return rows


def fair(dec):
    return M.devig_power(1.0 / np.asarray(dec, dtype=float))


def sim(rows, edge_ref, bet_at, thr, use_model=True):
    """Stake 1 unit whenever the criterion fires. Returns (n, profit, roi)."""
    n = 0
    prof = 0.0
    for r in rows:
        o = r["o"]
        if edge_ref not in o or bet_at not in o:
            continue
        f = fair(o[edge_ref])
        price = np.asarray(o[bet_at], dtype=float)
        for k in range(3):
            if use_model:
                trigger = r["p"][k] - f[k] > thr
            else:
                # no model: is the best available price better than sharp fair?
                trigger = price[k] * f[k] - 1.0 > thr
            if trigger:
                n += 1
                prof += (price[k] - 1.0) if r["y"] == k else -1.0
    roi = 100 * prof / n if n else 0.0
    return n, prof, roi


def clv(rows, thr):
    """For model picks made at the opening price, did the market move toward
    us by close? Positive number = closing price shorter than we took = good."""
    moves = []
    for r in rows:
        o = r["o"]
        if "pinnacle_open" not in o or "pinnacle_close" not in o:
            continue
        fo = fair(o["pinnacle_open"])
        fc = fair(o["pinnacle_close"])
        for k in range(3):
            if r["p"][k] - fo[k] > thr:
                moves.append(fc[k] - fo[k])
    if not moves:
        return 0, 0.0
    return len(moves), float(np.mean(moves))


if __name__ == "__main__":
    print("building walk-forward predictions ...")
    rows = load()
    print(f"  {len(rows)} matches with odds\n")

    print("=" * 72)
    print("A.  LOG-LOSS: model vs the market at OPEN and at CLOSE")
    print("=" * 72)
    for key, lab in (("pinnacle_open", "Pinnacle OPEN"),
                     ("pinnacle_close", "Pinnacle CLOSE")):
        P, Q, y = [], [], []
        for r in rows:
            if key in r["o"]:
                P.append(r["p"])
                Q.append(fair(r["o"][key]))
                y.append(r["y"])
        P, Q, y = np.vstack(P), np.vstack(Q), np.array(y)
        print(f"  vs {lab:15s} n={len(y):5d}   model={E.ll(P,y):.4f}   "
              f"market={E.ll(Q,y):.4f}   gap={E.ll(P,y)-E.ll(Q,y):+.4f}")

    print("\n" + "=" * 72)
    print("B.  CLOSING LINE VALUE of the model's picks (taken at the open)")
    print("=" * 72)
    print("   thr    picks   mean move in fair prob, open -> close")
    for thr in (0.02, 0.03, 0.05, 0.08):
        n, mv = clv(rows, thr)
        verdict = "toward us" if mv > 0 else "AWAY from us"
        print(f"   {thr:4.2f}  {n:6d}    {mv*100:+6.2f} pp   ({verdict})")

    print("\n" + "=" * 72)
    print("C.  STRATEGIES  (1 unit flat, ROI on turnover)")
    print("=" * 72)
    print("   strategy                                    thr    bets     ROI")
    for thr in (0.02, 0.05):
        n, p, roi = sim(rows, "pinnacle_close", "pinnacle_close", thr)
        print(f"   S1 model vs close, bet close              {thr:5.2f} {n:7d}  {roi:+6.2f}%")
    for thr in (0.02, 0.05):
        n, p, roi = sim(rows, "pinnacle_open", "max_open", thr)
        print(f"   S2 model vs open,  bet BEST open price    {thr:5.2f} {n:7d}  {roi:+6.2f}%")
    for thr in (0.0, 0.01, 0.02):
        n, p, roi = sim(rows, "pinnacle_open", "max_open", thr, use_model=False)
        print(f"   S3 NO MODEL, best open price > sharp fair {thr:5.2f} {n:7d}  {roi:+6.2f}%")
    for thr in (0.0, 0.01, 0.02):
        n, p, roi = sim(rows, "pinnacle_close", "max_close", thr, use_model=False)
        print(f"   S4 NO MODEL, best close price > sharp fair{thr:5.2f} {n:7d}  {roi:+6.2f}%")
