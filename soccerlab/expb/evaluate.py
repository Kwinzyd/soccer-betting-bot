"""EXP-2026-11-B analysis, guarded by the protocol lock.

* `data_quality()` may run at any time. It reports coverage, freshness and
  quality only -- no entry-vs-close comparison of any kind.
* `primary()` refuses to run unless registry/experiments/EXP-2026-11-B.json
  has status 'locked'; it then uses only fixtures kicking off at or after
  `locked_at`. Quotes logged before the lock are pilot data for the
  data-quality acceptance criteria and are never used for the endpoint.

Entry rule (model-free, one entry per fixture, fixed in the protocol):
  at E = kickoff - entry_offset, take each approved venue's latest complete,
  unflagged book retrieved in [E - tolerance, E]; reference fair = mean of
  the venues' power-de-vigged probabilities; for each selection the best
  approved price; pick the single selection with the highest
  best_price * fair - 1; enter (always, if `threshold` is null; otherwise only
  if that exceeds `threshold`).
Endpoint: closing EV = entry_price * fair_close(selection) - 1, against
  (a) the external closing reference (strong) and (b) the approved venues'
  last complete pre-kickoff books within `close_window_minutes` (weak).
"""

import json
from collections import Counter
from datetime import timedelta

import numpy as np

from .. import metrics
from ..clv import selection_clv
from ..odds import fair_from_decimals
from ..paths import REGISTRY_DIR
from ..timeutil import parse_utc, utcnow
from . import config as C

PROTOCOL = REGISTRY_DIR / "experiments" / "EXP-2026-11-B.json"
BLOCKING_FLAGS = {"source_stale", "source_ts_in_future", "source_ts_unparseable", "partial_book",
                  "duplicate_vendor_row", "unknown_venue"}
VOID_STATUS = {"STATUS_POSTPONED", "STATUS_CANCELED", "STATUS_CANCELLED", "STATUS_ABANDONED"}


class ProtocolNotLocked(RuntimeError):
    pass


def load_protocol(path=PROTOCOL):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _books(db, uid, venues, lo, hi):
    """Latest complete unflagged available book per venue with lo <= retrieved_at <= hi (strings, iso)."""
    rows = db.c.execute(f"""SELECT * FROM quotes WHERE fixture_uid=? AND retrieved_at>=? AND retrieved_at<=?
        AND venue IN ({','.join('?' * len(venues))}) ORDER BY retrieved_at""",  # noqa: S608 (placeholders only)
                        (uid, lo, hi, *venues)).fetchall()
    polls = {}
    for r in rows:
        polls.setdefault((r["venue"], r["poll_id"]), []).append(dict(r))
    best = {}
    for (v, _pid), qs in polls.items():
        if len(qs) != 3 or any(q["status"] != "available" or set(json.loads(q["flags"])) & BLOCKING_FLAGS
                               for q in qs):
            continue
        t = qs[0]["retrieved_at"]
        if v not in best or t > best[v][0]:
            px = {q["selection"]: q["decimal_odds"] for q in qs}
            px["_kickoff_at_retrieval"] = qs[0]["kickoff_at_retrieval"]
            best[v] = (t, px)
    return {v: b[1] for v, b in best.items()}


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def data_quality(db, now=None):
    now = now or utcnow()
    sched = db.schedule()
    past = [f for f in sched if parse_utc(f["kickoff"]) <= now]
    tgt = Counter((r[0], r[1]) for r in db.c.execute("SELECT target, status FROM targets"))
    venue = Counter()
    for r in db.c.execute("SELECT venue, status FROM quotes"):
        venue[(r[0], r[1])] += 1
    closing = db.c.execute("SELECT count(DISTINCT fixture_uid) FROM closing_refs").fetchone()[0]
    return {"fixtures_in_schedule": len(sched), "fixtures_kicked_off": len(past),
            "targets": {f"{k[0]}:{k[1]}": v for k, v in sorted(tgt.items())},
            "quote_status_by_venue": {f"{k[0]}:{k[1]}": v for k, v in sorted(venue.items())},
            "fixtures_with_external_close": closing,
            "note": "data-quality only; endpoint statistics are unavailable until the protocol is locked"}


def primary(db, protocol=None, now=None):
    p = protocol or load_protocol()
    if p.get("status") != "locked" or not p.get("locked_at"):
        raise ProtocolNotLocked("EXP-2026-11-B is not locked; only data_quality() may run")
    now = now or utcnow()
    start = parse_utc(p["locked_at"])
    venues = p["approved_entry_venues"]
    off = timedelta(hours=p["entry_offset_hours"])
    tol = timedelta(hours=p["entry_tolerance_hours"])
    cw = timedelta(minutes=p["close_window_minutes"])
    thr = p["threshold"]
    excl, rows = Counter(), {"strong": [], "weak": []}
    for f in sorted(db.schedule(), key=lambda f: (f["kickoff"], f["fixture_uid"])):
        k = parse_utc(f["kickoff"])
        if k < start or k > now - timedelta(hours=2):
            continue
        if f["status"] in VOID_STATUS:
            excl["void_status"] += 1
            continue
        e = k - off
        if e < start:
            excl["entry_before_lock"] += 1
            continue
        books = _books(db, f["fixture_uid"], venues, _iso(e - tol), _iso(e))
        if not books:
            excl["no_entry_snapshot"] += 1
            continue
        moved = max(abs(k - parse_utc(b["_kickoff_at_retrieval"])) for b in books.values())
        if moved > timedelta(hours=p["void_if_kickoff_moved_hours"]):
            excl["void_kickoff_moved"] += 1
            continue
        fair_e = np.mean([fair_from_decimals([b[s] for s in C.SELECTIONS]) for b in books.values()], axis=0)
        best = {s: max(b[s] for b in books.values()) for s in C.SELECTIONS}
        ev = {s: best[s] * fair_e[i] - 1 for i, s in enumerate(C.SELECTIONS)}
        sel = max(C.SELECTIONS, key=lambda s: (ev[s], s))
        if thr is not None and ev[sel] <= thr:
            excl["below_threshold"] += 1
            continue
        i = C.SELECTIONS.index(sel)
        refs = {}
        ext = db.c.execute("SELECT home, draw, away FROM closing_refs WHERE fixture_uid=? AND venue='betfair_exchange'",
                           (f["fixture_uid"],)).fetchone()
        if ext:
            px = [ext[0], ext[1], ext[2]]
            refs["strong"] = (px[i], fair_from_decimals(px)[i])
        cb = _books(db, f["fixture_uid"], venues, _iso(k - cw), _iso(k - timedelta(microseconds=1)))
        if cb:
            fc = np.mean([fair_from_decimals([b[s] for s in C.SELECTIONS]) for b in cb.values()], axis=0)
            refs["weak"] = (max(b[sel] for b in cb.values()), fc[i])
        if not refs:
            excl["no_closing_reference"] += 1
        y, w, _ = k.isocalendar()
        for name, (close_price, close_fair) in refs.items():
            r = selection_clv(best[sel], fair_e[i], close_price, close_fair)
            rows[name].append(dict(r, fixture=f["fixture_uid"], week=y * 100 + w, selection=sel))
    out = {"exclusions": dict(excl), "locked_at": p["locked_at"], "min_n": p["min_n"]}
    for name, rs in rows.items():
        if not rs:
            out[name] = {"n": 0, "verdict": "inconclusive (no entries)"}
            continue
        v = np.array([r["ev_vs_close_fair"] for r in rs])
        fx = metrics.bootstrap_mean(v, [r["fixture"] for r in rs], p["n_boot"], p["seed"])
        wk = metrics.bootstrap_mean(v, [r["week"] for r in rs], p["n_boot"], p["seed"])
        out[name] = {"n": len(rs), "ev_vs_close_fair": fx, "ev_vs_close_fair_week_blocks": wk,
                     "pct_beat_close": float(np.mean([r["beat_close"] for r in rs])),
                     "odds_ratio_mean": float(np.mean([r["odds_ratio"] for r in rs])),
                     "verdict": metrics.verdict(fx, len(rs), p["min_n"], better="higher")}
    return out
