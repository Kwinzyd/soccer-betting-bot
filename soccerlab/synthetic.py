"""Deterministic synthetic leagues with known generating strengths.

Used by the leakage tests and by registry golden cases (which must depend
only on code, not on repository data).
"""

import json
from datetime import datetime, timedelta, timezone

import numpy as np


def synth_league(seed=0, n_teams=12, seasons=(2022, 2023, 2024), promote=2, start=datetime(2022, 8, 6, tzinfo=timezone.utc)):
    """Synthetic league files: double round robin per season, Poisson goals
    from known strengths, `promote` teams replaced each season."""
    rng = np.random.default_rng(seed)
    next_id = 1000
    teams = list(range(1, n_teams + 1))
    strength = {t: rng.normal(0, 0.3) for t in teams}
    files, mid = {}, 1
    for si, season in enumerate(seasons):
        if si > 0:
            ranked = sorted(teams, key=lambda t: strength[t])
            for t in ranked[:promote]:
                teams.remove(t)
            for _ in range(promote):
                teams.append(next_id)
                strength[next_id] = rng.normal(-0.25, 0.15)
                next_id += 1
        fixtures = [(h, a) for h in teams for a in teams if h != a]
        order = rng.permutation(len(fixtures))
        t0 = start + timedelta(days=365 * si)
        rows = []
        for k, idx in enumerate(order):
            h, a = fixtures[idx]
            ko = t0 + timedelta(days=3 * (k // (n_teams // 2)), hours=int(k % 3) * 2 + 13)
            lh = np.exp(0.2 + 0.25 + strength[h] - strength[a] * 0.8)
            la = np.exp(0.2 + strength[a] - strength[h] * 0.8)
            rows.append({"id": mid, "season": season, "home_team_id": int(h), "away_team_id": int(a),
                         "date": ko.strftime("%Y-%m-%dT%H:%M:%S.000Z"), "status_state": "final",
                         "home_score": int(rng.poisson(lh)), "away_score": int(rng.poisson(la))})
            mid += 1
        files[f"syn_{season}_matches.json"] = rows
        files[f"syn_{season}_teams.json"] = [{"id": int(t), "name": f"Team {t}"} for t in teams]
    return files


def write_files(dirpath, files):
    dirpath.mkdir(parents=True, exist_ok=True)
    for name, rows in files.items():
        (dirpath / name).write_text(json.dumps(rows), encoding="utf-8")
    return dirpath
