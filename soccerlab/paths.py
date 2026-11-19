"""Filesystem locations. Research output never lands in production paths."""

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LEGACY_MODEL_DIR = REPO_ROOT / "model"
DATA_DIR = REPO_ROOT / "balldontlie_consolidated"
ODDS_HIST_DIR = REPO_ROOT / "odds_hist"
REGISTRY_DIR = REPO_ROOT / "registry"
REPORTS_DIR = REPO_ROOT / "reports"

# Research scratch/state (SQLite stores, snapshots). Gitignored.
VAR_DIR = Path(os.environ.get("SOCCERLAB_VAR", REPO_ROOT / "var"))

# Optional read-only pointer to the live production checkout, used only to
# *copy* snapshots of its odds log. Nothing in soccerlab writes there.
PROD_ROOT = Path(os.environ["SOCCERLAB_PROD_ROOT"]) if os.environ.get("SOCCERLAB_PROD_ROOT") else None


def ensure_var() -> Path:
    VAR_DIR.mkdir(parents=True, exist_ok=True)
    return VAR_DIR


def assert_not_production(path: Path) -> None:
    """Refuse to write inside the production checkout."""
    if PROD_ROOT is None:
        return
    p, prod = Path(path).resolve(), PROD_ROOT.resolve()
    if p == prod or prod in p.parents:
        raise PermissionError(f"refusing to write inside production checkout: {p}")
