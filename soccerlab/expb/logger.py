"""Research odds logger runner.

    python -m soccerlab expb run          one invocation (meant to be scheduled every 5 min)

Each invocation:
  1. takes an exclusive lock (stale locks older than LOCK_STALE are broken),
  2. marks polls that started but never finished as 'interrupted',
  3. plans due work in priority order:
       missing schedules -> T-15 snapshots -> T-60 snapshots -> regular polls
       -> one stale schedule refresh,
  4. executes within MAX_RUN_SECONDS, spacing requests for the shared
     5 req/min key, stopping immediately on an authentication failure,
  5. records targets whose window passed without a capture as 'missed',
  6. appends one row to `runs`.

A failed request writes a 'failed' poll event and NO quote rows. Absent
venues get explicit 'missing' rows. Nothing is carried forward or filled in.
"""

import json
import os
import time
import uuid
from collections import defaultdict
from datetime import timedelta
from pathlib import Path

from .. import provider
from ..odds import InvalidOdds, american_to_decimal, validate_1x2
from ..timeutil import iso, parse_utc, utcnow
from . import config as C
from .db import DB


def load_key():
    """Same lookup as the legacy loaders; the key is never logged."""
    k = os.environ.get("BALLDONTLIE_KEY")
    if k:
        return k.strip()
    p = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "soccer-betting-bot" / "bdl.key"
    return p.read_text().strip() if p.exists() else None


class Locked(RuntimeError):
    pass


class RunLock:
    def __init__(self, path, now_fn=utcnow):
        self.path, self.now_fn, self.broke_stale = Path(path), now_fn, False

    def __enter__(self):
        for _ in range(2):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, json.dumps({"pid": os.getpid(), "at": iso(self.now_fn())}).encode())
                os.close(fd)
                return self
            except FileExistsError:
                try:
                    at = parse_utc(json.loads(self.path.read_text())["at"])
                except (ValueError, KeyError, OSError, json.JSONDecodeError):
                    at = None
                if at is not None and self.now_fn() - at < C.LOCK_STALE:
                    raise Locked(f"another run holds {self.path}") from None
                self.path.unlink(missing_ok=True)       # stale or unreadable: break it once
                self.broke_stale = True
        raise Locked("could not acquire lock")

    def __exit__(self, *exc):
        self.path.unlink(missing_ok=True)
        return False


def _uid(league, mid):
    return f"bdl:{league}:{int(mid)}"


class Runner:
    def __init__(self, db: DB, client, now_fn=utcnow, sleep=time.sleep, monotonic=time.monotonic,
                 budget=C.MAX_RUN_SECONDS, spacing=C.SPACING_SECONDS):
        self.db, self.client, self.now_fn, self.sleep = db, client, now_fn, sleep
        self.monotonic, self.budget, self.spacing = monotonic, budget, spacing
        self._requests = 0
        self._last_raw = None
        self._poll_id = None
        self._endpoint = None
        client.on_page = self._on_page

    # --------------------------------------------------------------- plumbing

    def _on_page(self, url, status, body):
        self._last_raw = self.db.put_raw(self._poll_id or "-", self._endpoint or "-", url, status, body or b"",
                                         self.now_fn())

    def _get(self, url):
        if self._requests:
            self.sleep(self.spacing)
        self._requests += 1
        return self.client.get(url)

    def _pages(self, league, endpoint, params, max_pages=20):
        """Yield (row, raw_id). Raises provider errors; repeated cursors raise."""
        cursor, seen = None, set()
        for _ in range(max_pages):
            p = dict(params, **({"cursor": cursor} if cursor is not None else {}))
            data = self._get(provider.url_for(league, endpoint, p))
            raw = self._last_raw
            for r in data["data"]:
                yield r, raw
            cursor = (data.get("meta") or {}).get("next_cursor")
            if cursor is None:
                return
            if cursor in seen:
                raise provider.IncompleteResponse(f"repeated cursor {cursor}")
            seen.add(cursor)
        raise provider.IncompleteResponse("too many pages")

    # --------------------------------------------------------------- recovery

    def recover(self, now):
        n = 0
        for p in self.db.open_polls():
            if now - parse_utc(p["at"]) >= C.INTERRUPTED_AFTER:
                self.db.poll_event(p["poll_id"], "interrupted", p["kind"], p["league"], now,
                                   "no terminal event (process died or was killed)")
                n += 1
        return n

    # --------------------------------------------------------------- planning

    def _fixtures(self):
        out = defaultdict(list)
        for f in self.db.schedule():
            out[f["league"]].append(f)
        return out

    def plan(self, now):
        fx = self._fixtures()
        acts = [("schedule", lg, None) for lg in C.LEAGUES if not fx.get(lg)]
        for name, _m, lo, hi in sorted(C.TARGETS, key=lambda t: t[1]):           # T-15 before T-60
            for lg in C.LEAGUES:
                due = []
                for f in fx.get(lg, []):
                    if f["status_state"] != "scheduled":
                        continue
                    mins = (parse_utc(f["kickoff"]) - now).total_seconds() / 60
                    if lo <= mins <= hi and "captured" not in self.db.target_status(f["fixture_uid"], name, f["kickoff"]):
                        due.append(f)
                if due:
                    acts.append((name, lg, due))
        for lg in C.LEAGUES:
            last = self.db.last_poll("regular", lg)
            if last is None or now - parse_utc(last) >= C.REGULAR_INTERVAL:
                up = [f for f in fx.get(lg, []) if f["status_state"] == "scheduled"
                      and now < parse_utc(f["kickoff"]) <= now + C.LOOKAHEAD]
                if up:
                    acts.append(("regular", lg, up))
        stale = []
        for lg in C.LEAGUES:
            if fx.get(lg):
                last = self.db.last_poll("schedule", lg)
                if last is None or now - parse_utc(last) >= C.SCHEDULE_REFRESH:
                    stale.append((last or "", lg))
        for _, lg in sorted(stale)[:1]:
            acts.append(("schedule", lg, None))
        return acts

    @staticmethod
    def _cost(act):
        kind, _lg, fx = act
        return 4 if kind == "schedule" else max(1, -(-len(fx) // C.BATCH))

    # --------------------------------------------------------------- actions

    def refresh_schedule(self, league):
        now = self.now_fn()
        pid = self._start("schedule", league, now)
        try:
            rows = list(self._pages(league, "matches", {"season": C.SEASON, "per_page": 100}))
            parsed = []
            for r, _raw in rows:
                if r.get("season") != C.SEASON or not r.get("id") or not r.get("date") \
                        or r.get("home_team_id") is None or r.get("away_team_id") is None:
                    raise provider.IncompleteResponse(f"schedule row failed validation: id={r.get('id')}")
                parsed.append({"fixture_uid": _uid(league, r["id"]), "league": league,
                               "provider_match_id": int(r["id"]), "season": r["season"],
                               "home_team_id": r["home_team_id"], "away_team_id": r["away_team_id"],
                               "kickoff": iso(parse_utc(r["date"])), "status": r.get("status"),
                               "status_state": r.get("status_state"),
                               "home_score": r.get("home_score") if r.get("status_state") == "final" else None,
                               "away_score": r.get("away_score") if r.get("status_state") == "final" else None})
            if not parsed:
                raise provider.IncompleteResponse("empty schedule")
        except provider.ProviderError as exc:
            self._fail(pid, "schedule", league, exc)
            raise
        changed = moved = 0
        at = self.now_fn()
        for f in parsed:
            ins, prev = self.db.put_fixture(f, pid, at)
            changed += ins
            if ins and prev and prev["kickoff"] != f["kickoff"]:
                moved += 1
                for name, *_ in C.TARGETS:
                    if "captured" in self.db.target_status(f["fixture_uid"], name, prev["kickoff"]):
                        self.db.put_target(f["fixture_uid"], name, prev["kickoff"], "superseded", pid, at,
                                           f"kickoff moved to {f['kickoff']}")
        self.db.poll_event(pid, "finished", "schedule", league, at,
                           json.dumps({"fixtures": len(parsed), "changed": changed, "rescheduled": moved}))
        return {"fixtures": len(parsed), "changed": changed, "rescheduled": moved}

    def poll_odds(self, kind, league, fixtures):
        now = self.now_fn()
        pid = self._start(kind, league, now)
        rows = []
        try:
            ids = [f["provider_match_id"] for f in fixtures]
            for i in range(0, len(ids), C.BATCH):
                rows += list(self._pages(league, "odds", {"match_ids[]": ids[i:i + C.BATCH], "per_page": 100}))
        except provider.ProviderError as exc:
            self._fail(pid, kind, league, exc)
            raise
        at = self.now_fn()
        quotes = self.build_quotes(pid, kind, league, fixtures, rows, at)
        n = self.db.put_quotes(quotes)
        if kind != "regular":
            for f in fixtures:
                self.db.put_target(f["fixture_uid"], kind, f["kickoff"], "captured", pid, at)
        avail = sum(q["status"] == "available" for q in quotes)
        self.db.poll_event(pid, "finished", kind, league, at,
                           json.dumps({"fixtures": len(fixtures), "api_rows": len(rows), "quote_rows": n,
                                       "available": avail}))
        return {"fixtures": len(fixtures), "quote_rows": n, "available": avail}

    def build_quotes(self, pid, kind, league, fixtures, api_rows, at):
        by = defaultdict(dict)
        dup = set()
        for r, raw in api_rows:
            mid, v = int(r["match_id"]), r.get("vendor") or "?"
            if v in by[mid]:
                dup.add((mid, v))
            by[mid][v] = (r, raw)
        out = []
        for f in fixtures:
            uid, mid = f["fixture_uid"], f["provider_match_id"]
            ko = parse_utc(f["kickoff"])
            mins = (ko - at).total_seconds() / 60
            vendors = by.get(mid, {})
            for v in sorted(set(C.EXPECTED_VENUES) | self.db.seen_venues(uid) | set(vendors)):
                meta = C.VENUES.get(v, {"class": "unknown", "eligibility": "observational"})
                base = {"poll_id": pid, "snapshot_kind": kind, "fixture_uid": uid, "league": league,
                        "provider_match_id": mid, "venue": v, "venue_class": meta["class"],
                        "eligibility": meta["eligibility"], "market": C.MARKET, "retrieved_at": iso(at),
                        "kickoff_at_retrieval": f["kickoff"], "minutes_to_kickoff": round(mins, 3),
                        "source_updated_at": None, "american_odds": None, "decimal_odds": None, "raw_ref": None}
                flags0 = [] if v in C.VENUES else ["unknown_venue"]
                if v not in vendors:
                    out += [dict(base, selection=s, status="missing", flags=flags0 + ["absent_from_response"])
                            for s in C.SELECTIONS]
                    continue
                r, raw = vendors[v]
                flags0 = flags0 + (["duplicate_vendor_row"] if (mid, v) in dup else [])
                src = r.get("updated_at")
                if src:
                    try:
                        st = parse_utc(src)
                        if at - st > C.SOURCE_STALE:
                            flags0.append("source_stale")
                        if st - at > C.SOURCE_FUTURE_SKEW:
                            flags0.append("source_ts_in_future")
                        src = iso(st)
                    except ValueError:
                        flags0.append("source_ts_unparseable")
                        src = None
                else:
                    flags0.append("source_ts_missing")
                if f["status_state"] != "scheduled":
                    flags0.append(f"fixture_{f['status_state']}")
                post = at >= ko
                sel = []
                for s in C.SELECTIONS:
                    a = r.get(f"moneyline_{s}_odds")
                    q = dict(base, selection=s, source_updated_at=src, raw_ref=raw, american_odds=a, flags=list(flags0))
                    if post:
                        q.update(status="post_kickoff", decimal_odds=None)
                        q["flags"].append("retrieved_after_kickoff")
                    elif a is None:
                        q.update(status="suspended", american_odds=None)
                    else:
                        try:
                            d = american_to_decimal(a)
                            if abs(float(a)) >= C.DEGENERATE_AMERICAN:
                                q.update(status="invalid", decimal_odds=d)
                                q["flags"].append("degenerate_price")
                            else:
                                q.update(status="available", decimal_odds=d)
                        except InvalidOdds:
                            q.update(status="invalid", decimal_odds=None)
                            q["flags"].append("invalid_price")
                    if f["status_state"] != "scheduled" and q["status"] == "available":
                        q["status"] = "invalid"
                    sel.append(q)
                avail = [q for q in sel if q["status"] == "available"]
                if len(avail) == 3:
                    probs = validate_1x2([q["decimal_odds"] for q in avail])
                    if probs:
                        for q in avail:
                            q["status"] = "invalid"
                            q["flags"].append("book_incoherent")
                elif avail:
                    for q in avail:
                        q["flags"].append("partial_book")
                out += sel
        return out

    def _start(self, kind, league, now):
        self._poll_id = uuid.uuid4().hex
        self._endpoint = "matches" if kind == "schedule" else "odds"
        self.db.poll_event(self._poll_id, "started", kind, league, now)
        return self._poll_id

    def _fail(self, pid, kind, league, exc):
        self.db.poll_event(pid, "failed", kind, league, self.now_fn(), f"{type(exc).__name__}: {str(exc)[:200]}")

    # --------------------------------------------------------------- missed targets

    def mark_missed(self, now):
        first = self.db.c.execute("SELECT MIN(at) FROM poll_events").fetchone()[0]
        if not first:
            return 0
        first = parse_utc(first)
        n = 0
        for f in self.db.schedule():
            ko = parse_utc(f["kickoff"])
            for name, _m, lo, _hi in C.TARGETS:
                window_end = ko - timedelta(minutes=lo)
                if window_end < now and window_end > first and ko > now - timedelta(days=3):
                    st = self.db.target_status(f["fixture_uid"], name, f["kickoff"])
                    if "captured" not in st and "missed" not in st:
                        n += self.db.put_target(f["fixture_uid"], name, f["kickoff"], "missed", None, now,
                                                f"no successful poll in window (status {f['status_state']})")
        return n

    # --------------------------------------------------------------- run

    def run_once(self):
        start, t0 = self.now_fn(), self.monotonic()
        res = {"started": iso(start), "recovered": self.recover(start), "actions": []}
        for act in self.plan(start):
            kind, lg, fx = act
            if (self.monotonic() - t0) + self._cost(act) * self.spacing > self.budget:
                res["actions"].append({"kind": kind, "league": lg, "result": "skipped: run budget"})
                continue
            try:
                out = self.refresh_schedule(lg) if kind == "schedule" else self.poll_odds(kind, lg, fx)
                res["actions"].append({"kind": kind, "league": lg, "result": "ok", **out})
            except provider.AuthError as exc:
                res["actions"].append({"kind": kind, "league": lg, "result": f"AUTH FAILURE: {exc}"})
                res["auth_failure"] = True
                break
            except provider.ProviderError as exc:
                res["actions"].append({"kind": kind, "league": lg, "result": f"failed: {type(exc).__name__}"})
        res["missed_marked"] = self.mark_missed(self.now_fn())
        res["finished"] = iso(self.now_fn())
        with self.db.tx() as c:
            c.execute("INSERT INTO runs (started_at, finished_at, summary) VALUES (?,?,?)",
                      (res["started"], res["finished"], json.dumps(res)))
        return res


def main(db_path=None, client=None):
    """Entry point for the scheduled task. Exit 0 ok/locked, 1 provider/auth failure, 2 no key."""
    db = DB(db_path)
    try:
        if client is None:
            key = load_key()
            if not key:
                print("no BallDontLie key configured (BALLDONTLIE_KEY or %LOCALAPPDATA%\\soccer-betting-bot\\bdl.key)")
                return 2
            client = provider.Client(key, sleep=time.sleep)
        try:
            with RunLock(db.path.parent / "logger.lock") as lock:
                res = Runner(db, client).run_once()
                res["broke_stale_lock"] = lock.broke_stale
        except Locked as exc:
            print(f"skipped: {exc}")
            return 0
    finally:
        db.close()
    for a in res["actions"]:
        print(f"  {a['kind']:8s} {a['league']:7s} {a['result']}  "
              f"{ {k: v for k, v in a.items() if k not in ('kind', 'league', 'result')} or ''}")
    print(f"run {res['started']} -> {res['finished']}: {len(res['actions'])} actions, "
          f"{res['recovered']} interrupted polls recovered, {res['missed_marked']} targets marked missed")
    return 1 if res.get("auth_failure") or any(a["result"].startswith("failed") for a in res["actions"]) else 0
