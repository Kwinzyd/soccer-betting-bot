"""Look-ahead tests on synthetic leagues with known outcomes.

Two kinds of test live here:

* test_pit_*            the corrected PIT engine must be invariant to anything
                        that happens after its information cutoff.
* test_legacy_defect_*  executable evidence that the *legacy* code is NOT
                        invariant. These assert the defect exists; if one
                        starts failing, the frozen legacy baseline changed.
"""

import dataclasses
from datetime import timedelta

import numpy as np
import pytest

from soccerlab import dcpit, legacy, pit
from soccerlab.walkforward import walk_forward
from soccerlab.synthetic import synth_league, write_files

CFG = dcpit.DCConfig(min_matches=40)


def _load(d, **kw):
    return pit.load_league("syn", d, **kw)


def _mutate(ld, pred):
    """Swap home/away scores (and xG) for every match where pred(m)."""
    out = []
    for m in ld.matches:
        if m.final and pred(m):
            m = dataclasses.replace(m, hg=m.ag + 3, ag=m.hg,
                                    xgh=None if m.xgh is None else m.xga + 2.0,
                                    xga=None if m.xga is None else m.xgh)
        out.append(m)
    return ld.replace(out)


def _key(p):
    return (p.mid, p.policy)


def test_pit_future_results_do_not_change_past_predictions(synth_dir):
    ld = _load(synth_dir)
    base = walk_forward(ld, [2023, 2024], CFG)
    T = sorted(p.kickoff for p in base if p.season == 2023)[len(base) // 4]
    mutated = _mutate(ld, lambda m: m.kickoff > T)
    again = {_key(p): p for p in walk_forward(mutated, [2023, 2024], CFG)}
    unaffected = [p for p in base if p.info_cutoff <= T]
    affected = [p for p in base if p.info_cutoff > T + timedelta(hours=4)]
    assert unaffected and affected
    for p in unaffected:
        assert again[_key(p)].p == p.p, f"prediction {p.mid} changed after future results were altered"
    assert any(again[_key(p)].p != p.p for p in affected), "mutation had no effect; test is not sensitive"


def test_pit_future_promoted_cohort_cannot_alter_prior(synth_dir):
    ld = _load(synth_dir)
    cutoff = pit.parse_utc("2023-07-01T00:00:00Z")
    before = dcpit.promoted_prior(ld, 2023, cutoff)
    promoted_2024 = ld.participants(2024) - ld.teams_before_season(2024)
    # make 2024's promoted sides world-beaters
    boosted = []
    for m in ld.matches:
        if m.season == 2024 and m.final:
            if m.home in promoted_2024:
                m = dataclasses.replace(m, hg=6, ag=0)
            elif m.away in promoted_2024:
                m = dataclasses.replace(m, hg=0, ag=6)
        boosted.append(m)
    after = dcpit.promoted_prior(ld.replace(boosted), 2023, cutoff)
    assert before == after
    # and the 2024 prior, computed at 2024's season start, must not see 2024 either
    c24 = pit.parse_utc("2024-07-01T00:00:00Z")
    assert dcpit.promoted_prior(ld, 2024, c24) == dcpit.promoted_prior(ld.replace(boosted), 2024, c24)


def test_legacy_defect_promoted_prior_uses_future_cohorts(synth_dir, tmp_path):
    files = synth_league()
    with legacy.data_root(synth_dir) as mods:
        M = mods["dcmodel"]
        matches, _ = M.load_league("syn")
        idx = {t: i for i, t in enumerate(sorted({t for m in matches for t in (m["home"], m["away"])}))}
        p1 = M.estimate_promoted_prior(matches, idx, len(idx))
    promoted_2024 = {t["id"] for t in files["syn_2024_teams.json"]} - {
        t["id"] for s in (2022, 2023) for t in files[f"syn_{s}_teams.json"]}
    for r in files["syn_2024_matches.json"]:
        if r["home_team_id"] in promoted_2024:
            r["home_score"], r["away_score"] = 6, 0
        elif r["away_team_id"] in promoted_2024:
            r["home_score"], r["away_score"] = 0, 6
    d2 = write_files(tmp_path / "boosted", files)
    with legacy.data_root(d2) as mods:
        M = mods["dcmodel"]
        matches, _ = M.load_league("syn")
        p2 = M.estimate_promoted_prior(matches, idx, len(idx))
    # The single prior used for EVERY historical prediction moved because of
    # 2024 results -- including predictions made in 2023.
    assert abs(p1[0] - p2[0]) > 0.05


def test_legacy_defect_baselines_train_on_future_seasons(synth_dir, tmp_path):
    files = synth_league()
    with legacy.data_root(synth_dir) as mods:
        b1 = mods["backtest"].baselines("syn", [2023])
    for r in files["syn_2024_matches.json"]:
        r["home_score"], r["away_score"] = 0, 5          # 2024: all away wins
    d2 = write_files(tmp_path / "future", files)
    with legacy.data_root(d2) as mods:
        b2 = mods["backtest"].baselines("syn", [2023])
    assert not np.allclose(b1["rates"], b2["rates"]), "2023 baseline should (defectively) depend on 2024"


def test_pit_future_league_entrant_is_invisible(synth_dir, tmp_path):
    ld = _load(synth_dir)
    base = walk_forward(ld, [2023], CFG)
    files = synth_league()
    files["syn_2025_matches.json"] = [
        {"id": 99000 + i, "season": 2025, "home_team_id": 777, "away_team_id": t, "date": "2025-08-10T15:00:00.000Z",
         "status_state": "final", "home_score": 9, "away_score": 0}
        for i, t in enumerate(sorted({r["home_team_id"] for r in files["syn_2024_matches.json"]}))]
    ld2 = _load(write_files(tmp_path / "plus2025", files))
    again = {_key(p): p.p for p in walk_forward(ld2, [2023], CFG)}
    assert all(again[_key(p)] == p.p for p in base)


def test_pit_season_opener_promoted_flag(synth_dir):
    """At 2024's first cutoff, 2023's promoted sides are established and
    2024's newcomers are promoted -- decided by the fixture's season."""
    ld = _load(synth_dir)
    first_2024 = min(m.kickoff for m in ld.matches if m.season == 2024)
    f = dcpit.fit_pit(ld, 2024, first_2024 - timedelta(days=1), CFG)
    new_2023 = ld.participants(2023) - ld.teams_before_season(2023)
    new_2024 = ld.participants(2024) - ld.teams_before_season(2024)
    assert f.promoted == new_2024
    assert not (f.promoted & new_2023)


def test_legacy_defect_season_opener_keeps_last_seasons_promoted_flag(synth_dir):
    ld = _load(synth_dir)
    first_2024 = min(m.kickoff for m in ld.matches if m.season == 2024)
    new_2023 = ld.participants(2023) - ld.teams_before_season(2023)
    with legacy.data_root(synth_dir) as mods:
        M = mods["dcmodel"]
        matches, _ = M.load_league("syn")
        first = M.first_season_seen(matches)
        asof_season = max(m["season"] for m in matches if m["date"] < np.datetime64(first_2024.date()))
        flagged = {t for t in first if first[t] >= asof_season}
    assert new_2023 <= flagged, "legacy should (defectively) still treat 2023 newcomers as promoted"


def test_pit_post_cutoff_xg_revisions_are_excluded(tmp_path):
    files = synth_league()
    rng = np.random.default_rng(1)
    stats = []
    for s in (2022, 2023, 2024):
        rows = []
        for r in files[f"syn_{s}_matches.json"]:
            for side in ("home", "away"):
                rows.append({"match_id": r["id"], "team_id": r[f"{side}_team_id"],
                             "expected_goals": float(rng.gamma(2.0, 0.6))})
        files[f"syn_{s}_team_match_stats.json"] = rows
        stats.append(rows)
    d = write_files(tmp_path / "xg", files)
    ld = _load(d, xg_mode="provider_lag")
    cfg = dataclasses.replace(CFG, xg_alpha=0.3, min_matches_xg=40)
    cutoff = pit.parse_utc("2023-08-20T00:00:00Z")
    f1 = dcpit.fit_pit(ld, 2023, cutoff, cfg)
    # revise xG for every match whose xG was not yet available at the cutoff
    revised = ld.replace([dataclasses.replace(m, xgh=5.0, xga=0.1) if (m.xg_at and m.xg_at > cutoff) else m
                          for m in ld.matches])
    f2 = dcpit.fit_pit(revised, 2023, cutoff, cfg)
    assert f1.xg is not None
    assert np.array_equal(f1.xg["atk"], f2.xg["atk"]) and f1.xg["mu"] == f2.xg["mu"]
    # strict ingestion mode: xG pulled 2026-08-22 does not exist in 2023
    strict = _load(d, xg_mode="ingestion")
    assert dcpit.fit_pit(strict, 2023, cutoff, cfg).xg is None


def test_pit_result_not_visible_before_result_lag(synth_dir):
    ld = _load(synth_dir)
    m = [x for x in ld.matches if x.season == 2023][10]
    assert m not in ld.available(m.kickoff + timedelta(hours=2, minutes=59))
    assert m in ld.available(m.kickoff + timedelta(hours=3))


def test_pit_scheduled_zero_zero_is_not_a_result(tmp_path):
    files = synth_league(seasons=(2022,))
    files["syn_2022_matches.json"][0].update(status_state="scheduled", home_score=0, away_score=0)
    ld = _load(write_files(tmp_path / "s", files))
    m = ld.get(files["syn_2022_matches.json"][0]["id"])
    assert not m.final and m.hg is None
    assert m not in ld.available(pit.parse_utc("2030-01-01T00:00:00Z"))


def test_pit_deterministic_rerun(synth_dir):
    ld = _load(synth_dir)
    a = walk_forward(ld, [2023, 2024], CFG)
    b = walk_forward(_load(synth_dir), [2023, 2024], CFG)
    assert a == b


def test_pit_naive_timestamps_rejected(tmp_path):
    files = synth_league(seasons=(2022,))
    files["syn_2022_matches.json"][0]["date"] = "2022-08-06T15:00:00"
    from soccerlab.timeutil import NaiveTimestamp
    with pytest.raises(NaiveTimestamp):
        _load(write_files(tmp_path / "n", files))


def test_pit_duplicate_match_ids_rejected(tmp_path):
    files = synth_league(seasons=(2022, 2023))
    files["syn_2023_matches.json"][0]["id"] = files["syn_2022_matches.json"][0]["id"]
    with pytest.raises(pit.DataError):
        _load(write_files(tmp_path / "dup", files))


def test_pit_unknown_team_fails_closed(synth_dir):
    ld = _load(synth_dir)
    f = dcpit.fit_pit(ld, 2023, pit.parse_utc("2023-09-01T00:00:00Z"), CFG)
    with pytest.raises(dcpit.UnknownTeam):
        dcpit.predict(f, 424242, next(iter(f.index)), CFG)


def test_pit_predictions_never_use_info_after_decision(synth_dir):
    ld = _load(synth_dir)
    for p in walk_forward(ld, [2023, 2024], CFG):
        assert p.info_cutoff <= p.decision_time < p.kickoff
