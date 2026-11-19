"""Forecast and betting metrics with clustered uncertainty.

Bootstrap methodology
---------------------
Resampling is by cluster (default: fixture), implemented with multinomial
cluster weights: each replicate draws n_clusters clusters with replacement
and every row inherits its cluster's draw count as a weight. Statistics are
computed as weighted means / ratios, which is exactly equivalent to
concatenating the resampled clusters. Percentile intervals, fixed seed.

ECE methodology
---------------
Class-wise ECE over the three 1X2 outcomes: for each outcome k, predictions
p_k are put in equal-width bins on [0, 1]; ECE_k = sum_b (n_b / N) *
|mean(p_k in b) - freq(y = k in b)|; reported ECE is the mean over k.
"""

import numpy as np

EPS = 1e-12


def _check(P, y):
    P = np.asarray(P, dtype=float)
    y = np.asarray(y, dtype=int)
    if P.ndim != 2 or P.shape[0] != y.shape[0]:
        raise ValueError("P must be (n, k) and y length n")
    if not np.allclose(P.sum(axis=1), 1.0, atol=1e-6):
        raise ValueError("probability rows must sum to 1")
    if np.any(P < 0):
        raise ValueError("negative probability")
    return P, y


def logloss_rows(P, y):
    P, y = _check(P, y)
    return -np.log(np.clip(P[np.arange(len(y)), y], EPS, 1.0))


def brier_rows(P, y):
    P, y = _check(P, y)
    oh = np.zeros_like(P)
    oh[np.arange(len(y)), y] = 1.0
    return np.sum((P - oh) ** 2, axis=1)


def calibration_table(p, hit, edges=(0, .05, .10, .15, .20, .25, .30, .40, .50, .60, .75, 1.0000001)):
    """Rows of (lo, hi, n, mean_pred, freq, gap) for a binary event."""
    p = np.asarray(p, dtype=float)
    hit = np.asarray(hit, dtype=float)
    out = []
    for lo, hi in zip(edges[:-1], edges[1:], strict=True):
        sel = (p >= lo) & (p < hi)
        n = int(sel.sum())
        if n == 0:
            continue
        mp, fr = float(p[sel].mean()), float(hit[sel].mean())
        out.append({"lo": float(lo), "hi": float(min(hi, 1.0)), "n": n,
                    "mean_pred": mp, "freq": fr, "gap": fr - mp})
    return out


def ece_classwise(P, y, n_bins=10):
    P, y = _check(P, y)
    edges = np.linspace(0, 1, n_bins + 1)
    edges[-1] += 1e-9
    per = []
    for k in range(P.shape[1]):
        p, hit = P[:, k], (y == k).astype(float)
        tot = 0.0
        for lo, hi in zip(edges[:-1], edges[1:], strict=True):
            sel = (p >= lo) & (p < hi)
            if sel.any():
                tot += sel.mean() * abs(p[sel].mean() - hit[sel].mean())
        per.append(tot)
    return float(np.mean(per)), [float(v) for v in per]


# --------------------------------------------------------------- bootstrap

def cluster_weights(clusters, n_boot, seed):
    """Yield per-row weight vectors for a cluster bootstrap."""
    clusters = np.asarray(clusters)
    uniq, inv = np.unique(clusters, return_inverse=True)
    rng = np.random.default_rng(seed)
    n = len(uniq)
    for _ in range(n_boot):
        counts = rng.multinomial(n, np.full(n, 1.0 / n))
        yield counts[inv].astype(float)


def bootstrap_mean(values, clusters=None, n_boot=2000, seed=0, alpha=0.05):
    v = np.asarray(values, dtype=float)
    if len(v) == 0:
        return {"n": 0, "mean": None, "lo": None, "hi": None}
    clusters = np.arange(len(v)) if clusters is None else clusters
    stats = [float(np.dot(w, v) / w.sum()) for w in cluster_weights(clusters, n_boot, seed)]
    lo, hi = np.quantile(stats, [alpha / 2, 1 - alpha / 2])
    return {"n": int(len(v)), "n_clusters": int(len(np.unique(clusters))), "mean": float(v.mean()),
            "lo": float(lo), "hi": float(hi), "se": float(np.std(stats, ddof=1))}


def bootstrap_ratio(num, den, clusters=None, n_boot=2000, seed=0, alpha=0.05):
    """CI for sum(num)/sum(den), e.g. ROI = profit / turnover."""
    a, b = np.asarray(num, dtype=float), np.asarray(den, dtype=float)
    if len(a) == 0 or b.sum() == 0:
        return {"n": int(len(a)), "value": None, "lo": None, "hi": None}
    clusters = np.arange(len(a)) if clusters is None else clusters
    stats = []
    for w in cluster_weights(clusters, n_boot, seed):
        d = np.dot(w, b)
        stats.append(float(np.dot(w, a) / d) if d > 0 else np.nan)
    stats = np.asarray(stats)
    stats = stats[np.isfinite(stats)]
    lo, hi = np.quantile(stats, [alpha / 2, 1 - alpha / 2])
    return {"n": int(len(a)), "n_clusters": int(len(np.unique(clusters))),
            "value": float(a.sum() / b.sum()), "lo": float(lo), "hi": float(hi),
            "se": float(np.std(stats, ddof=1))}


def max_drawdown(pnl):
    """Largest peak-to-trough fall of cumulative P&L (in units, >= 0)."""
    c = np.cumsum(np.asarray(pnl, dtype=float))
    if len(c) == 0:
        return 0.0
    peak = np.maximum.accumulate(np.concatenate([[0.0], c]))[1:]
    return float(np.max(peak - c))


def verdict(ci, n, min_n, better="lower", threshold=0.0):
    """'pass' / 'fail' / 'inconclusive' from a CI against a threshold."""
    if n is None or n < min_n or ci.get("lo") is None:
        return "inconclusive (n=%s < %s)" % (n, min_n) if (n or 0) < min_n else "inconclusive"
    if better == "lower":
        if ci["hi"] < threshold:
            return "pass"
        if ci["lo"] > threshold:
            return "fail"
        return "inconclusive (CI straddles %g)" % threshold
    if ci["lo"] > threshold:
        return "pass"
    if ci["hi"] < threshold:
        return "fail"
    return "inconclusive (CI straddles %g)" % threshold
