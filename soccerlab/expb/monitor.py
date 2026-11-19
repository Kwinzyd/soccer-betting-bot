"""Read-only monitoring of the EXP-2026-11-B logger.

    python -m soccerlab expb monitor      exit 0 green/amber, 1 red

Reports liveness, poll freshness per league, failures, T-60/T-15 capture
rates, candidate-venue availability at T-15, quote quality and the age of the
last pre-kickoff quote (how stale a "closing proxy" would be). It contains no
endpoint statistics: nothing here compares entry and closing prices.
"""

import json
import statistics
from collections import Counter, defaultdict
from datetime import timedelta

from ..timeutil import parse_utc, utcnow
from . import config as C


def check(db, now=None):
    now = now or utcnow()
    out = []

    def add(level, name, detail):
        out.append((level, name, detail))

    last_run = db.c.execute("SELECT finished_at FROM runs ORDER BY id DESC LIMIT 1").fetchone()
    if not last_run:
        add("red", "logger liveness", "no runs recorded")
    else:
        age = now - parse_utc(last_run[0])
        add("red" if age > timedelta(minutes=20) else "green", "logger liveness", f"last run {age} ago")

    sched = db.schedule()
    by_lg = defaultdict(list)
    for f in sched:
        by_lg[f["league"]].append(f)
    for lg in C.LEAGUES:
        fx = by_lg.get(lg, [])
        upcoming = [f for f in fx if f["status_state"] == "scheduled"
                    and now < parse_utc(f["kickoff"]) <= now + C.LOOKAHEAD]
        s = db.last_poll("schedule", lg)
        r = db.last_poll("regular", lg)
        s_age = now - parse_utc(s) if s else None
        r_age = now - parse_utc(r) if r else None
        lvl = "green"
        if s is None or s_age > 2 * C.SCHEDULE_REFRESH:
            lvl = "amber"
        if upcoming and (r is None or r_age > C.REGULAR_INTERVAL + timedelta(hours=1)):
            lvl = "red"
        add(lvl, f"{lg} polling", f"schedule age {s_age}, regular-poll age {r_age}, {len(upcoming)} fixtures in next 7d")

    day = now - timedelta(hours=24)
    ev = Counter((r["kind"], r["event"]) for r in db.c.execute(
        "SELECT kind, event FROM poll_events WHERE at >= ?", (day.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),)))
    auth = db.c.execute("SELECT count(*) FROM poll_events WHERE event='failed' AND detail LIKE 'AuthError%' AND at>=?",
                        (day.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),)).fetchone()[0]
    fails = sum(v for (k, e), v in ev.items() if e in ("failed", "interrupted"))
    add("red" if auth else ("amber" if fails else "green"), "poll failures (24h)",
        f"{dict(ev)}; auth failures {auth}")

    t = defaultdict(Counter)
    for r in db.c.execute("""SELECT target, status FROM targets t WHERE recorded_at >= ?""",
                          ((now - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),)):
        t[r[0]][r[1]] += 1
    for name, *_ in C.TARGETS:
        c = t.get(name, Counter())
        tot = c["captured"] + c["missed"]
        rate = c["captured"] / tot if tot else None
        add("green" if rate is None or rate >= 0.95 else ("amber" if rate >= 0.8 else "red"),
            f"{name} capture (7d)", f"{dict(c)}" + (f", rate {rate:.1%}" if rate is not None else ""))

    avail = defaultdict(Counter)
    for r in db.c.execute("""SELECT venue, fixture_uid, MAX(status='available') ok FROM quotes
                             WHERE snapshot_kind='T-15' AND retrieved_at >= ? GROUP BY venue, fixture_uid""",
                          ((now - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),)):
        avail[r[0]]["fixtures"] += 1
        avail[r[0]]["available"] += r[2]
    for v, c in sorted(avail.items()):
        rate = c["available"] / c["fixtures"]
        lvl = "green" if rate >= 0.9 else "amber"
        if C.VENUES.get(v, {}).get("eligibility") != "candidate_unverified":
            lvl = "green"            # observational venues: informational only
        add(lvl, f"{v} available at T-15 (7d)", f"{c['available']}/{c['fixtures']} fixtures ({rate:.0%})")

    st = Counter()
    flags = Counter()
    for r in db.c.execute("SELECT status, flags FROM quotes WHERE retrieved_at >= ?",
                          (day.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),)):
        st[r[0]] += 1
        flags.update(json.loads(r[1]))
    tot = sum(st.values())
    add("green" if not tot or st["available"] / tot >= 0.6 else "amber", "quote status (24h)",
        f"{dict(st)}; flags {dict(flags.most_common(6))}")

    moved = db.c.execute("""SELECT count(*) FROM (SELECT fixture_uid FROM fixture_obs
                            GROUP BY fixture_uid HAVING COUNT(DISTINCT kickoff) > 1)""").fetchone()[0]
    add("green", "rescheduled fixtures (all time)", str(moved))

    gaps = []
    for f in sched:
        ko = parse_utc(f["kickoff"])
        if now - timedelta(days=7) <= ko <= now:
            r = db.c.execute("""SELECT MAX(retrieved_at) FROM quotes WHERE fixture_uid=? AND status='available'
                                AND eligibility='candidate_unverified' AND retrieved_at < ?""",
                             (f["fixture_uid"], f["kickoff"])).fetchone()[0]
            if r:
                gaps.append((ko - parse_utc(r)).total_seconds() / 60)
    if gaps:
        add("green" if statistics.median(gaps) <= 20 else "amber", "last pre-kickoff candidate quote (7d)",
            f"median {statistics.median(gaps):.0f} min before kickoff, max {max(gaps):.0f} min, n={len(gaps)}")
    worst = "red" if any(x[0] == "red" for x in out) else ("amber" if any(x[0] == "amber" for x in out) else "green")
    return worst, out


def main(db):
    worst, out = check(db)
    for level, name, detail in out:
        print(f"  [{level.upper():5s}] {name}: {detail}")
    print(f"EXP-B LOGGER {worst.upper()}")
    return 1 if worst == "red" else 0
