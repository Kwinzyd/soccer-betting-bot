"""Filesystem locations. Research output never lands in production paths."""

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LEGACY_MODEL_DIR = REPO_ROOT / "model"
DATA_DIR = REPO_ROOT / "balldontlie_consolidated"
ODDS_HIST_DIR = REPO_ROOT / "odds_hist"
REGISTRY_DIR = REPO_ROOT / "registry"
REPORTS_DIR = REPO_ROOT / "reports"

# Marker of a live production checkout: the legacy logger's dedup state file
# exists only where the scheduled task actually runs.
PRODUCTION_MARKER = Path("odds_logger") / "seen.json"


def var_dir() -> Path:
    """Research scratch/state (SQLite stores, snapshots). Self-gitignored.
    Resolved at call time so SOCCERLAB_VAR can be changed (tests do)."""
    return Path(os.environ.get("SOCCERLAB_VAR", REPO_ROOT / "var"))


def prod_root():
    """Optional explicit pointer to the live checkout (read-only use only)."""
    v = os.environ.get("SOCCERLAB_PROD_ROOT")
    return Path(v) if v else None


def ensure_var() -> Path:
    d = var_dir()
    assert_not_production(d)
    d.mkdir(parents=True, exist_ok=True)
    gi = d / ".gitignore"
    if not gi.exists():
        gi.write_text("*\n", encoding="utf-8")   # research state never enters git
    return d


def assert_not_production(path) -> None:
    """Refuse to write inside the production checkout: either the explicit
    SOCCERLAB_PROD_ROOT, or any ancestor that carries the live logger's
    state file."""
    p = Path(path).resolve()
    explicit = prod_root()
    if explicit is not None:
        prod = explicit.resolve()
        if p == prod or prod in p.parents:
            raise PermissionError(f"refusing to write inside production checkout: {p}")
    for anc in (p, *p.parents):
        if (anc / PRODUCTION_MARKER).exists():
            raise PermissionError(f"refusing to write inside a live production checkout ({anc}): {p}")
