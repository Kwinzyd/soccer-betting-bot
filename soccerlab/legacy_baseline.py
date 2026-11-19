"""Freeze and verify the `soccer-dc-legacy` baseline manifest.

    python -m soccerlab baseline freeze      (once; refuses to overwrite)
    python -m soccerlab baseline verify [--golden]

The legacy model refits from scratch on every run, so it has no stored
coefficients. To pin it, the freeze re-runs the legacy code (unmodified) on
the baseline commit's data at a fixed as-of date and records the resulting
coefficients and golden predictions. `verify --golden` repeats that and
fails on any difference beyond floating-point tolerance.
"""

import json
import platform
import subprocess
import sys
from datetime import datetime, timezone

import numpy as np

from . import legacy, manifest as MF
from .hashing import aggregate_sha256, git_blob_bytes, git_tracked
from .paths import REGISTRY_DIR, REPO_ROOT

MODEL_ID = "soccer-dc-legacy"
BASELINE_COMMIT = "e1b55feffc29f3bf6f3d74d136ff49bf413d96ad"
GOLDEN_ASOF = "2026-11-19"
GOLDEN_PER_LEAGUE = 3
TOL = 1e-6
MANIFEST_PATH = REGISTRY_DIR / MODEL_ID / "manifest.json"

CODE_SUFFIXES = (".py", ".ps1", ".md")

# Tracked files the live scheduled tasks rewrite in the production checkout.
# Their drift in a working tree is expected and only warns; the git history
# check still pins their baseline bytes.
LIVE_MUTATED = {
    "balldontlie_consolidated/epl_2026_matches.json": "refreshed every 6h by log_odds.py --refresh-schedules",
    "balldontlie_consolidated/laliga_2026_matches.json": "refreshed every 6h by log_odds.py --refresh-schedules",
    "balldontlie_consolidated/seriea_2026_matches.json": "refreshed every 6h by log_odds.py --refresh-schedules",
    "balldontlie_consolidated/ligue1_2026_matches.json": "refreshed every 6h by log_odds.py --refresh-schedules",
    "active_rosters.json": "rewritten daily by fetch_rosters.py sync",
}


def _is_code(path):
    return path.endswith(CODE_SUFFIXES) or path == ".gitignore"


def gradient_check(seed=0, n_teams=8, n_matches=120, reg=2.0):
    """Analytic vs central-difference gradient of legacy _obj_grad."""
    M = legacy.modules()["dcmodel"]
    rng = np.random.default_rng(seed)
    hi = rng.integers(0, n_teams, n_matches)
    ai = (hi + rng.integers(1, n_teams, n_matches)) % n_teams
    yh = rng.poisson(1.5, n_matches).astype(float)
    ya = rng.poisson(1.1, n_matches).astype(float)
    w = np.exp(-0.002 * rng.integers(0, 600, n_matches))
    pa = rng.normal(0, 0.1, n_teams)
    pd_ = rng.normal(0, 0.1, n_teams)
    x = rng.normal(0, 0.2, 2 * n_teams + 2)
    x[-1] = np.log(1.2)
    _, g = M._obj_grad(x, n_teams, hi, ai, yh, ya, w, pa, pd_, reg)
    num = np.zeros_like(x)
    h = 1e-6
    for i in range(len(x)):
        e = np.zeros_like(x)
        e[i] = h
        fp, _ = M._obj_grad(x + e, n_teams, hi, ai, yh, ya, w, pa, pd_, reg)
        fm, _ = M._obj_grad(x - e, n_teams, hi, ai, yh, ya, w, pa, pd_, reg)
        num[i] = (fp - fm) / (2 * h)
    return float(np.max(np.abs(num - g)) / max(1.0, float(np.max(np.abs(num)))))


def compute_golden(commit=BASELINE_COMMIT, asof=GOLDEN_ASOF):
    """Coefficients + golden predictions, legacy code on the commit's data."""
    coeffs, golden = {}, []
    with legacy.commit_data(commit) as mods:
        M = mods["dcmodel"]
        for lg in M.LEAGUES:
            f = legacy.fits_asof(mods, lg, asof)
            coeffs[lg] = {
                "promoted_prior": [float(f["promoted_prior"][0]), float(f["promoted_prior"][1])],
                "goals": legacy.fit_to_json(f["goals"], f["index"]),
                "xg": legacy.fit_to_json(f["xg"], f["index"]),
            }
            with open(f"{M.BASE}/{lg}_2026_matches.json", encoding="utf-8") as fh:
                sched = json.load(fh)
            names = f["names"]
            upcoming = sorted((m for m in sched if m.get("status_state") == "scheduled"
                               and m["date"][:10] >= asof), key=lambda m: (m["date"], m["id"]))
            for m in upcoming[:GOLDEN_PER_LEAGUE]:
                mk = legacy.predict_fixture(mods, lg, names[m["home_team_id"]], names[m["away_team_id"]])
                golden.append({
                    "league": lg, "match_id": m["id"], "kickoff": m["date"],
                    "home": names[m["home_team_id"]], "away": names[m["away_team_id"]],
                    "p_home": float(mk["home"]), "p_draw": float(mk["draw"]), "p_away": float(mk["away"]),
                    "p_over25": float(mk["over25"]),
                    "lam_h": float(mk["lam_h"]), "lam_a": float(mk["lam_a"]),
                    "used_xg": bool(mk["used_xg"]),
                })
    return coeffs, golden


def _run_selftest():
    r = subprocess.run([sys.executable, "settle.py", "--selftest"], cwd=REPO_ROOT / "odds_logger",
                       capture_output=True, text=True)
    return {"command": "python odds_logger/settle.py --selftest", "exit_code": r.returncode,
            "passed": r.returncode == 0,
            "note": "settle.py reads the BallDontLie key at import time, so even --selftest needs credentials present"}


def build(commit=BASELINE_COMMIT):
    import numpy
    import scipy
    tracked = git_tracked(commit, cwd=REPO_ROOT)
    code, data = [], []
    for p in tracked:
        b = git_blob_bytes(commit, p, cwd=REPO_ROOT)
        if _is_code(p):
            code.append(MF.file_entry(p, b, protected=True))
        else:
            note = LIVE_MUTATED.get(p) or ("rewritten by fetch_rosters.py" if p.startswith("rosters/") else None)
            data.append(MF.file_entry(p, b, protected=note is None, note=note))

    mods = legacy.modules()
    P, M = mods["predict"], mods["dcmodel"]
    coeffs, golden = compute_golden(commit)
    grad_err = gradient_check()

    return {
        "schema": MF.SCHEMA,
        "model_id": MODEL_ID,
        "role": "legacy-baseline",
        "status": "UNVALIDATED -- preserved for comparison and regression only",
        "frozen_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "git": {"commit": commit, "repository": "Kwinzyd/soccer-betting-bot"},
        "description": ("Dixon-Coles bivariate Poisson ratings with time decay, ridge to a "
                        "promoted-team prior, rho by grid search, and a geometric goals/xG "
                        "blend; 1X2 picks flagged when model minus best-book de-vigged "
                        "probability exceeds 2pp and Kelly > 0."),
        "entrypoints": {
            "predict": "model/predict.py (via bot.py)",
            "fit": "model/dcmodel.py:fit_asof",
            "prior": "model/dcmodel.py:estimate_promoted_prior",
        },
        "hyperparameters": {
            "xi": P.XI, "half_life_days": float(np.log(2) / P.XI), "reg": P.REG, "xg_alpha": P.ALPHA,
            "max_goals": M.MAX_GOALS, "seasons": M.SEASONS, "xg_seasons": M.XG_SEASONS,
            "leagues": M.LEAGUES, "refit_days_backtest": 14, "min_matches_goals": 60,
            "min_matches_xg": 80, "rho_grid": [-0.25, 0.25, 101], "rho_ridge": 50.0,
            "promoted_prior_reg": 1.0, "optimizer": "L-BFGS-B maxiter=500 ftol=1e-11",
            "mean_pin_K": 1.0, "log_lambda_clip": [-6, 3],
            "edge_threshold": 0.02, "devig": "power, per best-priced book",
            "stake": "1 paper unit; half-Kelly displayed only",
            "asof_granularity": "UTC calendar date; matches with date < asof",
        },
        "golden_asof": GOLDEN_ASOF,
        "learned_coefficients": coeffs,
        "golden_predictions": golden,
        "golden_tolerance": TOL,
        "code_files": code,
        "code_files_aggregate_sha256": aggregate_sha256(code),
        "data_files": data,
        "data_files_aggregate_sha256": aggregate_sha256(data),
        "dependencies": {
            "python": platform.python_version(), "numpy": numpy.__version__, "scipy": scipy.__version__,
            "platform": platform.platform(),
            "lockfile": "none existed at baseline (no requirements file in repo)",
        },
        "providers": {
            "balldontlie": {"base": "https://api.balldontlie.io", "versions": {"laliga": "v1", "seriea": "v1",
                            "ligue1": "v1", "epl": "v2"},
                            "endpoints": ["matches", "teams", "team_match_stats", "odds"],
                            "rate_limit": "5 req/min (observed)"},
            "football-data.co.uk": {"files": "odds_hist/{E0,SP1,I1,F1}_{2223..2526}.csv (+SP1_2627)",
                                    "use": "historical Pinnacle/avg/max early and closing 1X2 odds"},
            "api-football": {"base": "https://v3.football.api-sports.io", "use": "fixtures + statistics (xG partial)"},
            "fotmob": {"use": "rosters (fetch_rosters.py); not used by the model"},
        },
        "production_entrypoints": [
            {"task": "balldontlie-odds-logger", "schedule": "every 3h",
             "command": "python -u bot.py", "cwd": "<repo root>",
             "effect": "polls odds, refreshes 2026 schedules, runs predict.py (non-preview): writes "
                       "odds_logger/paper.sqlite3 and sends Telegram alerts"},
            {"task": "balldontlie-odds-settle", "schedule": "daily", "command": "python -u settle.py --refresh",
             "cwd": "odds_logger", "effect": "writes results_cache.json, settled.jsonl; prints drift/ROI"},
            {"task": "balldontlie-roster-sync", "schedule": "daily", "command": "python -u fetch_rosters.py sync",
             "cwd": "<repo root>", "effect": "writes rosters/, active_rosters.json; may send Telegram"},
        ],
        "test_results": [
            _run_selftest(),
            {"command": "soccerlab.legacy_baseline.gradient_check()", "max_rel_error": grad_err,
             "passed": grad_err < 1e-6},
        ],
        "documented_performance": {
            "source": "model/README.md at baseline commit (not independently reproduced at freeze)",
            "walk_forward_logloss_2023_2025": {"n": 4336, "logloss": 0.9874},
            "vs_pinnacle_close_2025": {"n": 749, "model": 0.9892, "market": 0.9718},
            "vs_pinnacle_close_2022_2025": {"n": 3804, "model": 0.9982, "market": 0.9735},
            "flat_roi_at_close_2025": {"thr_0.02": -0.154, "thr_0.03": -0.139, "thr_0.05": -0.088, "thr_0.08": -0.017},
            "clv_open_to_close_pp": {"thr_0.02": -0.04, "thr_0.03": -0.05, "thr_0.05": -0.03},
            "optimal_blend_weight_on_model_vs_close": 0.0,
        },
        "known_evaluation_limitations": [
            "estimate_promoted_prior() uses every season in the data (incl. later ones) and is reused for all historical predictions",
            "backtest.baselines() trains on all non-test seasons, including later ones",
            "xi/reg tuned on 2024-2025 and alpha on 2025, then 2023-2025 reported as out-of-sample",
            "team index includes future-season entrants (affects centring of ratings)",
            "promoted flag uses max(season of training data), so at each season opener last season's promoted sides keep the promoted prior",
            "xG treated as available the day after each match; actually ingested 2026-08-22, with no provider timestamps",
            "football-data 'open' columns are early snapshots (Fri/Tue collection), not true opening prices",
            "settle.report_clv averages over all three 1X2 outcomes, which cancels to zero by construction",
            "settle.report_shopping counts every 3-hourly observation of a fixture as a separate bet",
            "predict.py de-vigs each side using the book that offers the best price on that side (biases edge upward)",
        ],
    }


def golden_check(m):
    errs = []
    coeffs, golden = compute_golden(m["git"]["commit"], m["golden_asof"])
    tol = m.get("golden_tolerance", TOL)
    for g_new, g_old in zip(golden, m["golden_predictions"], strict=False):  # count checked below
        for k in ("p_home", "p_draw", "p_away", "p_over25", "lam_h", "lam_a"):
            if abs(g_new[k] - g_old[k]) > tol:
                errs.append(f"golden {g_old['league']} {g_old['match_id']} {k}: {g_new[k]} != {g_old[k]}")
    if len(golden) != len(m["golden_predictions"]):
        errs.append("golden prediction count changed")
    for lg, c in m["learned_coefficients"].items():
        for kind in ("goals", "xg"):
            old, new = c[kind], coeffs[lg][kind]
            if (old is None) != (new is None):
                errs.append(f"{lg} {kind}: fit availability changed")
                continue
            if old is None:
                continue
            for k in ("home_adv", "mu", "rho"):
                if abs(old[k] - new[k]) > tol:
                    errs.append(f"{lg} {kind} {k}: {new[k]} != {old[k]}")
            for side in ("atk", "def"):
                for t, v in old[side].items():
                    if abs(new[side].get(t, np.inf) - v) > tol:
                        errs.append(f"{lg} {kind} {side}[{t}] changed")
    return errs


def freeze_cmd():
    sealed = MF.freeze(build(), MANIFEST_PATH)
    print(f"froze {MODEL_ID} -> {MANIFEST_PATH.relative_to(REPO_ROOT)}")
    print(f"manifest_sha256 {sealed['manifest_sha256']}")


def verify_cmd(golden=False):
    rep = MF.verify(MANIFEST_PATH, golden_fn=golden_check if golden else None)
    print(rep.render())
    return 0 if rep.ok else 1
