"""Research store: SQLite, append-only, single machine.

Layers (one table each, all append-only, enforced by triggers):

  raw_responses   immutable provider payloads + request/ingest metadata
  quotes          market quote events (price, side, vendor, status, raw ref)
  outcomes        observed match states/results; corrections are new rows
  predictions     immutable predictions tied to model version + input hash
  decisions       every bet / no-bet decision with its reasons
  paper_entries   simulated selections (namespace-scoped)
  settlements     grading events; a correction is a new row superseding one
  alerts_sent     dedup ledger for outbound alerts

Canonical ids: match_uid = "bdl:{league}:{match_id}" -- vendor match ids are
only unique within a league feed, so the league is part of the key.

SQLite is enough here: one writer process at a time, WAL journal for safe
concurrent readers, busy_timeout for contention, every write in a
transaction, and `backup()` uses SQLite's online backup API.
"""

import json
import sqlite3
from contextlib import closing
from pathlib import Path

from .hashing import canonical_json, sha256_bytes, sha256_json
from .paths import assert_not_production, ensure_var
from .timeutil import iso, utcnow

NAMESPACES = ("shadow", "paper", "research")
SCHEMA_VERSION = 1

APPEND_ONLY = ("raw_responses", "quotes", "outcomes", "predictions", "decisions", "paper_entries",
               "settlements", "alerts_sent")

DDL = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS raw_responses (
    id INTEGER PRIMARY KEY, source TEXT NOT NULL, endpoint TEXT NOT NULL, request_key TEXT NOT NULL,
    params_json TEXT NOT NULL, http_status INTEGER, provider_ts TEXT, ingested_at TEXT NOT NULL,
    schema_version TEXT NOT NULL, body_sha256 TEXT NOT NULL, body BLOB NOT NULL,
    UNIQUE (source, request_key, body_sha256));
CREATE TABLE IF NOT EXISTS quotes (
    id INTEGER PRIMARY KEY, match_uid TEXT NOT NULL, market TEXT NOT NULL, selection TEXT NOT NULL,
    vendor TEXT NOT NULL, decimal_odds REAL, status TEXT NOT NULL
        CHECK (status IN ('available','suspended','removed','invalid')),
    observed_at TEXT NOT NULL, vendor_updated_at TEXT, raw_ref INTEGER REFERENCES raw_responses(id),
    note TEXT, CHECK (status != 'available' OR decimal_odds > 1.0),
    UNIQUE (match_uid, market, selection, vendor, observed_at));
CREATE INDEX IF NOT EXISTS quotes_match ON quotes (match_uid, market, observed_at);
CREATE TABLE IF NOT EXISTS outcomes (
    id INTEGER PRIMARY KEY, match_uid TEXT NOT NULL, observed_at TEXT NOT NULL, status TEXT NOT NULL,
    status_state TEXT, kickoff TEXT NOT NULL, home_score INTEGER, away_score INTEGER,
    source TEXT NOT NULL, row_sha256 TEXT NOT NULL, UNIQUE (match_uid, row_sha256));
CREATE TABLE IF NOT EXISTS predictions (
    id INTEGER PRIMARY KEY, prediction_uid TEXT NOT NULL UNIQUE,
    namespace TEXT NOT NULL CHECK (namespace IN ('shadow','paper','research')),
    model_id TEXT NOT NULL, model_manifest_sha256 TEXT NOT NULL, match_uid TEXT NOT NULL,
    market TEXT NOT NULL, kickoff TEXT NOT NULL, decision_time TEXT NOT NULL, info_cutoff TEXT NOT NULL,
    probs_json TEXT NOT NULL, input_sha256 TEXT NOT NULL, created_at TEXT NOT NULL,
    CHECK (info_cutoff <= decision_time AND decision_time < kickoff),
    UNIQUE (namespace, model_id, match_uid, market, decision_time));
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY, namespace TEXT NOT NULL, model_id TEXT NOT NULL, match_uid TEXT NOT NULL,
    market TEXT NOT NULL, decision_time TEXT NOT NULL, action TEXT NOT NULL CHECK (action IN ('bet','no_bet')),
    selection TEXT, reasons_json TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS paper_entries (
    id INTEGER PRIMARY KEY, entry_uid TEXT NOT NULL UNIQUE,
    namespace TEXT NOT NULL CHECK (namespace IN ('shadow','paper','research')),
    prediction_uid TEXT NOT NULL REFERENCES predictions(prediction_uid), model_id TEXT NOT NULL,
    match_uid TEXT NOT NULL, league TEXT NOT NULL, market TEXT NOT NULL, selection TEXT NOT NULL,
    vendor TEXT NOT NULL, decimal_odds REAL NOT NULL CHECK (decimal_odds > 1.0),
    quote_observed_at TEXT NOT NULL, stake REAL NOT NULL CHECK (stake > 0), model_prob REAL NOT NULL,
    fair_prob REAL, implied_prob REAL NOT NULL, ev REAL NOT NULL, rule TEXT NOT NULL,
    decision_time TEXT NOT NULL, kickoff TEXT NOT NULL, created_at TEXT NOT NULL,
    CHECK (quote_observed_at <= decision_time AND decision_time < kickoff),
    UNIQUE (namespace, match_uid, market));
CREATE TABLE IF NOT EXISTS settlements (
    id INTEGER PRIMARY KEY, entry_uid TEXT NOT NULL REFERENCES paper_entries(entry_uid),
    state TEXT NOT NULL CHECK (state IN ('settled','void','pending','review')),
    outcome TEXT, pnl REAL, settled_at TEXT NOT NULL, source TEXT NOT NULL, note TEXT,
    supersedes INTEGER REFERENCES settlements(id));
CREATE TABLE IF NOT EXISTS alerts_sent (
    id INTEGER PRIMARY KEY, namespace TEXT NOT NULL, dedup_key TEXT NOT NULL, sent_at TEXT NOT NULL,
    transport TEXT NOT NULL, body_sha256 TEXT NOT NULL, UNIQUE (namespace, dedup_key));
"""


def _triggers():
    out = []
    for t in APPEND_ONLY:
        for op in ("UPDATE", "DELETE"):
            out.append(f"CREATE TRIGGER IF NOT EXISTS {t}_no_{op.lower()} BEFORE {op} ON {t} "
                       f"BEGIN SELECT RAISE(ABORT, '{t} is append-only'); END;")
    return "\n".join(out)


class Store:
    def __init__(self, path=None):
        path = Path(path) if path else ensure_var() / "research.sqlite3"
        assert_not_production(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.db = sqlite3.connect(path, timeout=30, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA busy_timeout=30000")
        self.db.executescript(DDL + _triggers())
        with self.tx():
            self.db.execute("INSERT INTO meta VALUES ('schema_version', ?) ON CONFLICT DO NOTHING", (str(SCHEMA_VERSION),))

    def close(self):
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def tx(self):
        """BEGIN IMMEDIATE ... COMMIT, ROLLBACK on error."""
        store = self

        class _Tx:
            def __enter__(self_inner):
                store.db.execute("BEGIN IMMEDIATE")
                return store.db

            def __exit__(self_inner, et, ev, tb):
                store.db.execute("ROLLBACK" if et else "COMMIT")
                return False
        return _Tx()

    def backup(self, dest):
        dest = Path(dest)
        assert_not_production(dest)
        with closing(sqlite3.connect(dest)) as out:
            self.db.backup(out)
        return dest

    # ------------------------------------------------------------- raw layer

    def put_raw(self, source, endpoint, params, body: bytes, http_status, provider_ts=None,
                schema_version="1", ingested_at=None):
        """Idempotent: an identical payload for the same request is stored once."""
        key = sha256_json({"endpoint": endpoint, "params": params})
        digest = sha256_bytes(body)
        with self.tx() as db:
            db.execute("""INSERT INTO raw_responses (source, endpoint, request_key, params_json,
                http_status, provider_ts, ingested_at, schema_version, body_sha256, body)
                VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING""",
                       (source, endpoint, key, canonical_json(params), http_status, provider_ts,
                        iso(ingested_at or utcnow()), schema_version, digest, body))
            row = db.execute("SELECT id FROM raw_responses WHERE source=? AND request_key=? AND body_sha256=?",
                             (source, key, digest)).fetchone()
        return row["id"]

    # ----------------------------------------------------------- quote layer

    def put_quote_events(self, events):
        """events: dicts with match_uid, market, selection, vendor, decimal_odds,
        status, observed_at (datetime), vendor_updated_at, raw_ref, note.
        Duplicates (same key + observed_at) are ignored -> idempotent."""
        n = 0
        with self.tx() as db:
            for e in events:
                cur = db.execute("""INSERT INTO quotes (match_uid, market, selection, vendor,
                    decimal_odds, status, observed_at, vendor_updated_at, raw_ref, note)
                    VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING""",
                                 (e["match_uid"], e["market"], e["selection"], e["vendor"], e.get("decimal_odds"),
                                  e["status"], iso(e["observed_at"]),
                                  iso(e["vendor_updated_at"]) if e.get("vendor_updated_at") else None,
                                  e.get("raw_ref"), e.get("note")))
                n += cur.rowcount
        return n

    def quotes_asof(self, match_uid, market, t):
        """Latest event per (vendor, selection) at or before t."""
        rows = self.db.execute("""SELECT q.* FROM quotes q JOIN (
                SELECT vendor, selection, MAX(observed_at) AS mx FROM quotes
                WHERE match_uid=? AND market=? AND observed_at<=? GROUP BY vendor, selection) l
              ON q.vendor=l.vendor AND q.selection=l.selection AND q.observed_at=l.mx
              WHERE q.match_uid=? AND q.market=?""", (match_uid, market, iso(t), match_uid, market)).fetchall()
        return [dict(r) for r in rows]

    # --------------------------------------------------------- outcome layer

    def put_outcome(self, match_uid, observed_at, status, status_state, kickoff, home_score, away_score, source):
        """Append an observed state; identical repeats are ignored, changes
        (e.g. a corrected score) become a new row -> full correction history."""
        body = {"status": status, "state": status_state, "kickoff": kickoff, "hs": home_score, "as": away_score}
        with self.tx() as db:
            cur = db.execute("""INSERT INTO outcomes (match_uid, observed_at, status, status_state,
                kickoff, home_score, away_score, source, row_sha256) VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING""",
                             (match_uid, iso(observed_at), status, status_state, kickoff, home_score, away_score,
                              source, sha256_json(body)))
            return cur.rowcount

    def outcome_history(self, match_uid):
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM outcomes WHERE match_uid=? ORDER BY observed_at, id", (match_uid,))]

    # ------------------------------------------------------ prediction layer

    def put_prediction(self, namespace, model_id, manifest_sha, match_uid, market, kickoff, decision_time,
                       info_cutoff, probs, input_sha):
        if namespace not in NAMESPACES:
            raise ValueError(f"unknown namespace {namespace!r}")
        uid = sha256_json({"ns": namespace, "model": model_id, "match": match_uid, "market": market,
                           "t": iso(decision_time)})[:32]
        with self.tx() as db:
            db.execute("""INSERT INTO predictions (prediction_uid, namespace, model_id,
                model_manifest_sha256, match_uid, market, kickoff, decision_time, info_cutoff, probs_json,
                input_sha256, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING""",
                       (uid, namespace, model_id, manifest_sha, match_uid, market, iso(kickoff), iso(decision_time),
                        iso(info_cutoff), json.dumps(probs, sort_keys=True), input_sha, iso(utcnow())))
        return uid

    def log_decision(self, namespace, model_id, match_uid, market, decision_time, action, selection, reasons):
        with self.tx() as db:
            db.execute("""INSERT INTO decisions (namespace, model_id, match_uid, market, decision_time, action,
                selection, reasons_json, created_at) VALUES (?,?,?,?,?,?,?,?,?)""",
                       (namespace, model_id, match_uid, market, iso(decision_time), action, selection,
                        json.dumps(reasons), iso(utcnow())))

    # -------------------------------------------------------- settlement

    def latest_settlement(self, entry_uid):
        r = self.db.execute("SELECT * FROM settlements WHERE entry_uid=? ORDER BY id DESC LIMIT 1",
                            (entry_uid,)).fetchone()
        return dict(r) if r else None

    def put_settlement(self, entry_uid, state, outcome, pnl, source, note=None, settled_at=None):
        """Append a grading event unless it repeats the latest one."""
        prev = self.latest_settlement(entry_uid)
        if prev and (prev["state"], prev["outcome"], prev["pnl"]) == (state, outcome, pnl):
            return None
        with self.tx() as db:
            cur = db.execute("""INSERT INTO settlements (entry_uid, state, outcome, pnl, settled_at, source, note,
                supersedes) VALUES (?,?,?,?,?,?,?,?)""",
                             (entry_uid, state, outcome, pnl, iso(settled_at or utcnow()), source, note,
                              prev["id"] if prev else None))
            return cur.lastrowid

    def entries(self, namespace):
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM paper_entries WHERE namespace=? ORDER BY decision_time, id", (namespace,))]
