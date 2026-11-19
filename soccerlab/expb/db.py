"""Isolated append-only database for EXP-2026-11-B (var/expb/exp_b_odds.sqlite3).

Tables (UPDATE/DELETE abort via triggers on every table):

  raw_responses  every provider page: request key, HTTP status, retrieval time,
                 body (zlib) -- identical repeats stored once
  poll_events    started / finished / failed / interrupted per poll
  fixture_obs    fixture snapshots; a new row only when something changed
                 (kickoff, status, score) -> reschedule history
  quotes         one row per (poll, fixture, venue, selection), including
                 explicit 'missing' / 'suspended' / 'invalid' / 'post_kickoff'
                 rows. Prices are never carried forward or interpolated.
  targets        T-60 / T-15 snapshot bookkeeping: captured / missed / superseded
  closing_refs   externally sourced closing prices (e.g. football-data BFEC*)
  closing_quarantine  external rows that could not be matched unambiguously, with reason
  spot_checks    manual venue price checks (obtainability evidence)
  runs           one row per invocation (liveness heartbeat for monitoring)
"""

import json
import sqlite3
import zlib
from pathlib import Path

from ..hashing import canonical_json, sha256_bytes, sha256_json
from ..paths import assert_not_production, ensure_var
from ..timeutil import iso

TABLES = ("raw_responses", "poll_events", "fixture_obs", "quotes", "targets", "closing_refs", "spot_checks", "runs", "closing_quarantine")

DDL = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS raw_responses (
  id INTEGER PRIMARY KEY, poll_id TEXT NOT NULL, endpoint TEXT NOT NULL, url TEXT NOT NULL,
  request_key TEXT NOT NULL, http_status INTEGER, retrieved_at TEXT NOT NULL,
  body_sha256 TEXT NOT NULL, body_zlib BLOB NOT NULL, UNIQUE (request_key, body_sha256));
CREATE TABLE IF NOT EXISTS poll_events (
  id INTEGER PRIMARY KEY, poll_id TEXT NOT NULL, event TEXT NOT NULL
    CHECK (event IN ('started','finished','failed','interrupted')),
  kind TEXT NOT NULL CHECK (kind IN ('regular','T-60','T-15','schedule')),
  league TEXT NOT NULL, at TEXT NOT NULL, detail TEXT, UNIQUE (poll_id, event));
CREATE TABLE IF NOT EXISTS fixture_obs (
  id INTEGER PRIMARY KEY, fixture_uid TEXT NOT NULL, league TEXT NOT NULL, provider_match_id INTEGER NOT NULL,
  season INTEGER, home_team_id INTEGER, away_team_id INTEGER, kickoff TEXT NOT NULL, status TEXT,
  status_state TEXT, home_score INTEGER, away_score INTEGER, retrieved_at TEXT NOT NULL,
  poll_id TEXT NOT NULL, row_sha256 TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS fixture_obs_uid ON fixture_obs (fixture_uid, id);
CREATE TABLE IF NOT EXISTS quotes (
  id INTEGER PRIMARY KEY, poll_id TEXT NOT NULL, snapshot_kind TEXT NOT NULL,
  fixture_uid TEXT NOT NULL, league TEXT NOT NULL, provider_match_id INTEGER NOT NULL,
  venue TEXT NOT NULL, venue_class TEXT NOT NULL, eligibility TEXT NOT NULL,
  market TEXT NOT NULL, selection TEXT NOT NULL CHECK (selection IN ('home','draw','away')),
  status TEXT NOT NULL CHECK (status IN ('available','suspended','missing','invalid','post_kickoff')),
  american_odds REAL, decimal_odds REAL, source_updated_at TEXT, retrieved_at TEXT NOT NULL,
  kickoff_at_retrieval TEXT, minutes_to_kickoff REAL, flags TEXT NOT NULL, raw_ref INTEGER,
  CHECK (status != 'available' OR decimal_odds > 1.0),
  CHECK (status = 'available' OR status = 'invalid' OR decimal_odds IS NULL),
  UNIQUE (poll_id, fixture_uid, venue, selection));
CREATE INDEX IF NOT EXISTS quotes_fixture ON quotes (fixture_uid, retrieved_at);
CREATE TABLE IF NOT EXISTS targets (
  id INTEGER PRIMARY KEY, fixture_uid TEXT NOT NULL, target TEXT NOT NULL, kickoff TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('captured','missed','superseded')),
  poll_id TEXT, recorded_at TEXT NOT NULL, detail TEXT, UNIQUE (fixture_uid, target, kickoff, status));
CREATE TABLE IF NOT EXISTS closing_refs (
  id INTEGER PRIMARY KEY, fixture_uid TEXT NOT NULL, source TEXT NOT NULL, venue TEXT NOT NULL,
  market TEXT NOT NULL, home REAL NOT NULL, draw REAL NOT NULL, away REAL NOT NULL,
  source_file_sha256 TEXT NOT NULL, imported_at TEXT NOT NULL, match_note TEXT,
  UNIQUE (fixture_uid, source, venue));
CREATE TABLE IF NOT EXISTS spot_checks (
  id INTEGER PRIMARY KEY, fixture_uid TEXT NOT NULL, venue TEXT NOT NULL, selection TEXT NOT NULL,
  observed_american REAL NOT NULL, observed_at TEXT NOT NULL, stake_accepted REAL, note TEXT,
  recorded_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS closing_quarantine (
  id INTEGER PRIMARY KEY, league TEXT NOT NULL, row_key TEXT NOT NULL, fixture_uid TEXT, reason TEXT NOT NULL,
  source_file_sha256 TEXT NOT NULL, recorded_at TEXT NOT NULL, UNIQUE (league, row_key, reason, source_file_sha256));
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT NOT NULL, summary TEXT NOT NULL);
"""


def _triggers():
    return "\n".join(
        f"CREATE TRIGGER IF NOT EXISTS {t}_no_{op.lower()} BEFORE {op} ON {t} "
        f"BEGIN SELECT RAISE(ABORT, '{t} is append-only'); END;"
        for t in TABLES for op in ("UPDATE", "DELETE"))


DB_NAME = "exp_b_odds.sqlite3"
STORE_MARKER = ".soccerlab-expb-store"
POINTER = "DB_LOCATION"          # var/expb/DB_LOCATION: one line, the external store directory
IDENTITY = "STORE_IDENTITY"      # var/expb/STORE_IDENTITY: volume serial the store must be on (optional)


class StoreUnavailable(RuntimeError):
    """The configured external store is missing (drive unplugged, different
    stick on the same letter, folder removed). Fail closed: never create a
    fresh database somewhere unexpected."""


def store_dir():
    """Directory holding the database, lock file and downloaded CSVs.

    Order: SOCCERLAB_EXPB_STORE env var, then the pointer file
    var/expb/DB_LOCATION, then var/expb/ itself. An external store must
    already exist and carry STORE_MARKER (created by `expb init-store`)."""
    import os
    local = ensure_var() / "expb"
    local.mkdir(exist_ok=True)
    target = os.environ.get("SOCCERLAB_EXPB_STORE")
    if not target and (local / POINTER).exists():
        target = (local / POINTER).read_text(encoding="utf-8").strip()
    if not target:
        return local
    d = Path(target)
    if not (d / STORE_MARKER).is_file():
        raise StoreUnavailable(f"external store {d} unavailable or not initialised (missing {STORE_MARKER})")
    ident = local / IDENTITY
    if ident.exists():
        from .volume import volume_id
        want, got = ident.read_text(encoding="utf-8").strip(), volume_id(d)
        if want != got:
            raise StoreUnavailable(f"store {d} is on volume {got}, expected {want} (different drive on the same letter?)")
    return d


def default_path():
    return store_dir() / DB_NAME


def init_store(directory):
    """Create an external store directory, mark it, and point the logger at it."""
    import json as _json
    d = Path(directory)
    assert_not_production(d)
    d.mkdir(parents=True, exist_ok=True)
    marker = d / STORE_MARKER
    if not marker.exists():
        marker.write_text(_json.dumps({"experiment": "EXP-2026-11-B", "created_at": iso(utcnow_())}) + "\n",
                          encoding="utf-8")
    local = ensure_var() / "expb"
    local.mkdir(exist_ok=True)
    (local / POINTER).write_text(str(d) + "\n", encoding="utf-8")
    return d


def pin_store_identity():
    """Record the current store's volume serial in var/expb/STORE_IDENTITY (on the
    local disk, not on the store) so that a different drive mounted at the same
    letter is refused even if the marker file was copied onto it."""
    from .volume import volume_id
    d = store_dir()
    local = ensure_var() / "expb"
    vid = volume_id(d)
    (local / IDENTITY).write_text(vid + "\n", encoding="utf-8")
    return d, vid


def utcnow_():
    from ..timeutil import utcnow
    return utcnow()


class DB:
    def __init__(self, path=None):
        path = Path(path) if path else default_path()
        assert_not_production(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.c = sqlite3.connect(path, timeout=30, isolation_level=None)
        self.c.row_factory = sqlite3.Row
        self.c.execute("PRAGMA journal_mode=WAL")
        self.c.execute("PRAGMA busy_timeout=30000")
        self.c.executescript(DDL + _triggers())
        self.c.execute("INSERT INTO meta VALUES ('experiment','EXP-2026-11-B') ON CONFLICT DO NOTHING")

    def close(self):
        self.c.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def tx(self):
        db = self

        class _Tx:
            def __enter__(self):
                db.c.execute("BEGIN IMMEDIATE")
                return db.c

            def __exit__(self, et, ev, tb):
                db.c.execute("ROLLBACK" if et else "COMMIT")
                return False
        return _Tx()

    # ------------------------------------------------------------ writers

    def poll_event(self, poll_id, event, kind, league, at, detail=None):
        with self.tx() as c:
            c.execute("INSERT INTO poll_events (poll_id, event, kind, league, at, detail) VALUES (?,?,?,?,?,?) "
                      "ON CONFLICT DO NOTHING", (poll_id, event, kind, league, iso(at), detail))

    def put_raw(self, poll_id, endpoint, url, status, body: bytes, at):
        key = sha256_bytes(url.encode())
        digest = sha256_bytes(body or b"")
        with self.tx() as c:
            c.execute("""INSERT INTO raw_responses (poll_id, endpoint, url, request_key, http_status, retrieved_at,
                body_sha256, body_zlib) VALUES (?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING""",
                      (poll_id, endpoint, url, key, status, iso(at), digest, zlib.compress(body or b"", 6)))
            r = c.execute("SELECT id FROM raw_responses WHERE request_key=? AND body_sha256=?", (key, digest)).fetchone()
        return r["id"]

    def latest_fixture(self, fixture_uid):
        r = self.c.execute("SELECT * FROM fixture_obs WHERE fixture_uid=? ORDER BY id DESC LIMIT 1",
                           (fixture_uid,)).fetchone()
        return dict(r) if r else None

    def put_fixture(self, row, poll_id, at):
        """Append only if the fixture changed since its latest snapshot.
        Returns (inserted, previous_row)."""
        body = {k: row[k] for k in ("kickoff", "status", "status_state", "home_score", "away_score",
                                    "home_team_id", "away_team_id", "season")}
        digest = sha256_json(body)
        prev = self.latest_fixture(row["fixture_uid"])
        if prev and prev["row_sha256"] == digest:
            return False, prev
        with self.tx() as c:
            c.execute("""INSERT INTO fixture_obs (fixture_uid, league, provider_match_id, season, home_team_id,
                away_team_id, kickoff, status, status_state, home_score, away_score, retrieved_at, poll_id, row_sha256)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                      (row["fixture_uid"], row["league"], row["provider_match_id"], row["season"],
                       row["home_team_id"], row["away_team_id"], row["kickoff"], row["status"], row["status_state"],
                       row["home_score"], row["away_score"], iso(at), poll_id, digest))
        return True, prev

    def put_quotes(self, rows):
        with self.tx() as c:
            n = 0
            for r in rows:
                cur = c.execute("""INSERT INTO quotes (poll_id, snapshot_kind, fixture_uid, league, provider_match_id,
                    venue, venue_class, eligibility, market, selection, status, american_odds, decimal_odds,
                    source_updated_at, retrieved_at, kickoff_at_retrieval, minutes_to_kickoff, flags, raw_ref)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING""",
                                (r["poll_id"], r["snapshot_kind"], r["fixture_uid"], r["league"], r["provider_match_id"],
                                 r["venue"], r["venue_class"], r["eligibility"], r["market"], r["selection"], r["status"],
                                 r["american_odds"], r["decimal_odds"], r["source_updated_at"], r["retrieved_at"],
                                 r["kickoff_at_retrieval"], r["minutes_to_kickoff"], json.dumps(sorted(r["flags"])),
                                 r["raw_ref"]))
                n += cur.rowcount
        return n

    def put_target(self, fixture_uid, target, kickoff, status, poll_id, at, detail=None):
        with self.tx() as c:
            cur = c.execute("""INSERT INTO targets (fixture_uid, target, kickoff, status, poll_id, recorded_at, detail)
                VALUES (?,?,?,?,?,?,?) ON CONFLICT DO NOTHING""",
                            (fixture_uid, target, kickoff, status, poll_id, iso(at), detail))
            return cur.rowcount

    def target_status(self, fixture_uid, target, kickoff):
        return {r[0] for r in self.c.execute(
            "SELECT status FROM targets WHERE fixture_uid=? AND target=? AND kickoff=?", (fixture_uid, target, kickoff))}

    # ------------------------------------------------------------ readers

    def schedule(self, league=None):
        """Latest snapshot per fixture."""
        latest = """SELECT f.* FROM fixture_obs f JOIN (SELECT fixture_uid, MAX(id) mx FROM fixture_obs
                    GROUP BY fixture_uid) l ON f.id = l.mx"""
        if league:
            return [dict(r) for r in self.c.execute(latest + " WHERE f.league=?", (league,))]  # noqa: S608 (constant)
        return [dict(r) for r in self.c.execute(latest)]

    def last_poll(self, kind, league, event="finished"):
        r = self.c.execute("SELECT at FROM poll_events WHERE kind=? AND league=? AND event=? ORDER BY at DESC LIMIT 1",
                           (kind, league, event)).fetchone()
        return r[0] if r else None

    def open_polls(self):
        return [dict(r) for r in self.c.execute("""SELECT * FROM poll_events s WHERE event='started' AND NOT EXISTS (
            SELECT 1 FROM poll_events t WHERE t.poll_id=s.poll_id AND t.event IN ('finished','failed','interrupted'))""")]

    def seen_venues(self, fixture_uid):
        return {r[0] for r in self.c.execute(
            "SELECT DISTINCT venue FROM quotes WHERE fixture_uid=? AND status!='missing'", (fixture_uid,))}

    def raw_body(self, raw_id):
        r = self.c.execute("SELECT body_zlib FROM raw_responses WHERE id=?", (raw_id,)).fetchone()
        return zlib.decompress(r[0]) if r else None


__all__ = ["DB", "default_path", "canonical_json"]
