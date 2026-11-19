"""The ridge penalty shrinks team ratings toward the league mean, which
compresses predicted probabilities toward (1/3, 1/3, 1/3): longshots come out
too high and favourites too low. Fit a single sharpening exponent

    p'_i  proportional to  p_i ** gamma

on strictly out-of-sample walk-forward predictions and see whether gamma > 1
genuinely improves log-loss, or whether the compression was imaginary.

gamma is fitted on seasons 2023-2024 and tested on 2025, so the reported test
number is not the number that chose gamma.
"""

import numpy as np
import dcmodel as M
import validate as V


def collect(seasons, refit_days=14):
    rows = []
    for lg in M.LEAGUES:
        rows += V.predict_set(lg, lambda m: m["season"] in seasons,
                              refit_days=refit_days)
    P = np.vstack([r[0] for r in rows])
    y = np.array([r[1] for r in rows])
    return P, y


def apply_gamma(P, gamma):
    Q = np.clip(P, 1e-9, 1) ** gamma
    return Q / Q.sum(axis=1, keepdims=True)


def logloss(P, y):
    return -np.mean(np.log(np.clip(P[np.arange(len(y)), y], 1e-12, 1)))


if __name__ == "__main__":
    print("collecting walk-forward predictions ...")
    Ptr, ytr = collect([2023, 2024])
    Pte, yte = collect([2025])
    print(f"  fit set  n={len(ytr)}   test set n={len(yte)}")

    grid = np.arange(0.80, 1.61, 0.01)
    lls = [logloss(apply_gamma(Ptr, g), ytr) for g in grid]
    best_g = float(grid[int(np.argmin(lls))])

    print(f"\nfitted on 2023-2024:  best gamma = {best_g:.2f}")
    print(f"  fit-set  logloss  gamma=1.00 -> {logloss(Ptr, ytr):.4f}   "
          f"gamma={best_g:.2f} -> {logloss(apply_gamma(Ptr, best_g), ytr):.4f}")

    base = logloss(Pte, yte)
    tuned = logloss(apply_gamma(Pte, best_g), yte)
    print(f"\nHELD-OUT 2025:")
    print(f"  gamma=1.00 -> {base:.4f}")
    print(f"  gamma={best_g:.2f} -> {tuned:.4f}   ({tuned-base:+.4f})")

    print("\n  gamma sweep on held-out 2025 (for reference only):")
    for g in [0.9, 1.0, 1.1, 1.2, 1.3, 1.4]:
        print(f"    gamma={g:.2f}  logloss={logloss(apply_gamma(Pte, g), yte):.4f}")

    # where does the sharpening actually change things?
    print("\n  effect on the probability scale (gamma=%.2f):" % best_g)
    for p in [0.05, 0.10, 0.20, 0.35, 0.50, 0.70]:
        trio = np.array([[p, (1 - p) / 2, (1 - p) / 2]])
        print(f"    p={p:.2f} -> {apply_gamma(trio, best_g)[0,0]:.3f}")
