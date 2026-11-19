"""Model registry: roles, statuses, freeze and verify for challenger specs.

registry/index.json is the single source of truth for what a model id is
allowed to do:

  legacy-baseline   frozen for comparison; never produces soccerlab output
  challenger        research + shadow predictions only
  promoted          may generate production-paper entries/alerts. Only set
                    by a human after every gate in docs/EXPERIMENT_PLAN.md
                    passes. Nothing in this package writes this status.

    python -m soccerlab model list
    python -m soccerlab model freeze soccer-dc-pit-v1
    python -m soccerlab model verify soccer-dc-pit-v1 [--golden]
"""

import dataclasses
import json
import platform
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from . import dcpit, manifest as MF, pit
from .hashing import aggregate_sha256, git, git_blob_bytes, git_head, git_tracked, sha256_file
from .paths import DATA_DIR, REGISTRY_DIR, REPO_ROOT, REPORTS_DIR

INDEX = REGISTRY_DIR / "index.json"
STATUSES = ("legacy-baseline", "challenger", "promoted", "retired")
GOLDEN_CUTOFF = "2026-11-19T00:00:00Z"
TOL = 1e-6

SPECS = {
    "soccer-dc-pit-v1": {
        "config": dcpit.DCConfig(),
        "xg_mode": "provider_lag",
        "code": ["soccerlab/dcpit.py", "soccerlab/pit.py", "soccerlab/walkforward.py", "soccerlab/odds.py",
                 "soccerlab/timeutil.py", "soccerlab/legacy.py", "model/dcmodel.py"],
        "features": ["team attack/defence ratings from final-match goals (time-decayed)",
                     "home advantage, global scoring rate, Dixon-Coles rho",
                     "promoted-team prior from earlier cohorts only"],
        "training_population": "all final league matches with result_at <= cutoff (same league)",
        "calibration": "none (raw model probabilities)",
        "evaluation": {"experiment": "EXP-2026-11-A", "plan": "docs/EXPERIMENT_PLAN.md",
                       "report": "reports/EXP-2026-11-A.json"},
    },
}


def load_index():
    with open(INDEX, encoding="utf-8") as f:
        return json.load(f)


def status_of(model_id):
    for m in load_index()["models"]:
        if m["id"] == model_id:
            return m["status"]
    return "unregistered"


def _synthetic_golden(cfg):
    """Golden cases on a deterministic synthetic league: depends only on code."""
    from soccerlab.synthetic import synth_league, write_files
    with tempfile.TemporaryDirectory() as tmp:
        d = write_files(Path(tmp), synth_league(seed=42))
        ld = pit.load_league("syn", d, xg_mode="none")
        cutoff = pit.parse_utc("2023-09-15T00:00:00Z")
        f = dcpit.fit_pit(ld, 2023, cutoff, dataclasses.replace(cfg, min_matches=40))
        fx = [m for m in ld.matches if m.season == 2023 and m.kickoff > cutoff][:3]
        return [{"case": f"synthetic:{m.mid}", "p": [float(v) for v in dcpit.predict(f, m.home, m.away, cfg)["p"]]}
                for m in fx]


def _real_golden(cfg, xg_mode, data_dir=DATA_DIR):
    out = []
    cutoff = pit.parse_utc(GOLDEN_CUTOFF)
    for lg in ("epl", "laliga", "seriea", "ligue1"):
        ld = pit.load_league(lg, data_dir, xg_mode=xg_mode)
        f = dcpit.fit_pit(ld, 2026, cutoff, cfg)
        fx = [m for m in ld.matches if m.season == 2026 and m.kickoff > cutoff and not m.final][:3]
        for m in fx:
            r = dcpit.predict(f, m.home, m.away, cfg)
            out.append({"case": f"{lg}:{m.mid}", "p": [float(v) for v in r["p"]], "lam": [r["lam_h"], r["lam_a"]]})
    return out


def build(model_id):
    spec = SPECS[model_id]
    head = git_head(cwd=REPO_ROOT)
    dirty = git("status", "--porcelain", "--", *spec["code"], cwd=REPO_ROOT).decode().strip()
    if dirty:
        raise RuntimeError(f"commit model code before freezing; uncommitted: {dirty}")
    code = [MF.file_entry(p, git_blob_bytes(head, p, cwd=REPO_ROOT)) for p in spec["code"]]
    data = []
    for p in git_tracked(head, cwd=REPO_ROOT):
        if p.startswith("balldontlie_consolidated/"):
            live = p.endswith("_2026_matches.json")
            data.append(MF.file_entry(p, git_blob_bytes(head, p, cwd=REPO_ROOT), protected=not live,
                                      note="current season, refreshed by production" if live else None))
    plan = REPO_ROOT / spec["evaluation"]["plan"]
    rep = REPO_ROOT / spec["evaluation"]["report"]
    gates = json.loads(rep.read_text(encoding="utf-8"))["gates"] if rep.exists() else None
    import numpy as _np
    import scipy
    cfg = spec["config"]
    return {
        "schema": MF.SCHEMA, "model_id": model_id, "role": "challenger",
        "status": "challenger -- research/shadow only",
        "frozen_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "git": {"commit": head},
        "hyperparameters": cfg.to_dict(), "xg_mode": spec["xg_mode"],
        "features": spec["features"], "training_population": spec["training_population"],
        "historical_cutoff_rule": "fit at weekly 00:00 UTC cutoffs; latest cutoff <= decision time",
        "calibration": spec["calibration"],
        "evaluation": dict(spec["evaluation"], plan_sha256=sha256_file(plan),
                           report_sha256=sha256_file(rep) if rep.exists() else None),
        "evaluation_gates": {k: v.get("status") for k, v in (gates or {}).items() if isinstance(v, dict)},
        "metric_definitions": "soccerlab/metrics.py (log-loss natural log; Brier sum over 3; class-wise ECE)",
        "code_files": code, "code_files_aggregate_sha256": aggregate_sha256(code),
        "data_files": data, "data_files_aggregate_sha256": aggregate_sha256(data),
        "dependencies": {"python": platform.python_version(), "numpy": _np.__version__,
                         "scipy": scipy.__version__},
        "golden_synthetic": _synthetic_golden(cfg),
        "golden_real": {"cutoff": GOLDEN_CUTOFF, "cases": _real_golden(cfg, spec["xg_mode"])},
        "golden_tolerance": TOL,
    }


def golden_check(m):
    spec = SPECS[m["model_id"]]
    cfg = dcpit.DCConfig(**m["hyperparameters"])
    errs = []
    if cfg != spec["config"]:
        errs.append("registered config differs from frozen hyperparameters")
    for new, old in zip(_synthetic_golden(cfg), m["golden_synthetic"]):
        if not np.allclose(new["p"], old["p"], atol=m["golden_tolerance"], rtol=0):
            errs.append(f"golden {old['case']}: {new['p']} != {old['p']}")
    # real-data golden uses the commit's data, not the (possibly refreshed) working tree
    with tempfile.TemporaryDirectory() as tmp:
        for e in m["data_files"]:
            p = Path(tmp) / Path(e["path"]).name
            p.write_bytes(git_blob_bytes(m["git"]["commit"], e["path"], cwd=REPO_ROOT))
        real = _real_golden(cfg, m["xg_mode"], data_dir=tmp)
    for new, old in zip(real, m["golden_real"]["cases"]):
        if new["case"] != old["case"] or not np.allclose(new["p"], old["p"], atol=m["golden_tolerance"], rtol=0):
            errs.append(f"golden {old['case']}: {new['p']} != {old['p']}")
    return errs


def cli(action, model_id, golden=False):
    if action == "list":
        for m in load_index()["models"]:
            print(f"  {m['id']:22s} {m['status']:16s} {m.get('note', '')}")
        print(f"  promoted: {load_index().get('promoted') or 'none'}")
        return 0
    if model_id is None:
        print("model id required")
        return 2
    path = REGISTRY_DIR / model_id / "manifest.json"
    if action == "freeze":
        if model_id == "soccer-dc-legacy":
            from . import legacy_baseline
            legacy_baseline.freeze_cmd()
            return 0
        sealed = MF.freeze(build(model_id), path)
        print(f"froze {model_id} -> {path.relative_to(REPO_ROOT)}  sha256 {sealed['manifest_sha256']}")
        return 0
    if action == "verify":
        if model_id == "soccer-dc-legacy":
            from . import legacy_baseline
            return legacy_baseline.verify_cmd(golden=golden)
        rep = MF.verify(path, golden_fn=golden_check if golden else None)
        print(rep.render())
        return 0 if rep.ok else 1
    return 2


def manifest_sha(model_id):
    p = REGISTRY_DIR / model_id / "manifest.json"
    return MF.load(p)["manifest_sha256"] if p.exists() else "unfrozen"


__all__ = ["status_of", "load_index", "cli", "manifest_sha", "REPORTS_DIR"]
