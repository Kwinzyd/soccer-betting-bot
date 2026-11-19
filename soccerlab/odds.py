"""Odds arithmetic: conversions, de-vigging, EV and settlement.

Conventions
-----------
* decimal odds d > 1 are the canonical internal price (stake included).
* American odds must satisfy |a| >= 100; anything in (-100, 100) is not a
  valid American price and raises InvalidOdds instead of being coerced.
* "implied" probability = 1/d (includes the margin). "fair"/"de-vigged"
  probability has the margin removed by an explicit, named method.
* Commission is charged on net winnings only (exchange convention).
"""

import math

import numpy as np


class InvalidOdds(ValueError):
    pass


# ------------------------------------------------------------ conversions

def american_to_decimal(a) -> float:
    try:
        a = float(a)
    except (TypeError, ValueError) as exc:
        raise InvalidOdds(f"not a number: {a!r}") from exc
    if not math.isfinite(a) or -100.0 < a < 100.0:
        raise InvalidOdds(f"invalid American odds {a!r}: |a| must be >= 100")
    return 1.0 + a / 100.0 if a > 0 else 1.0 + 100.0 / (-a)


def decimal_to_american(d) -> float:
    d = check_decimal(d)
    return (d - 1.0) * 100.0 if d >= 2.0 else -100.0 / (d - 1.0)


def check_decimal(d, max_decimal=1001.0) -> float:
    try:
        d = float(d)
    except (TypeError, ValueError) as exc:
        raise InvalidOdds(f"not a number: {d!r}") from exc
    if not math.isfinite(d) or d <= 1.0 or d > max_decimal:
        raise InvalidOdds(f"invalid decimal odds {d!r}")
    return d


def implied(d) -> float:
    return 1.0 / check_decimal(d)


def overround(decimals) -> float:
    return float(sum(implied(d) for d in decimals) - 1.0)


# ------------------------------------------------------------- de-vigging

def devig_proportional(probs) -> np.ndarray:
    p = np.asarray(probs, dtype=float)
    if np.any(p <= 0) or not np.all(np.isfinite(p)):
        raise InvalidOdds("implied probabilities must be positive and finite")
    return p / p.sum()


def devig_power(probs, tol=1e-12, max_iter=200) -> np.ndarray:
    """Find k with sum(p_i ** k) = 1 and return p_i ** k.

    Unlike proportional de-vigging this removes more margin from longshots
    than favourites, matching the favourite-longshot bias. The bracket is
    widened as needed and non-convergence raises instead of silently
    returning an unconverged exponent.
    """
    p = np.asarray(probs, dtype=float)
    if np.any(p <= 0) or np.any(p >= 1) or not np.all(np.isfinite(p)):
        raise InvalidOdds(f"implied probabilities must lie in (0, 1): {p}")
    s1 = p.sum()
    if abs(s1 - 1.0) < tol:
        return p / s1
    lo, hi = (1.0, 2.0) if s1 > 1.0 else (0.5, 1.0)
    for _ in range(60):                       # widen until bracketed
        if (np.sum(p ** lo) - 1.0) * (np.sum(p ** hi) - 1.0) <= 0:
            break
        lo, hi = (lo, hi * 2.0) if s1 > 1.0 else (lo / 2.0, hi)
    else:
        raise InvalidOdds("power de-vig could not bracket the exponent")
    for _ in range(max_iter):
        k = 0.5 * (lo + hi)
        s = np.sum(p ** k)
        if abs(s - 1.0) < tol:
            break
        if s > 1.0:
            lo = k
        else:
            hi = k
    else:
        raise InvalidOdds("power de-vig did not converge")
    q = p ** k
    return q / q.sum()


def fair_from_decimals(decimals, method="power") -> np.ndarray:
    imp = np.array([implied(d) for d in decimals])
    if method == "power":
        return devig_power(imp)
    if method == "proportional":
        return devig_proportional(imp)
    raise ValueError(f"unknown de-vig method {method!r}")


# ------------------------------------------------------------ EV and P&L

def net_decimal(d, commission=0.0) -> float:
    """Effective decimal odds after commission on winnings."""
    if not 0.0 <= commission < 1.0:
        raise ValueError("commission must be in [0, 1)")
    d = check_decimal(d)
    return 1.0 + (d - 1.0) * (1.0 - commission)


def expected_value(p, d, commission=0.0) -> float:
    """Expected profit per unit staked at probability p."""
    if not 0.0 <= p <= 1.0:
        raise ValueError(f"probability out of range: {p}")
    return p * net_decimal(d, commission) - 1.0


def kelly_fraction(p, d, commission=0.0) -> float:
    """Full-Kelly fraction (diagnostic only; never a staking recommendation)."""
    b = net_decimal(d, commission) - 1.0
    return max(0.0, (p * b - (1.0 - p)) / b)


SETTLE_OUTCOMES = ("win", "loss", "void")


def settle_pnl(stake, d, outcome, commission=0.0) -> float:
    """Profit/loss of a single-selection bet.

    void (postponed/abandoned/cancelled under the market's rules) returns the
    stake: P&L zero.
    """
    if stake < 0:
        raise ValueError("negative stake")
    if outcome == "win":
        return stake * (net_decimal(d, commission) - 1.0)
    if outcome == "loss":
        return -float(stake)
    if outcome == "void":
        return 0.0
    raise ValueError(f"unknown outcome {outcome!r}")


# --------------------------------------------------------- quote validity

def validate_1x2(decimals, max_overround=0.25, min_overround=-0.02, max_decimal=101.0):
    """Return a list of reasons a 1X2 quote is unusable ([] = usable).

    Rejects in-play/settled degenerate prices (e.g. -100000 / +35000 seen in
    the BallDontLie feed), implausibly wide books and arbitrage-negative
    books, which in this data are almost always stale or mis-mapped quotes.
    """
    reasons = []
    try:
        ds = [check_decimal(d, max_decimal=max_decimal) for d in decimals]
    except InvalidOdds as exc:
        return [f"invalid price: {exc}"]
    if len(ds) != 3:
        return ["1X2 quote must have exactly three prices"]
    ov = sum(1.0 / d for d in ds) - 1.0
    if ov > max_overround:
        reasons.append(f"overround {ov:.3f} > {max_overround}")
    if ov < min_overround:
        reasons.append(f"overround {ov:.3f} < {min_overround} (stale or mismatched)")
    return reasons
