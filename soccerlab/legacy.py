"""Read-only adapter around the legacy model (model/*.py).

Nothing here edits legacy code. It imports the legacy modules, optionally
points them at data materialised from a git commit (so the baseline can be
re-run even where production keeps refreshing the live schedule files), and
exposes the legacy computations for regression comparison.
"""

import contextlib
import importlib
import sys
import tempfile
from pathlib import Path

import numpy as np

from .hashing import git_blob_bytes, git_tracked
from .paths import LEGACY_MODEL_DIR, REPO_ROOT


def modules():
    """Import (once) and return the legacy modules as a dict."""
    if str(LEGACY_MODEL_DIR) not in sys.path:
        sys.path.insert(0, str(LEGACY_MODEL_DIR))
    names = ("dcmodel", "backtest", "validate", "evaluate", "predict")
    return {n: importlib.import_module(n) for n in names}


def _clear_caches(mods):
    """The legacy code caches league data in module dicts and in mutable
    default arguments. Clear all of them so a data swap takes effect."""
    for n in ("validate", "predict", "blend"):
        if n in mods and hasattr(mods[n], "CACHE"):
            mods[n].CACHE.clear()
    B = mods["backtest"]
    for fn in (B.walk_forward, B.baselines):
        for d in fn.__defaults__ or ():
            if isinstance(d, dict):
                d.clear()


@contextlib.contextmanager
def data_root(path):
    """Temporarily point legacy dcmodel.BASE at another consolidated dir."""
    mods = modules()
    M = mods["dcmodel"]
    old = M.BASE
    M.BASE = str(path)
    _clear_caches(mods)
    try:
        yield mods
    finally:
        M.BASE = old
        _clear_caches(mods)


@contextlib.contextmanager
def commit_data(commit, prefix="balldontlie_consolidated/"):
    """Materialise the consolidated data exactly as it was at `commit`."""
    with tempfile.TemporaryDirectory(prefix="soccerlab_legacy_") as tmp:
        for path in git_tracked(commit, cwd=REPO_ROOT):
            if path.startswith(prefix):
                dest = Path(tmp) / path[len(prefix):]
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(git_blob_bytes(commit, path, cwd=REPO_ROOT))
        with data_root(tmp) as mods:
            yield mods


def fits_asof(mods, league, asof):
    """Exactly what legacy predict.fits() computes, at a chosen date."""
    P = mods["predict"]
    P.CACHE.clear()
    P.ASOF = np.datetime64(asof)
    fg, fx, pp = P.fits(league)
    matches, names, index, n_teams, first_seen = P.get(league)
    return {"goals": fg, "xg": fx, "promoted_prior": pp, "index": index, "names": names,
            "first_seen": first_seen}


def predict_fixture(mods, league, home_name, away_name):
    mk, err = mods["predict"].predict(league, home_name, away_name)
    if err:
        raise KeyError(err)
    return mk


def fit_to_json(f, index):
    if f is None:
        return None
    inv = {i: t for t, i in index.items()}
    return {
        "home_adv": float(f["home_adv"]), "mu": float(f["mu"]), "rho": float(f["rho"]),
        "converged": bool(f["ok"]),
        "atk": {str(inv[i]): float(v) for i, v in enumerate(f["atk"])},
        "def": {str(inv[i]): float(v) for i, v in enumerate(f["def"])},
    }
