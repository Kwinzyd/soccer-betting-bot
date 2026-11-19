"""EXP-2026-11-B analysis, guarded by the protocol lock.

Three analyses are kept strictly separate, each with its own function:

  price_drift()    same venue, same selection: early price vs that venue's
                   own last pre-kickoff price. No selection rule; every
                   selection of every fixture. Describes price movement only.
  closing_line()   one entry per fixture chosen by `select_entry`, compared
                   with an independent closing reference. The primary
                   endpoint (closing EV) lives here.
  simulated_roi()  the same entries settled on 90-minute results at the
                   entry price, flat 1 unit. Secondary; never a gate.

All three refuse unless registry/experiments/EXP-2026-11-B.json is locked
and its approved venues are verified. Only quotes retrieved at or after
`locked_at` for fixtures kicking off after it are used: pilot data is
excluded by construction. `data_quality()` may run at any time and contains
no comparison between prices at different times.
"""

import json
from collections import Counter
from datetime import timedelta

import numpy as np

from .. import metrics
from ..clv import selection_clv
from ..odds import fair_from_decimals, settle_pnl
from ..paths import REGISTRY_DIR
from ..settlement import grade_1x2
from ..timeutil import iso, parse_utc, utcnow
from . import config as C

PROTOCOL = REGISTRY_DIR / "experiments" / "EXP-2026-11-B.json"
BLOCKING_FLAGS = {"source_stale", "source_ts_in_future", "source_ts_unparseable", "partial_book",
                  "duplicate_vendor_row", "unknown_venue", "cross_venue_discrepancy"}
VOID_STATUS = {"STATUS_POSTPONED", "STATUS_CANCELED", "STATUS_CANCELLED", "STATUS_ABANDONED"}
CLOSE_MISMATCH = 0.10      # max |fair_external - fair_venue_close| per outcome before quarantine


class ProtocolNotLocked(RuntimeError):
    pass


def load_protocol(path=PROTOCOL):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _guard(p):
    if p.get("status") != "locked" or not p.get("locked_at"):
        raise ProtocolNotLocked("EXP-2026-11-B is not locked; only data_quality() may run")
    venues = p.get("approved_entry_venues") or []
    ver = p.get("venue_verification", {})
    unverified = [v for v in venues if ver.get(v, {}).get("status") != "verified"]
    if not venues or unverified:
        raise ProtocolNotLocked(f"no verified entry venues (unverified: {unverified or 'none approved'})")
    return parse_utc(p["locked_at"])


# ------------------------------------------------------------------ helpers

def books_at(db, uid, venues, lo, hi, not_before):
    """Latest complete, unflagged, available book per venue retrieved in
    [max(lo, not_before), hi]. Returns {venue: {"home","draw","away","_kickoff_at_retrieval","_t"}}."""
    lo = max(lo, not_before)
    if lo > hi:
        return {}
    rows = db.c.execute(f"""SELECT * FROM quotes WHERE fixture_uid=? AND retrieved_at>=? AND retrieved_at<=?
        AND venue IN ({','.join('?' * len(venues))}) ORDER BY retrieved_at""",  # noqa: S608 (placeholders only)
                        (uid, iso(lo), iso(hi), *venues)).fetchall()
    polls = {}
    for r in rows:
        polls.setdefault((r["venue"], r["poll_id"]), []).append(dict(r))
    best = {}
    for (v, _pid), qs in polls.items():
        if len(qs) != 3 or any(q["status"] != "available" or set(json.loads(q["flags"])) & BLOCKING_FLAGS
                               for q in qs):
            continue
        t = qs[0]["retrieved_at"]
        if v not in best or t > best[v]["_t"]:
            px = {q["selection"]: q["decimal_odds"] for q in qs}
            px.update(_t=t, _kickoff_at_retrieval=qs[0]["kickoff_at_retrieval"])
            best[v] = px
    return best


def consensus_fair(books):
    return np.mean([fair_from_decimals([b[s] for s in C.SELECTIONS]) for b in books.values()], axis=0)


def select_entry(entry_books, threshold=None):
    """THE selection function. Inputs: only the approved venues' books observed
    in the entry window (nothing later, no outcomes, no model).

    For each selection s: best_s = max over venues of the price; fair_s = mean
    over venues of the power-de-vigged probability; score_s = best_s * fair_s - 1.
    Returns {selection, price, fair_at_entry, venue, score} for the highest
    score, ties broken by the fixed order home > draw > away and then venue
    name, or None if `threshold` is set and the best score does not exceed it.
    Deterministic.
    """
    if not entry_books:
        return None
    fair = consensus_fair(entry_books)
    scored = []
    for i, s in enumerate(C.SELECTIONS):
        venue, price = max(((v, b[s]) for v, b in sorted(entry_books.items())), key=lambda vp: (vp[1], vp[0]))
        scored.append((price * fair[i] - 1, -i, s, price, float(fair[i]), venue))
    score, _, s, price, f, venue = max(scored)
    if threshold is not None and score <= threshold:
        return None
    return {"selection": s, "price": price, "fair_at_entry": f, "venue": venue, "score": score}


def _fixtures(db, start, now):
    for f in sorted(db.schedule(), key=lambda f: (f["kickoff"], f["fixture_uid"])):
        k = parse_utc(f["kickoff"])
        if start <= k <= now - timedelta(hours=2):
            yield f, k


def _entries(db, p, now):
    start = _guard(p)
    venues = p["approved_entry_venues"]
    off, tol = timedelta(hours=p["entry_offset_hours"]), timedelta(hours=p["entry_tolerance_hours"])
    excl, out = Counter(), []
    for f, k in _fixtures(db, start, now):
        if f["status"] in VOID_STATUS:
            excl["void_status"] += 1
            continue
        e = k - off
        if e < start:
            excl["entry_before_lock"] += 1
            continue
        books = books_at(db, f["fixture_uid"], venues, e - tol, e, start)
        if not books:
            excl["no_entry_snapshot"] += 1
            continue
        moved = max(abs(k - parse_utc(b["_kickoff_at_retrieval"])) for b in books.values())
        if moved > timedelta(hours=p["void_if_kickoff_moved_hours"]):
            excl["void_kickoff_moved"] += 1
            continue
        pick = select_entry(books, p.get("threshold"))
        if pick is None:
            excl["below_threshold"] += 1
            continue
        y, w, _ = k.isocalendar()
        out.append(dict(pick, fixture=f["fixture_uid"], fixture_row=f, kickoff=k, week=y * 100 + w))
    return out, excl


def _summ(vals, rows, p, name="value"):
    v = np.asarray(vals, dtype=float)
    fx = metrics.bootstrap_mean(v, [r["fixture"] for r in rows], p["n_boot"], p["seed"])
    wk = metrics.bootstrap_mean(v, [r["week"] for r in rows], p["n_boot"], p["seed"])
    return {"n": len(v), name: fx, f"{name}_week_blocks": wk,
            "verdict": metrics.verdict(fx, len(v), p["min_n"], better="higher") if len(v) else "inconclusive"}


# ---------------------------------------------------------------- analyses

def closing_line(db, protocol=None, now=None):
    p = protocol or load_protocol()
    now = now or utcnow()
    start = _guard(p)
    entries, excl = _entries(db, p, now)
    cw = timedelta(minutes=p["close_window_minutes"])
    rows = {"strong": [], "weak": []}
    for e in entries:
        i = C.SELECTIONS.index(e["selection"])
        ext = db.c.execute("SELECT home, draw, away FROM closing_refs WHERE fixture_uid=? AND venue='betfair_exchange'",
                           (e["fixture"],)).fetchone()
        cb = books_at(db, e["fixture"], p["approved_entry_venues"], e["kickoff"] - cw,
                      e["kickoff"] - timedelta(microseconds=1), start)
        fc_weak = consensus_fair(cb) if cb else None
        if ext:
            px = [ext[0], ext[1], ext[2]]
            fc = fair_from_decimals(px)
            if fc_weak is not None and np.max(np.abs(fc - fc_weak)) > CLOSE_MISMATCH:
                excl["closing_reference_mismatch"] += 1          # likely mis-join or stale; quarantined
            else:
                rows["strong"].append(dict(selection_clv(e["price"], e["fair_at_entry"], px[i], fc[i]), **e))
        if cb:
            rows["weak"].append(dict(selection_clv(e["price"], e["fair_at_entry"],
                                                   max(b[e["selection"]] for b in cb.values()), fc_weak[i]), **e))
        if not ext and not cb:
            excl["no_closing_reference"] += 1
    out = {"analysis": "closing_line", "entries": len(entries), "exclusions": dict(excl), "locked_at": p["locked_at"]}
    for name, rs in rows.items():
        s = _summ([r["ev_vs_close_fair"] for r in rs], rs, p, "ev_vs_close_fair") if rs else {"n": 0, "verdict": "inconclusive"}
        if rs:
            s["pct_beat_close"] = float(np.mean([r["beat_close"] for r in rs]))
        out["strong (primary)" if name == "strong" else "weak (EXP-2026-11-B-weak)"] = s
    return out


def price_drift(db, protocol=None, now=None):
    """Every selection of every eligible fixture, per venue: early price vs the
    same venue's last pre-kickoff price. Clustered by fixture."""
    p = protocol or load_protocol()
    now = now or utcnow()
    start = _guard(p)
    off, tol = timedelta(hours=p["entry_offset_hours"]), timedelta(hours=p["entry_tolerance_hours"])
    cw = timedelta(minutes=p["close_window_minutes"])
    rows, excl = [], Counter()
    for f, k in _fixtures(db, start, now):
        early = books_at(db, f["fixture_uid"], p["approved_entry_venues"], k - off - tol, k - off, start)
        late = books_at(db, f["fixture_uid"], p["approved_entry_venues"], k - cw, k - timedelta(microseconds=1), start)
        common = sorted(set(early) & set(late))
        if not common:
            excl["no_matching_early_and_late_books"] += 1
            continue
        y, w, _ = k.isocalendar()
        for v in common:
            for s in C.SELECTIONS:
                rows.append({"fixture": f["fixture_uid"], "week": y * 100 + w, "venue": v, "selection": s,
                             "log_ratio": float(np.log(early[v][s] / late[v][s]))})
    out = {"analysis": "price_drift", "exclusions": dict(excl)}
    out.update(_summ([r["log_ratio"] for r in rows], rows, p, "mean_log_price_ratio_early_over_late") if rows
               else {"n": 0, "verdict": "inconclusive"})
    return out


def simulated_roi(db, protocol=None, now=None):
    """Secondary only. Settles closing_line entries on 90-minute results at the
    entry price, flat 1 unit. Never a promotion or lock criterion."""
    p = protocol or load_protocol()
    now = now or utcnow()
    entries, excl = _entries(db, p, now)
    pnl, rows = [], []
    for e in entries:
        f = e["fixture_row"]
        state, outcome, _ = grade_1x2(e["selection"], {"status": f["status"], "status_state": f["status_state"],
                                                       "home_score": f["home_score"], "away_score": f["away_score"],
                                                       "date": f["kickoff"]})
        if state not in ("settled", "void"):
            excl[f"unsettled_{state}"] += 1
            continue
        pnl.append(settle_pnl(1.0, e["price"], outcome))
        rows.append(e)
    out = {"analysis": "simulated_roi (secondary)", "exclusions": dict(excl), "n": len(pnl)}
    if pnl:
        out["roi"] = metrics.bootstrap_ratio(pnl, np.ones(len(pnl)), [r["fixture"] for r in rows],
                                             p["n_boot"], p["seed"])
        out["max_drawdown_units"] = metrics.max_drawdown(pnl)
    return out


def data_quality(db, now=None):
    now = now or utcnow()
    sched = db.schedule()
    past = [f for f in sched if parse_utc(f["kickoff"]) <= now]
    tgt = Counter((r[0], r[1]) for r in db.c.execute("SELECT target, status FROM targets"))
    venue = Counter()
    for r in db.c.execute("SELECT venue, status FROM quotes"):
        venue[(r[0], r[1])] += 1
    flags = Counter()
    for r in db.c.execute("SELECT flags FROM quotes"):
        flags.update(json.loads(r[0]))
    closing = db.c.execute("SELECT count(DISTINCT fixture_uid) FROM closing_refs").fetchone()[0]
    quarantine = Counter(r[0] for r in db.c.execute("SELECT reason FROM closing_quarantine"))
    return {"fixtures_in_schedule": len(sched), "fixtures_kicked_off": len(past),
            "targets": {f"{k[0]}:{k[1]}": v for k, v in sorted(tgt.items())},
            "quote_status_by_venue": {f"{k[0]}:{k[1]}": v for k, v in sorted(venue.items())},
            "quote_flags": dict(flags), "fixtures_with_external_close": closing,
            "closing_quarantine": dict(quarantine),
            "note": "data-quality only; no endpoint statistics before the protocol is locked"}


def primary(db, protocol=None, now=None):
    """The primary analysis is closing_line()."""
    return closing_line(db, protocol, now)
