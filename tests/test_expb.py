"""EXP-2026-11-B research logger: isolation, polling, failures, recovery, monitoring, protocol guard."""

import json
import shutil
import sqlite3
import subprocess
import sys
import urllib.parse
from datetime import timedelta

import pytest

from soccerlab import provider
from soccerlab.expb import closing, config as C, evaluate, logger as L, monitor
from soccerlab.expb.db import DB
from soccerlab.paths import REPO_ROOT
from soccerlab.timeutil import iso, parse_utc

T0 = parse_utc("2026-11-21T09:00:00Z")
KO = parse_utc("2026-11-21T15:00:00Z")


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


class FakeAPI:
    """Serves /{league}/{v}/matches and /odds from in-memory data."""

    def __init__(self, clock):
        self.clock = clock
        self.schedules = {}
        self.odds = {}
        self.fail = {}            # endpoint -> list of HTTP statuses returned first
        self.calls = []

    def __call__(self, url, headers):
        assert headers.get("Authorization") == "test-key"
        self.calls.append(url)
        u = urllib.parse.urlparse(url)
        _, league, _ver, endpoint = u.path.split("/")
        if self.fail.get(endpoint):
            return self.fail[endpoint].pop(0), b'{"error":"x"}'
        if endpoint == "matches":
            data = self.schedules.get(league, [])
        else:
            ids = [int(x) for x in urllib.parse.parse_qs(u.query).get("match_ids[]", [])]
            data = []
            for i in ids:
                for r in self.odds.get((league, i), []):
                    r = dict(r)
                    r.setdefault("updated_at", iso(self.clock() - timedelta(minutes=1)))
                    data.append(r)
        return 200, json.dumps({"data": data, "meta": {"next_cursor": None}}).encode()


def match(mid, ko, state="scheduled", status="STATUS_SCHEDULED"):
    return {"id": mid, "season": 2026, "date": ko.strftime("%Y-%m-%dT%H:%M:%S.000Z"), "home_team_id": 10 + mid,
            "away_team_id": 50 + mid, "status": status, "status_state": state, "home_score": 0, "away_score": 0}


def book(mid, vendor, h=150, d=240, a=190, **kw):
    return dict({"match_id": mid, "vendor": vendor, "moneyline_home_odds": h, "moneyline_draw_odds": d,
                 "moneyline_away_odds": a}, **kw)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "LEAGUES", ("epl",))
    clock = Clock(T0)
    api = FakeAPI(clock)
    api.schedules["epl"] = [match(1, KO), match(2, KO + timedelta(days=2))]
    api.odds[("epl", 1)] = [book(1, "draftkings"), book(1, "fanduel", 155, 235, 185), book(1, "polymarket", 160, 250, 200)]
    api.odds[("epl", 2)] = [book(2, "draftkings")]
    db = DB(tmp_path / "expb.sqlite3")
    client = provider.Client("test-key", transport=api, sleep=lambda s: None)
    runner = L.Runner(db, client, now_fn=clock, sleep=lambda s: None, monotonic=lambda: 0.0)
    yield clock, api, db, runner
    db.close()


def q(db, sql, *a):
    return [dict(r) for r in db.c.execute(sql, a)]


# --------------------------------------------------------------- isolation

def test_logger_imports_no_legacy_or_alerting_code():
    code = ("import sys; import soccerlab.expb.logger, soccerlab.expb.monitor, soccerlab.expb.evaluate, "
            "soccerlab.expb.closing, soccerlab.expb.cli; "
            "bad = {'predict','paper','telegram_alerts','generate_live_card','dcmodel','backtest','log_odds','settle',"
            "'bot','soccerlab.ledger','soccerlab.alerts','soccerlab.live','soccerlab.legacy','soccerlab.dcpit'}; "
            "print(sorted(bad & set(sys.modules)))")
    out = subprocess.run([sys.executable, "-c", code], cwd=REPO_ROOT, capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "[]", out.stdout


def test_logger_source_has_no_selection_or_alert_paths():
    src = "".join(p.read_text(encoding="utf-8") for p in (REPO_ROOT / "soccerlab" / "expb").glob("*.py"))
    for forbidden in ("import telegram", "telegram_alerts import", "api.telegram.org", "send_message(", "paper_entries",
                      "RiskEngine", "Alerter(", "kelly_fraction", "import paper", "import predict"):
        assert forbidden not in src, forbidden


# --------------------------------------------------------------- polling

def test_first_run_loads_schedule_then_regular_poll(env):
    clock, api, db, runner = env
    r1 = runner.run_once()
    assert [a["kind"] for a in r1["actions"]] == ["schedule"]
    assert len(db.schedule("epl")) == 2
    clock.t += timedelta(minutes=5)
    r2 = runner.run_once()
    assert [a["kind"] for a in r2["actions"]] == ["regular"]
    rows = q(db, "SELECT venue, eligibility, status, snapshot_kind FROM quotes WHERE fixture_uid='bdl:epl:1'")
    assert {r["venue"] for r in rows} == {"draftkings", "fanduel", "polymarket"}
    assert all(r["eligibility"] == "observational" for r in rows if r["venue"] == "polymarket")
    assert all(r["eligibility"] == "candidate_unverified" for r in rows if r["venue"] != "polymarket")
    assert all(r["status"] == "available" and r["snapshot_kind"] == "regular" for r in rows)
    # fixture 2: only DraftKings quoted -> explicit 'missing' rows for the others, no invented prices
    miss = q(db, "SELECT venue, decimal_odds FROM quotes WHERE fixture_uid='bdl:epl:2' AND status='missing'")
    assert {m["venue"] for m in miss} == {"fanduel", "polymarket"} and all(m["decimal_odds"] is None for m in miss)
    with pytest.raises(sqlite3.IntegrityError):
        db.c.execute("UPDATE quotes SET decimal_odds = 9.9")       # append-only
    clock.t += timedelta(minutes=5)
    assert runner.run_once()["actions"] == []                      # regular interval not elapsed; nothing invented
    assert db.c.execute("SELECT count(*) FROM runs").fetchone()[0] == 3


def test_quote_quality_flags(env):
    clock, api, db, runner = env
    runner.run_once()
    api.odds[("epl", 1)] = [
        book(1, "draftkings", d=None),                                          # suspended selection
        book(1, "fanduel", -100000, 25000, 35000),                              # degenerate in-play junk
        book(1, "polymarket", 300, 300, -150, updated_at=iso(T0 - timedelta(hours=5))),   # stale source
        book(1, "betmgm")]                                                      # unknown venue
    clock.t += timedelta(minutes=5)
    runner.run_once()
    rows = {(r["venue"], r["selection"]): r for r in q(db, "SELECT * FROM quotes WHERE fixture_uid='bdl:epl:1'")}
    assert rows[("draftkings", "draw")]["status"] == "suspended"
    assert "partial_book" in json.loads(rows[("draftkings", "home")]["flags"])
    assert rows[("fanduel", "home")]["status"] == "invalid"
    assert "degenerate_price" in json.loads(rows[("fanduel", "home")]["flags"])
    assert "source_stale" in json.loads(rows[("polymarket", "home")]["flags"])
    assert rows[("betmgm", "home")]["eligibility"] == "observational"
    assert "unknown_venue" in json.loads(rows[("betmgm", "home")]["flags"])


def test_incoherent_book_and_post_kickoff(env):
    clock, api, db, runner = env
    runner.run_once()
    fx = db.schedule("epl")
    arb = [(book(1, "draftkings", 400, 400, 110), None)]          # implied sum < 1: stale/mismatched
    out = runner.build_quotes("p1", "regular", "epl", [f for f in fx if f["provider_match_id"] == 1], arb, T0)
    assert all(o["status"] == "invalid" and "book_incoherent" in o["flags"] for o in out if o["venue"] == "draftkings")
    late = runner.build_quotes("p2", "regular", "epl", [f for f in fx if f["provider_match_id"] == 1],
                               [(book(1, "draftkings"), None)], KO + timedelta(minutes=1))
    assert all(o["status"] == "post_kickoff" and o["decimal_odds"] is None for o in late if o["venue"] == "draftkings")


def test_targeted_snapshots_captured_once(env):
    clock, api, db, runner = env
    runner.run_once()
    clock.t = KO - timedelta(minutes=60)
    kinds = [a["kind"] for a in runner.run_once()["actions"]]
    assert "T-60" in kinds
    clock.t = KO - timedelta(minutes=55)
    assert "T-60" not in [a["kind"] for a in runner.run_once()["actions"]]
    clock.t = KO - timedelta(minutes=14)
    acts = runner.run_once()["actions"]
    assert [a["kind"] for a in acts][0] == "T-15"                   # highest priority
    st = q(db, "SELECT target, status FROM targets WHERE fixture_uid='bdl:epl:1'")
    assert sorted((s["target"], s["status"]) for s in st) == [("T-15", "captured"), ("T-60", "captured")]
    assert q(db, "SELECT count(DISTINCT poll_id) n FROM quotes WHERE snapshot_kind='T-15'")[0]["n"] == 1


def test_api_failure_records_failure_not_snapshot_then_missed(env):
    clock, api, db, runner = env
    runner.run_once()
    clock.t = KO - timedelta(minutes=58)
    api.fail["odds"] = [500] * 20
    res = runner.run_once()
    assert any(a["result"].startswith("failed") for a in res["actions"])
    assert q(db, "SELECT count(*) n FROM quotes WHERE snapshot_kind='T-60'")[0]["n"] == 0
    assert q(db, "SELECT count(*) n FROM poll_events WHERE event='failed' AND kind='T-60'")[0]["n"] == 1
    api.fail["odds"] = []
    clock.t = KO - timedelta(minutes=40)                            # T-60 window has closed
    runner.run_once()
    st = {(s["target"], s["status"]) for s in q(db, "SELECT target, status FROM targets WHERE fixture_uid='bdl:epl:1'")}
    assert ("T-60", "missed") in st and ("T-60", "captured") not in st


def test_auth_failure_stops_run_and_main_exits_nonzero(env, tmp_path, monkeypatch):
    clock, api, db, runner = env
    runner.run_once()
    clock.t += timedelta(hours=3)
    api.fail["odds"] = [401]
    res = runner.run_once()
    assert res.get("auth_failure") and "AUTH FAILURE" in res["actions"][0]["result"]
    assert len(res["actions"]) == 1                                 # nothing attempted after the 401
    api.fail["matches"] = [401]
    client = provider.Client("test-key", transport=api, sleep=lambda s: None)
    monkeypatch.setattr(L, "utcnow", clock)
    assert L.main(db_path=tmp_path / "other.sqlite3", client=client) == 1


def test_reschedule_supersedes_capture_and_reschedules_target(env):
    clock, api, db, runner = env
    runner.run_once()
    clock.t = KO - timedelta(minutes=60)
    runner.run_once()
    new_ko = KO + timedelta(days=3)
    api.schedules["epl"] = [match(1, new_ko), match(2, KO + timedelta(days=2))]
    clock.t = KO - timedelta(minutes=50)
    runner.refresh_schedule("epl")
    hist = q(db, "SELECT kickoff FROM fixture_obs WHERE fixture_uid='bdl:epl:1' ORDER BY id")
    assert [h["kickoff"] for h in hist] == [iso(KO), iso(new_ko)]
    st = {(s["kickoff"], s["status"]) for s in q(db, "SELECT kickoff, status FROM targets WHERE fixture_uid='bdl:epl:1'")}
    assert (iso(KO), "superseded") in st
    clock.t = new_ko - timedelta(minutes=60)
    assert "T-60" in [a["kind"] for a in runner.run_once()["actions"]]


def test_restart_recovery_and_locking(env, tmp_path, monkeypatch):
    clock, api, db, runner = env
    runner.run_once()
    clock.t += timedelta(hours=3)

    def boom(rows):
        raise RuntimeError("killed mid-write")
    monkeypatch.setattr(db, "put_quotes", boom)
    with pytest.raises(RuntimeError):
        runner.run_once()
    monkeypatch.undo()
    assert len(db.open_polls()) == 1
    clock.t += timedelta(minutes=31)
    res = runner.run_once()
    assert res["recovered"] == 1 and db.open_polls() == []
    assert q(db, "SELECT count(*) n FROM poll_events WHERE event='interrupted'")[0]["n"] == 1
    # lock: fresh lock blocks, stale lock is broken
    lock = tmp_path / "logger.lock"
    with L.RunLock(lock, now_fn=clock):
        with pytest.raises(L.Locked):
            with L.RunLock(lock, now_fn=clock):
                pass
    lock.write_text(json.dumps({"pid": 1, "at": iso(clock.t - timedelta(hours=1))}))
    with L.RunLock(lock, now_fn=clock) as lk:
        assert lk.broke_stale
    assert not lock.exists()


def test_raw_dedup_and_append_only(env):
    clock, api, db, runner = env
    runner.run_once()
    n = db.c.execute("SELECT count(*) FROM raw_responses").fetchone()[0]
    runner.refresh_schedule("epl")                                  # identical body -> stored once
    assert db.c.execute("SELECT count(*) FROM raw_responses").fetchone()[0] == n
    assert db.raw_body(1).startswith(b'{"data"')
    for t in ("fixture_obs", "poll_events", "raw_responses", "runs"):            # non-empty tables
        with pytest.raises(sqlite3.IntegrityError):
            db.c.execute(f"DELETE FROM {t}")  # noqa: S608 (constant table names)


def test_schedule_validation_failure_writes_nothing(env):
    clock, api, db, runner = env
    api.schedules["epl"] = [dict(match(1, KO), season=2025)]
    with pytest.raises(provider.IncompleteResponse):
        runner.refresh_schedule("epl")
    assert db.schedule("epl") == []
    assert q(db, "SELECT event FROM poll_events ORDER BY id")[-1]["event"] == "failed"


# --------------------------------------------------------------- monitoring

def test_monitor_levels(env):
    clock, api, db, runner = env
    level, _ = monitor.check(db, now=clock.t)
    assert level == "red"                                           # no runs yet
    runner.run_once()
    clock.t += timedelta(minutes=5)
    runner.run_once()
    level, checks = monitor.check(db, now=clock.t)
    assert level in ("green", "amber"), checks
    level, checks = monitor.check(db, now=clock.t + timedelta(hours=2))
    assert level == "red" and any("liveness" in c[1] and c[0] == "red" for c in checks)


# --------------------------------------------------------------- protocol guard

def test_evaluate_refuses_while_draft(env):
    _, _, db, _ = env
    assert evaluate.load_protocol()["status"] == "draft"
    with pytest.raises(evaluate.ProtocolNotLocked):
        evaluate.primary(db)
    dq = evaluate.data_quality(db)
    assert "ev_vs_close" not in json.dumps(dq)


def test_evaluate_locked_one_entry_per_fixture(env):
    clock, api, db, runner = env
    proto = dict(evaluate.load_protocol(), status="locked", locked_at=iso(T0 - timedelta(days=2)), n_boot=200)
    runner.run_once()                                               # schedule
    clock.t = KO - timedelta(hours=24, minutes=30)                  # entry snapshot within [E-3h, E]
    api.odds[("epl", 1)] = [book(1, "draftkings", 200, 240, 150), book(1, "fanduel", 150, 240, 190)]
    runner.run_once()
    clock.t = KO - timedelta(minutes=15)                            # closing snapshot
    api.odds[("epl", 1)] = [book(1, "draftkings", 170, 240, 170), book(1, "fanduel", 165, 240, 175)]
    runner.run_once()
    with db.tx() as c:
        c.execute("INSERT INTO closing_refs (fixture_uid, source, venue, market, home, draw, away, "
                  "source_file_sha256, imported_at) VALUES ('bdl:epl:1','test','betfair_exchange','1x2_ft90',"
                  "2.6,3.5,2.9,'x','now')")
    out = evaluate.primary(db, proto, now=KO + timedelta(days=1))
    assert out["strong"]["n"] == 1 and out["weak"]["n"] == 1
    assert out["strong"]["verdict"].startswith("inconclusive")        # n < min_n is never a pass


# --------------------------------------------------------------- closing import

def test_closing_import_matches_or_reports(env, tmp_path, monkeypatch):
    clock, api, db, runner = env
    runner.run_once()
    al = tmp_path / "aliases.json"
    al.write_text(json.dumps({"aliases": {"epl": {"Home One": 11, "Away One": 51}}}))
    monkeypatch.setattr(closing, "ALIASES", al)
    csvp = tmp_path / "E0.csv"
    csvp.write_text("Div,Date,Time,HomeTeam,AwayTeam,FTHG,FTAG,BFECH,BFECD,BFECA\n"
                    "E0,21/11/2026,15:00,Home One,Away One,1,0,2.5,3.4,3.1\n"
                    "E0,21/11/2026,15:00,Nobody FC,Away One,1,0,2.5,3.4,3.1\n"
                    "E0,21/11/2026,15:00,Home One,Away One,1,0,,,\n", encoding="utf-8")
    rep, unmapped = closing.import_closing(db, csvp, "epl", now=clock.t)
    assert rep["imported"] == 1 and rep["unmapped_team"] == 1 and unmapped == {"Nobody FC": 1}
    assert rep.get("missing_or_invalid_price", 0) + rep.get("already_present", 0) == 1


# --------------------------------------------------------------- installer

@pytest.mark.skipif(shutil.which("powershell") is None, reason="Windows PowerShell not available")
def test_install_script_defaults_to_dry_run():
    out = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                          str(REPO_ROOT / "research_tasks" / "install_expb_task.ps1")],
                         capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    assert "DRY RUN - nothing registered" in out.stdout
