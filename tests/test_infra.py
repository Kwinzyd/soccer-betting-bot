"""Store, risk engine, alerts, provider client, manifests, registry, live runs."""

import json
import os
import sqlite3
from datetime import timedelta

import numpy as np
import pytest

from soccerlab import alerts, dcpit, hashing, ledger, live, manifest as MF, provider, registry
from soccerlab.store import Store
from soccerlab.timeutil import iso, parse_utc

KO = parse_utc("2026-11-21T15:00:00Z")
T = parse_utc("2026-11-21T10:00:00Z")


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "s.sqlite3")
    yield s
    s.close()


def _pred(store, match="bdl:epl:1", ns="shadow"):
    return store.put_prediction(ns, "soccer-dc-pit-v1", "sha", match, "1x2", KO, T, T - timedelta(hours=1),
                                {"home": .5, "draw": .25, "away": .25}, "input")


def _cand(store, **kw):
    base = dict(namespace="shadow", model_id="soccer-dc-pit-v1", model_status="challenger",
                prediction_uid=_pred(store, kw.get("match_uid", "bdl:epl:1"), kw.get("namespace", "shadow")),
                match_uid="bdl:epl:1", league="epl", market="1x2", selection="home", vendor="dk", decimal_odds=2.3,
                quote_observed_at=T - timedelta(minutes=5), decision_time=T, kickoff=KO, model_prob=0.5,
                fair_prob=0.42)
    base.update(kw)
    return ledger.Candidate(**base)


# ------------------------------------------------------------------- store

def test_store_append_only(store):
    uid = _pred(store)
    with pytest.raises(sqlite3.IntegrityError):
        store.db.execute("UPDATE predictions SET probs_json='{}' WHERE prediction_uid=?", (uid,))
    with pytest.raises(sqlite3.IntegrityError):
        store.db.execute("DELETE FROM predictions")


def test_store_rejects_prediction_after_kickoff(store):
    with pytest.raises(sqlite3.IntegrityError):
        store.put_prediction("shadow", "m", "sha", "bdl:epl:2", "1x2", KO, KO + timedelta(minutes=1), T, {}, "x")


def test_store_raw_and_quote_ingestion_idempotent(store):
    a = store.put_raw("bdl", "odds", {"m": [1]}, b'{"data":[]}', 200)
    b = store.put_raw("bdl", "odds", {"m": [1]}, b'{"data":[]}', 200)
    assert a == b
    ev = [{"match_uid": "bdl:epl:1", "market": "1x2", "selection": "home", "vendor": "dk", "decimal_odds": 2.1,
           "status": "available", "observed_at": T, "raw_ref": a}]
    assert store.put_quote_events(ev) == 1 and store.put_quote_events(ev) == 0
    later = [dict(ev[0], observed_at=T + timedelta(hours=1), decimal_odds=None, status="suspended")]
    store.put_quote_events(later)
    assert store.quotes_asof("bdl:epl:1", "1x2", T)[0]["decimal_odds"] == 2.1
    assert store.quotes_asof("bdl:epl:1", "1x2", T + timedelta(hours=2))[0]["status"] == "suspended"


def test_store_outcome_corrections_kept(store):
    store.put_outcome("bdl:epl:1", T, "STATUS_FULL_TIME", "final", iso(KO), 2, 1, "feed")
    store.put_outcome("bdl:epl:1", T + timedelta(hours=1), "STATUS_FULL_TIME", "final", iso(KO), 2, 1, "feed")
    store.put_outcome("bdl:epl:1", T + timedelta(hours=2), "STATUS_FULL_TIME", "final", iso(KO), 2, 2, "feed")
    h = store.outcome_history("bdl:epl:1")
    assert [(r["home_score"], r["away_score"]) for r in h] == [(2, 1), (2, 2)]


def test_store_refuses_production_checkout(tmp_path, monkeypatch):
    prod = tmp_path / "prod"
    (prod / "odds_logger").mkdir(parents=True)
    (prod / "odds_logger" / "seen.json").write_text("{}")
    with pytest.raises(PermissionError):
        Store(prod / "odds_logger" / "x.sqlite3")


def test_store_backup(store, tmp_path):
    _pred(store)
    out = store.backup(tmp_path / "bk.sqlite3")
    assert sqlite3.connect(out).execute("SELECT count(*) FROM predictions").fetchone()[0] == 1


# ------------------------------------------------------------- risk engine

def test_entry_recorded_once_and_opposing_selection_rejected(store):
    eng = ledger.RiskEngine(store)
    uid, reasons = eng.consider(_cand(store))
    assert uid and not reasons
    _, r2 = eng.consider(_cand(store))
    assert any("duplicate" in r for r in r2)
    _, r3 = eng.consider(_cand(store, selection="away", decimal_odds=5.0, model_prob=0.3))
    assert any("opposing" in r for r in r3)
    assert len(store.entries("shadow")) == 1
    acts = [r[0] for r in store.db.execute("SELECT action FROM decisions ORDER BY id")]
    assert acts == ["bet", "no_bet", "no_bet"]          # no-bet decisions are logged too


@pytest.mark.parametrize("kw,needle", [
    (dict(quote_observed_at=T - timedelta(hours=2)), "stale quote"),
    (dict(quote_observed_at=KO + timedelta(minutes=1)), "at/after kickoff"),
    (dict(quote_observed_at=T + timedelta(minutes=1)), "from the future"),
    (dict(decision_time=KO - timedelta(minutes=1), quote_observed_at=KO - timedelta(minutes=2)), "too close"),
    (dict(model_ok=False), "did not converge"),
    (dict(data_flags=["results feed stale"]), "data: results feed stale"),
    (dict(decimal_odds=1.0), "invalid price"),
    (dict(decimal_odds=1.9), "EV"),
    (dict(namespace="paper"), "not promoted"),
])
def test_risk_rejections(store, kw, needle):
    if kw.get("namespace") == "paper":
        kw["prediction_uid"] = _pred(store, ns="paper")
    _, reasons = ledger.RiskEngine(store).consider(_cand(store, **kw))
    assert any(needle in r for r in reasons), reasons
    assert store.entries(kw.get("namespace", "shadow")) == []


def test_kill_switch(store, monkeypatch):
    monkeypatch.setenv("SOCCERLAB_PAPER_KILL", "1")
    _, reasons = ledger.RiskEngine(store).consider(_cand(store))
    assert "kill switch engaged" in reasons


def test_exposure_caps(store):
    eng = ledger.RiskEngine(store, ledger.RiskLimits(max_daily_units=2, max_league_units=10))
    got = [eng.consider(_cand(store, match_uid=f"bdl:epl:{i}"))[0] for i in range(4)]
    assert sum(g is not None for g in got) == 2
    eng2 = ledger.RiskEngine(store, ledger.RiskLimits(max_daily_units=99, max_vendor_units=3))
    got2 = [eng2.consider(_cand(store, match_uid=f"bdl:laliga:{i}", league="laliga"))[0] for i in range(4)]
    assert sum(g is not None for g in got2) == 1           # 2 dk units already open + 1


def test_settlement_and_performance(store):
    eng = ledger.RiskEngine(store)
    for i, sel in enumerate(("home", "draw", "away")):
        eng.consider(_cand(store, match_uid=f"bdl:epl:{i}", selection=sel, decimal_odds=3.0, model_prob=0.4))
    res = {"bdl:epl:0": ("settled", "win", ""), "bdl:epl:1": ("void", "void", "postponed"),
           "bdl:epl:2": ("pending", None, "")}
    ledger.settle_entries(store, "shadow", lambda e: res[e["match_uid"]], "test")
    assert ledger.settle_entries(store, "shadow", lambda e: res[e["match_uid"]], "test") == 0   # idempotent
    p = ledger.performance(store, "shadow")
    assert p["settled"] == 1 and p["void"] == 1 and p["open_units"] == 1.0
    assert p["realised_profit"] == pytest.approx(2.0) and p["realised_roi"] == pytest.approx(2.0)
    # a correction supersedes rather than overwrites
    res["bdl:epl:0"] = ("settled", "loss", "score corrected")
    ledger.settle_entries(store, "shadow", lambda e: res[e["match_uid"]], "test")
    assert ledger.performance(store, "shadow")["realised_profit"] == pytest.approx(-1.0)
    hist = store.db.execute("SELECT outcome, supersedes FROM settlements WHERE entry_uid IN "
                            "(SELECT entry_uid FROM paper_entries WHERE match_uid='bdl:epl:0') ORDER BY id").fetchall()
    assert [h[0] for h in hist] == ["win", "loss"] and hist[1][1] is not None


# ------------------------------------------------------------------ alerts

def _entry(store):
    uid, _ = ledger.RiskEngine(store).consider(_cand(store))
    return store.entries("shadow")[0]


def test_shadow_never_alerts(store):
    a = alerts.Alerter(store, alerts.RecordingTransport())
    with pytest.raises(alerts.AlertRefused):
        a.send("shadow", "k", "x")
    with pytest.raises(alerts.AlertRefused):
        a.selection(_entry(store), "challenger", {}, {})
    assert a.transport.sent == []


def test_selection_alert_dedup_and_fields(store):
    t = alerts.RecordingTransport()
    a = alerts.Alerter(store, t)
    e = _entry(store)
    mkt = {"fixture": "A v B", "devig": "power", "fair_prob": 0.42, "quote_age_min": 5}
    assert a.selection(e, "promoted", {}, mkt) == "sent"
    assert a.selection(e, "promoted", {}, mkt) == "duplicate"
    assert len(t.sent) == 1
    body = t.sent[0]
    for needle in ("PAPER ONLY", "A v B", "HOME", "Kickoff", "Predicted", "2.30", "dk", "soccer-dc-pit-v1",
                   "implied", "de-vigged", "EV", "rule", "freshness"):
        assert needle in body, needle


def test_ops_alerts_dedup_per_day(store):
    t = alerts.RecordingTransport()
    a = alerts.Alerter(store, t)
    assert a.ops("auth_failure", "balldontlie", "HTTP 401", day="2026-11-19") == "sent"
    assert a.ops("auth_failure", "balldontlie", "HTTP 401", day="2026-11-19") == "duplicate"
    assert a.ops("auth_failure", "balldontlie", "HTTP 401", day="2026-11-20") == "sent"
    with pytest.raises(ValueError):
        a.ops("not_a_kind", "x", "y")


def test_default_transport_is_null(monkeypatch):
    monkeypatch.delenv("SOCCERLAB_ALERTS", raising=False)
    assert isinstance(alerts.from_env(), alerts.NullTransport)
    monkeypatch.setenv("SOCCERLAB_ALERTS", "1")
    assert isinstance(alerts.from_env(), alerts.NullTransport)       # no creds -> still null


def test_legacy_telegram_module_never_sends_in_tests(monkeypatch):
    import importlib
    tg = importlib.import_module("telegram_alerts")
    monkeypatch.setattr(tg, "CONFIG_PATH", "does-not-exist.json")
    assert tg.send_message("x") is False                              # no creds -> refuses before network


# ---------------------------------------------------------------- provider

class FakeTransport:
    def __init__(self, responses):
        self.responses, self.calls = list(responses), []

    def __call__(self, url, headers):
        self.calls.append(url)
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def _ok(data, cursor=None):
    return 200, json.dumps({"data": data, "meta": {"next_cursor": cursor}}).encode()


def _client(responses, **kw):
    t = FakeTransport(responses)
    return provider.Client("k", transport=t, sleep=lambda s: None, **kw), t


def test_provider_pagination_and_versions():
    c, t = _client([_ok([1, 2], cursor=5), _ok([3])])
    assert c.paginate("epl", "matches", {"season": 2026}) == [1, 2, 3]
    assert "/epl/v2/matches" in t.calls[0] and "cursor=5" in t.calls[1]
    assert "/laliga/v1/odds" in provider.url_for("laliga", "odds", {})
    with pytest.raises(ValueError):
        provider.url_for("bundesliga", "odds", {})


def test_provider_retries_then_succeeds_and_gives_up():
    c, _ = _client([(429, b""), (503, b""), TimeoutError(), _ok([1])])
    assert c.get("u")["data"] == [1]
    c2, t2 = _client([(429, b"")] * 5, max_retries=2)
    with pytest.raises(provider.ProviderError):
        c2.get("u")
    assert len(t2.calls) == 3


@pytest.mark.parametrize("resp,exc", [((401, b"Unauthorized"), provider.AuthError),
                                      ((404, b"nope"), provider.ProviderError),
                                      ((200, b"<html>"), provider.IncompleteResponse),
                                      ((200, b'{"error": 1}'), provider.IncompleteResponse)])
def test_provider_explicit_failures(resp, exc):
    c, _ = _client([resp])
    with pytest.raises(exc):
        c.get("u")


def test_provider_repeated_cursor_detected():
    c, _ = _client([_ok([1], cursor=7), _ok([2], cursor=7)])
    with pytest.raises(provider.IncompleteResponse):
        c.paginate("epl", "odds", {})


def test_provider_requires_key():
    with pytest.raises(provider.AuthError):
        provider.Client("")


# --------------------------------------------------------------- manifests

def test_manifest_seal_detects_edit(tmp_path):
    m = MF.seal({"schema": MF.SCHEMA, "model_id": "x", "git": {"commit": "HEAD"}, "code_files": [],
                 "data_files": []})
    p = tmp_path / "m.json"
    p.write_text(json.dumps(m))
    assert MF.verify(p, check_history=False, check_worktree=False).ok
    m["model_id"] = "y"
    p.write_text(json.dumps(m))
    assert not MF.verify(p, check_history=False, check_worktree=False).ok


def test_manifest_freeze_refuses_overwrite(tmp_path):
    p = tmp_path / "m.json"
    MF.freeze({"schema": MF.SCHEMA}, p)
    with pytest.raises(FileExistsError):
        MF.freeze({"schema": MF.SCHEMA}, p)


def test_manifest_detects_changed_protected_file(tmp_path):
    f = tmp_path / "code.py"
    f.write_bytes(b"x = 1\n")
    entry = MF.file_entry("code.py", f.read_bytes())
    m = MF.seal({"schema": MF.SCHEMA, "git": {"commit": "HEAD"}, "code_files": [entry], "data_files": []})
    p = tmp_path / "m.json"
    p.write_text(json.dumps(m))
    assert MF.verify(p, repo_root=tmp_path, check_history=False).ok
    f.write_bytes(b"x = 2\n")
    rep = MF.verify(p, repo_root=tmp_path, check_history=False)
    assert not rep.ok and "protected file changed" in rep.errors[0]


def test_legacy_baseline_manifest_verifies():
    rep = MF.verify(registry.REGISTRY_DIR / "soccer-dc-legacy" / "manifest.json")
    assert rep.ok, rep.render()


def test_registry_has_no_promoted_model():
    idx = registry.load_index()
    assert idx["promoted"] == []
    assert all(m["status"] != "promoted" for m in idx["models"])
    assert registry.status_of("nope") == "unregistered"


def test_atomic_write_leaves_old_file_on_failure(tmp_path, monkeypatch):
    p = tmp_path / "f.json"
    hashing.atomic_write_json(p, {"a": 1})
    real = os.replace

    def boom(src, dst):
        raise OSError("disk full")
    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        hashing.atomic_write_json(p, {"a": 2})
    monkeypatch.setattr(os, "replace", real)
    assert json.loads(p.read_text()) == {"a": 1}
    assert [x.name for x in tmp_path.iterdir()] == ["f.json"]       # temp file cleaned up


# ------------------------------------------------------------- live runs

def _poll(tmp_path, observed, rows):
    p = tmp_path / "poll.json"
    p.write_text(json.dumps({"observed_at": observed, "leagues": {"syn": rows}}))
    return p


def _sched(tmp_path):
    from soccerlab.synthetic import synth_league, write_files
    files = synth_league(seasons=(2025, 2026), start=parse_utc("2025-08-02T00:00:00Z"))
    for r in files["syn_2026_matches.json"][60:]:
        r.update(status_state="scheduled", home_score=0, away_score=0)
    return write_files(tmp_path / "sched", files), files["syn_2026_matches.json"][60:66]


def test_live_shadow_fail_closed_and_paper_refused(tmp_path, monkeypatch, store):
    sdir, upcoming = _sched(tmp_path)
    first_ko = parse_utc(upcoming[0]["date"])
    observed = iso(first_ko - timedelta(hours=20))
    rows = []
    for m in upcoming:
        rows += [{"match_id": m["id"], "vendor": "dk", "moneyline_home_odds": 150, "moneyline_draw_odds": 230,
                  "moneyline_away_odds": 190, "updated_at": observed},
                 {"match_id": m["id"], "vendor": "fd", "moneyline_home_odds": -100000,
                  "moneyline_draw_odds": 25000, "moneyline_away_odds": 35000, "updated_at": observed}]
    poll = _poll(tmp_path, observed, rows)
    monkeypatch.setitem(registry.SPECS["soccer-dc-pit-v1"], "config", dcpit.DCConfig(min_matches=40))
    assert live.main("paper", poll, model_id="soccer-dc-pit-v1", replay=True, schedule_dir=sdir, store=store) == 3
    assert live.main("shadow", poll, replay=False, schedule_dir=sdir, store=store,
                     now=parse_utc(observed) + timedelta(hours=1)) == 4
    assert live.main("shadow", poll, replay=True, schedule_dir=sdir, store=store) == 0
    preds = store.db.execute("SELECT namespace, count(*) FROM predictions GROUP BY namespace").fetchall()
    assert [tuple(r) for r in preds] == [("shadow", len(upcoming))]
    assert store.entries("paper") == []
    assert all(e["vendor"] == "dk" for e in store.entries("shadow"))          # degenerate fd quotes unusable
    assert store.db.execute("SELECT count(*) FROM quotes WHERE status='invalid'").fetchone()[0] == 3 * len(upcoming)
    # rerun: no duplicate predictions/entries
    n_e = len(store.entries("shadow"))
    live.main("shadow", poll, replay=True, schedule_dir=sdir, store=store)
    assert len(store.entries("shadow")) == n_e
    assert store.db.execute("SELECT count(*) FROM predictions").fetchone()[0] == len(upcoming)
    assert store.db.execute("SELECT count(*) FROM alerts_sent").fetchone()[0] == 0


def test_live_stale_results_feed_blocks_entries(tmp_path, monkeypatch, store):
    sdir, upcoming = _sched(tmp_path)
    late = parse_utc(upcoming[2]["date"]) + timedelta(hours=7)        # earlier fixtures still 'scheduled'
    later = [m for m in json.loads((sdir / "syn_2026_matches.json").read_text()) if parse_utc(m["date"]) > late][:2]
    observed = iso(late)
    rows = [{"match_id": m["id"], "vendor": "dk", "moneyline_home_odds": 300, "moneyline_draw_odds": 300,
             "moneyline_away_odds": -150, "updated_at": observed} for m in later]
    monkeypatch.setitem(registry.SPECS["soccer-dc-pit-v1"], "config", dcpit.DCConfig(min_matches=40))
    live.main("shadow", _poll(tmp_path, observed, rows), replay=True, schedule_dir=sdir, store=store)
    assert store.entries("shadow") == []
    reasons = [json.loads(r[0]) for r in store.db.execute("SELECT reasons_json FROM decisions")]
    assert len(reasons) == len(later)
    assert all(any("unresulted" in x for x in rs) for rs in reasons)


def test_score_matrix_truncation_and_dc_coherence():
    M = __import__("soccerlab.legacy", fromlist=["x"]).modules()["dcmodel"]
    for lh, la, rho in [(0.3, 0.2, -0.1), (1.5, 1.2, -0.05), (3.5, 0.4, 0.05), (4.5, 3.8, 0.0)]:
        S = M.score_matrix(lh, la, rho)
        assert S.min() >= 0 and S.sum() == pytest.approx(1.0)
        # Truncation at 0..11 goals then renormalising distorts market
        # probabilities by < 1e-4 for rates <= 3.5 and < 1e-3 even at 4.5.
        wide = M.markets_from_matrix(M.score_matrix(lh, la, rho, max_goals=40))
        mk = M.markets_from_matrix(S)
        tol = 1e-4 if max(lh, la) <= 3.5 else 1e-3
        assert max(abs(mk[k] - wide[k]) for k in ("home", "draw", "away", "over25", "btts_yes")) < tol
        assert mk["home"] + mk["draw"] + mk["away"] == pytest.approx(1.0)
        assert 0 <= mk["over25"] <= 1 and 0 <= mk["btts_yes"] <= 1
    # untruncated independent Poisson vs matrix: 1X2 agrees to 1e-6 at rho=0
    S = M.score_matrix(1.4, 1.1, 0.0, max_goals=40)
    S12 = M.score_matrix(1.4, 1.1, 0.0)
    assert abs(M.markets_from_matrix(S)["home"] - M.markets_from_matrix(S12)["home"]) < 1e-6


def test_legacy_gradient_matches_numerical():
    from soccerlab.legacy_baseline import gradient_check
    for seed in range(3):
        assert gradient_check(seed=seed) < 1e-6


def test_fit_converges_and_recovers_strength_order(synth_dir):
    from soccerlab import pit
    ld = pit.load_league("syn", synth_dir)
    f = dcpit.fit_pit(ld, 2024, parse_utc("2025-07-01T00:00:00Z"), dcpit.DCConfig(min_matches=40))
    assert f.converged
    assert np.isfinite(f.atk).all() and abs(f.atk.mean()) < 1e-6
    assert -0.25 <= f.rho <= 0.25
