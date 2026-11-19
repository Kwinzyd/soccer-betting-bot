"""EXP-2026-11-B pilot-readiness tests: scheduling, auth recovery, rate-limit
contention, venue discrepancies, closing-reference mismatches, incomplete
fixtures, pilot exclusion and the entry-time-only selection function."""

import json
from datetime import timedelta

import pytest

from soccerlab import provider
from soccerlab.expb import closing, config as C, evaluate, logger as L, monitor
from soccerlab.expb.db import DB
from soccerlab.timeutil import iso, parse_utc
from tests.test_expb import KO, T0, Clock, FakeAPI, book, match

LOCK = T0 - timedelta(days=3)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "LEAGUES", ("epl",))
    clock = Clock(T0)
    api = FakeAPI(clock)
    api.schedules["epl"] = [match(1, KO), match(2, KO), match(3, KO + timedelta(hours=2))]
    for m in (1, 2, 3):
        api.odds[("epl", m)] = [book(m, "draftkings"), book(m, "fanduel", 155, 235, 185)]
    db = DB(tmp_path / "x.sqlite3")
    sleeps = []
    client = provider.Client("test-key", transport=api, sleep=sleeps.append)
    tick = {"t": 0.0}
    runner = L.Runner(db, client, now_fn=clock, sleep=sleeps.append, monotonic=lambda: tick["t"])
    yield clock, api, db, runner, sleeps, tick
    db.close()


def kinds(res):
    return [(a["kind"], a["result"].split(":")[0]) for a in res["actions"]]


def locked(**kw):
    p = dict(evaluate.load_protocol(), status="locked", locked_at=iso(LOCK), n_boot=200,
             approved_entry_venues=["draftkings", "fanduel"],
             venue_verification={"draftkings": {"status": "verified"}, "fanduel": {"status": "verified"}})
    p.update(kw)
    return p


# ------------------------------------------------------------ scheduling

@pytest.mark.parametrize("mins,expect", [(76, []), (75, ["T-60"]), (60, ["T-60"]), (45, ["T-60"]), (44, []),
                                         (26, []), (25, ["T-15"]), (15, ["T-15"]), (5, ["T-15"]), (4, [])])
def test_target_window_boundaries(env, mins, expect):
    clock, api, db, runner, *_ = env
    runner.run_once()                                                   # schedule
    clock.t = KO - timedelta(minutes=mins)
    got = [k for k, _ in kinds(runner.run_once()) if k.startswith("T-")]
    assert got == expect


def test_priority_and_budget_skip(env):
    clock, api, db, runner, sleeps, tick = env
    runner.run_once()
    clock.t = KO - timedelta(minutes=14)                                # fixtures 1,2 at T-15; fixture 3 at T-134
    runner.budget = 30                                                  # room for ~2 requests at 13s spacing
    orig = runner._get

    def slow(url):
        tick["t"] += 13.0
        return orig(url)
    runner._get = slow
    res = runner.run_once()
    ks = kinds(res)
    assert ks[0] == ("T-15", "ok")                                      # highest priority runs first
    assert any(r == "skipped" for _, r in ks)                           # lower priority deferred, not dropped
    st = {s["fixture_uid"] for s in db.c.execute("SELECT fixture_uid FROM targets WHERE status='captured'")}
    assert st == {"bdl:epl:1", "bdl:epl:2"}                             # one batched request for both fixtures


def test_postponed_fixture_is_not_targeted(env):
    clock, api, db, runner, *_ = env
    api.schedules["epl"] = [match(1, KO, state="post", status="STATUS_POSTPONED")]
    runner.run_once()
    clock.t = KO - timedelta(minutes=60)
    assert not [k for k, _ in kinds(runner.run_once()) if k != "schedule"]     # no snapshot, no odds poll


# ------------------------------------------------------------ auth recovery

def test_auth_failure_then_recovery(env):
    clock, api, db, runner, *_ = env
    runner.run_once()
    clock.t += timedelta(hours=3)
    api.fail["odds"] = [401]
    assert runner.run_once().get("auth_failure")
    level, checks = monitor.check(db, now=clock.t)
    assert level == "red" and any(c[1].startswith("poll failures") and c[0] == "red" for c in checks)
    clock.t += timedelta(minutes=5)                                     # key fixed: next invocation succeeds
    res = runner.run_once()
    assert ("regular", "ok") in kinds(res) and not res.get("auth_failure")
    level, checks = monitor.check(db, now=clock.t + timedelta(hours=25))
    assert not any(c[1].startswith("poll failures") and c[0] == "red" for c in checks)   # auth memory is 24h


# ------------------------------------------------------- rate-limit contention

def test_rate_limit_backoff_then_success(env):
    clock, api, db, runner, sleeps, _ = env
    runner.run_once()
    clock.t += timedelta(hours=3)
    api.fail["odds"] = [429, 429]                                       # e.g. the legacy logger used the window
    sleeps.clear()
    assert ("regular", "ok") in kinds(runner.run_once())
    assert sleeps[:2] == [5, 15]                                        # bounded backoff from provider.Client


def test_rate_limit_persistent_records_failure_without_quotes(env):
    clock, api, db, runner, *_ = env
    runner.run_once()
    clock.t += timedelta(hours=3)
    api.fail["odds"] = [429] * 5                                        # retries exhausted (4 + 1)
    assert ("regular", "failed") in kinds(runner.run_once())
    assert db.c.execute("SELECT count(*) FROM quotes").fetchone()[0] == 0
    clock.t += timedelta(minutes=5)                                     # regular poll retried next invocation
    assert ("regular", "ok") in kinds(runner.run_once())


def test_requests_are_spaced(env):
    clock, api, db, runner, sleeps, _ = env
    runner.run_once()
    sleeps.clear()
    clock.t = KO - timedelta(minutes=14)
    runner.run_once()                                                   # T-15 + regular -> >= 2 requests
    assert sleeps.count(C.SPACING_SECONDS) >= 1


# -------------------------------------------------------- venue discrepancies

def test_cross_venue_discrepancy_flag_and_exclusion(env):
    clock, api, db, runner, *_ = env
    runner.run_once()
    api.odds[("epl", 1)] = [book(1, "draftkings", 150, 240, 190), book(1, "fanduel", 300, 240, -110)]
    clock.t += timedelta(minutes=5)
    runner.run_once()
    fl = [json.loads(r[0]) for r in db.c.execute(
        "SELECT flags FROM quotes WHERE fixture_uid='bdl:epl:1' AND selection='home' AND poll_id=("
        "SELECT poll_id FROM quotes ORDER BY id DESC LIMIT 1) AND venue_class='sportsbook'")]
    assert len(fl) == 2
    assert all("cross_venue_discrepancy" in f for f in fl)
    draw = [json.loads(r[0]) for r in db.c.execute(
        "SELECT flags FROM quotes WHERE fixture_uid='bdl:epl:1' AND selection='draw' AND venue_class='sportsbook'")]
    assert not any("cross_venue_discrepancy" in f for f in draw)
    books = evaluate.books_at(db, "bdl:epl:1", ["draftkings", "fanduel"], T0, clock.t + timedelta(hours=1), LOCK)
    assert books == {}                                                  # flagged books never enter analyses


# ------------------------------------------------- evaluation-level fixtures

def put_fixture(db, mid, ko, status="STATUS_FULL_TIME", state="final", hs=1, as_=0):
    db.put_fixture({"fixture_uid": f"bdl:epl:{mid}", "league": "epl", "provider_match_id": mid, "season": 2026,
                    "home_team_id": 10 + mid, "away_team_id": 50 + mid, "kickoff": iso(ko), "status": status,
                    "status_state": state, "home_score": hs, "away_score": as_}, "p", ko - timedelta(days=5))


def put_book(db, mid, venue, at, prices, ko, flags=(), poll=None, statuses=None):
    poll = poll or f"{venue}-{iso(at)}"
    db.put_quotes([{"poll_id": poll, "snapshot_kind": "regular", "fixture_uid": f"bdl:epl:{mid}", "league": "epl",
                    "provider_match_id": mid, "venue": venue, "venue_class": "sportsbook",
                    "eligibility": "observational", "market": C.MARKET, "selection": s,
                    "status": (statuses or {}).get(s, "available"), "american_odds": None,
                    "decimal_odds": None if (statuses or {}).get(s, "available") != "available" else prices[i],
                    "source_updated_at": iso(at), "retrieved_at": iso(at), "kickoff_at_retrieval": iso(ko),
                    "minutes_to_kickoff": (ko - at).total_seconds() / 60, "flags": list(flags), "raw_ref": None}
                   for i, s in enumerate(C.SELECTIONS)])


def ext_close(db, mid, px):
    with db.tx() as c:
        c.execute("INSERT INTO closing_refs (fixture_uid, source, venue, market, home, draw, away, source_file_sha256,"
                  " imported_at) VALUES (?,?,?,?,?,?,?,?,?)", (f"bdl:epl:{mid}", "t", "betfair_exchange", C.MARKET,
                                                                *px, "x", "now"))


def full_fixture(db, mid, ko, entry=(2.5, 3.4, 2.9), close=(2.4, 3.4, 3.0), ext=(2.45, 3.5, 3.1)):
    put_fixture(db, mid, ko)
    put_book(db, mid, "draftkings", ko - timedelta(hours=25), entry, ko)
    put_book(db, mid, "fanduel", ko - timedelta(hours=25), entry, ko)
    put_book(db, mid, "draftkings", ko - timedelta(minutes=15), close, ko)
    if ext:
        ext_close(db, mid, ext)


def test_incomplete_and_void_fixtures_are_excluded_with_reasons(tmp_path):
    db = DB(tmp_path / "e.sqlite3")
    base = T0
    full_fixture(db, 1, base)                                           # usable
    put_fixture(db, 2, base + timedelta(hours=1))                       # partial book only at entry
    put_book(db, 2, "draftkings", base - timedelta(hours=24), (2.5, 3.4, 2.9), base + timedelta(hours=1),
             statuses={"draw": "suspended"})
    put_fixture(db, 3, base + timedelta(hours=2), status="STATUS_POSTPONED", state="post")
    put_fixture(db, 4, base + timedelta(days=4))                        # kickoff moved 4 days after entry quote
    put_book(db, 4, "draftkings", base + timedelta(days=4) - timedelta(hours=24), (2.5, 3.4, 2.9), base)
    put_fixture(db, 5, base + timedelta(hours=3))                       # entry ok, no closing reference at all
    put_book(db, 5, "fanduel", base + timedelta(hours=3) - timedelta(hours=25), (2.5, 3.4, 2.9),
             base + timedelta(hours=3))
    out = evaluate.closing_line(db, locked(), now=base + timedelta(days=6))
    ex = out["exclusions"]
    assert ex["no_entry_snapshot"] == 1 and ex["void_status"] == 1 and ex["void_kickoff_moved"] == 1
    assert ex["no_closing_reference"] == 1
    assert out["strong (primary)"]["n"] == 1 and out["entries"] == 2
    db.close()


def test_pilot_quotes_are_never_used(tmp_path):
    db = DB(tmp_path / "p.sqlite3")
    ko = LOCK + timedelta(hours=26)
    put_fixture(db, 1, ko)
    put_book(db, 1, "draftkings", LOCK - timedelta(minutes=30), (2.5, 3.4, 2.9), ko)     # pilot, inside tolerance
    ext_close(db, 1, (2.45, 3.5, 3.1))
    out = evaluate.closing_line(db, locked(), now=ko + timedelta(days=1))
    assert out["entries"] == 0 and out["exclusions"]["no_entry_snapshot"] == 1
    db.close()


def test_closing_reference_mismatch_is_quarantined(tmp_path):
    db = DB(tmp_path / "m.sqlite3")
    full_fixture(db, 1, T0, close=(2.4, 3.4, 3.0), ext=(5.0, 4.0, 1.7))   # external close says the opposite
    out = evaluate.closing_line(db, locked(), now=T0 + timedelta(days=1))
    assert out["exclusions"]["closing_reference_mismatch"] == 1
    assert out["strong (primary)"]["n"] == 0 and out["weak (EXP-2026-11-B-weak)"]["n"] == 1
    db.close()


def test_closing_import_quarantines_score_mismatch_and_unmapped(tmp_path, monkeypatch):
    db = DB(tmp_path / "c.sqlite3")
    put_fixture(db, 1, parse_utc("2026-11-21T15:00:00Z"), hs=2, as_=1)
    al = tmp_path / "al.json"
    al.write_text(json.dumps({"aliases": {"epl": {"Home One": 11, "Away One": 51}}}))
    monkeypatch.setattr(closing, "ALIASES", al)
    csvp = tmp_path / "E0.csv"
    csvp.write_text("Div,Date,Time,HomeTeam,AwayTeam,FTHG,FTAG,BFECH,BFECD,BFECA\n"
                    "E0,21/11/2026,15:00,Home One,Away One,0,0,2.5,3.4,3.1\n"        # wrong score -> wrong join
                    "E0,21/11/2026,15:00,Ghost FC,Away One,2,1,2.5,3.4,3.1\n", encoding="utf-8")
    rep, unmapped = closing.import_closing(db, csvp, "epl")
    assert rep == {"score_mismatch": 1, "unmapped_team": 1} and unmapped == {"Ghost FC": 1}
    assert db.c.execute("SELECT count(*) FROM closing_refs").fetchone()[0] == 0
    q = sorted(r[0] for r in db.c.execute("SELECT reason FROM closing_quarantine"))
    assert q == ["score_mismatch", "unmapped_team"]
    closing.import_closing(db, csvp, "epl")                             # idempotent re-import
    assert db.c.execute("SELECT count(*) FROM closing_quarantine").fetchone()[0] == 2
    db.close()


# ------------------------------------------------------- selection function

def test_select_entry_uses_only_entry_books_and_is_deterministic():
    books = {"draftkings": {"home": 2.5, "draw": 3.4, "away": 2.9}, "fanduel": {"home": 2.6, "draw": 3.3, "away": 2.8}}
    a = evaluate.select_entry(books)
    assert a == evaluate.select_entry(dict(reversed(list(books.items()))))
    assert a["selection"] == "home" and a["price"] == 2.6 and a["venue"] == "fanduel"
    assert evaluate.select_entry(books, threshold=0.5) is None
    tie = {"x": {"home": 3.0, "draw": 3.0, "away": 3.0}}
    assert evaluate.select_entry(tie)["selection"] == "home"


def test_entry_unchanged_by_later_quotes(tmp_path):
    db = DB(tmp_path / "s.sqlite3")
    full_fixture(db, 1, T0)
    before = evaluate._entries(db, locked(), T0 + timedelta(days=1))[0][0]
    put_book(db, 1, "fanduel", T0 - timedelta(hours=10), (9.0, 9.0, 1.1), T0)           # after entry time
    after = evaluate._entries(db, locked(), T0 + timedelta(days=1))[0][0]
    assert (before["selection"], before["price"]) == (after["selection"], after["price"])
    db.close()


def test_guard_requires_lock_and_verified_venues(tmp_path):
    db = DB(tmp_path / "g.sqlite3")
    with pytest.raises(evaluate.ProtocolNotLocked):
        evaluate.closing_line(db)                                       # repository protocol is a draft
    p = locked(venue_verification={"draftkings": {"status": "unverified"}, "fanduel": {"status": "verified"}})
    for fn in (evaluate.closing_line, evaluate.price_drift, evaluate.simulated_roi):
        with pytest.raises(evaluate.ProtocolNotLocked):
            fn(db, p)
    with pytest.raises(evaluate.ProtocolNotLocked):
        evaluate.closing_line(db, locked(approved_entry_venues=[]))
    db.close()


def test_three_analyses_are_separate(tmp_path):
    db = DB(tmp_path / "a.sqlite3")
    full_fixture(db, 1, T0)
    p = locked()
    now = T0 + timedelta(days=1)
    cl, pd_, roi = evaluate.closing_line(db, p, now), evaluate.price_drift(db, p, now), evaluate.simulated_roi(db, p, now)
    assert cl["analysis"] == "closing_line" and "roi" not in cl
    assert pd_["analysis"] == "price_drift" and pd_["n"] == 3          # draftkings early vs late, 3 selections
    assert roi["analysis"].startswith("simulated_roi") and roi["n"] == 1
    db.close()


# ------------------------------------------------------------------ probe

def test_probe_stops_on_unauthorized_and_never_records_key(tmp_path):
    from soccerlab.expb import probe
    calls = []

    def fake(url, key):
        calls.append(url)
        return 401, {"x-ratelimit-limit": "5"}, b'{"error":"Unauthorized"}'
    rep = probe.run(tmp_path, key="SECRET-KEY-123", call=fake, sleep=lambda s: None, now=T0)
    assert rep["result"].startswith("STOPPED") and len(calls) == 1
    assert "SECRET-KEY-123" not in json.dumps(rep)


def test_probe_records_permissions_and_rate_headers(tmp_path):
    from soccerlab.expb import probe
    (tmp_path / "epl_2026_matches.json").write_text(json.dumps([match(1, KO)]))
    seen = []

    def fake(url, key):
        seen.append(url)
        body = {"data": [book(1, "draftkings")] if "/odds" in url else [match(1, KO)]}
        return 200, {"x-ratelimit-limit": "5", "x-ratelimit-remaining": "4", "Server": "x"}, json.dumps(body).encode()
    rep = probe.run(tmp_path, key="k", call=fake, sleep=lambda s: None, now=T0)
    assert rep["result"].startswith("OK")
    assert rep["requests"][0]["rate_headers"] == {"x-ratelimit-limit": "5", "x-ratelimit-remaining": "4"}
    assert any(r["endpoint"] == "odds" and r["vendors"] == ["draftkings"] for r in rep["requests"])
    assert len(seen) <= 2 * len(C.LEAGUES)


# ------------------------------------------------------------ external store

def test_external_store_pointer_and_fail_closed(tmp_path, monkeypatch):
    from soccerlab.expb import db as dbm
    usb = tmp_path / "usb" / "soccerlab-research"
    d = dbm.init_store(usb)
    assert dbm.store_dir() == d and dbm.default_path() == d / dbm.DB_NAME
    with DB() as db:
        assert db.path.parent == d
    assert closing.download_dir() == d / "fd"
    # drive unplugged / different stick on the same letter: marker gone -> refuse, create nothing
    (d / dbm.STORE_MARKER).unlink()
    with pytest.raises(dbm.StoreUnavailable):
        DB()
    before = sorted(p.name for p in d.iterdir())
    assert L.main() == 4
    assert sorted(p.name for p in d.iterdir()) == before
    monkeypatch.setenv("SOCCERLAB_EXPB_STORE", str(tmp_path / "nowhere"))
    with pytest.raises(dbm.StoreUnavailable):
        dbm.store_dir()
    assert not (tmp_path / "nowhere").exists()
