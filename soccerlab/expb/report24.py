"""Read-only live-pilot monitoring report (e.g. the first 24 hours).

    python -m soccerlab expb report --db D:\\soccerlab-research\\expb\\exp_b_odds.sqlite3 --hours 24

Opens the database with mode=ro: it never writes to the live store. Counts
are taken over [pilot start, pilot start + hours]. Limitations are stated in
the output rather than hidden:

* API request counts come from stored raw pages; identical repeated responses
  (e.g. an unchanged schedule page or repeated 429 bodies) are stored once,
  so counts are lower bounds.
* Skipped invocations (lock held, store unavailable) leave no row; they are
  inferred from gaps between runs.
"""

import json
import os
import shutil
import sqlite3
import statistics
from collections import Counter, defaultdict
from datetime import timedelta
from pathlib import Path

from ..timeutil import iso, parse_utc, utcnow
from . import config as C

SLOT = timedelta(minutes=5)


def _q(c, sql, *a):
    return [dict(r) for r in c.execute(sql, a)]


def build(db_path, start=None, hours=24, now=None):
    db_path = Path(db_path)
    now = now or utcnow()
    c = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    runs = _q(c, "SELECT * FROM runs ORDER BY id")
    start = start or (parse_utc(runs[0]["started_at"]) if runs else now)
    end = min(start + timedelta(hours=hours), now)
    lo, hi = iso(start), iso(end)
    complete = now >= start + timedelta(hours=hours)
    R = {"window": [lo, hi], "complete_window": complete, "generated_at": iso(now)}

    # ---- scheduled runs
    rs = [r for r in runs if lo <= r["started_at"] <= hi]
    acts = Counter()
    durations, delays, overlaps, gaps = [], [], 0, []
    prev_end = None
    for r in rs:
        s, e = parse_utc(r["started_at"]), parse_utc(r["finished_at"])
        durations.append((e - s).total_seconds())
        slot = s.replace(second=0, microsecond=0) - timedelta(minutes=(s.minute - 2) % 5)   # task fires at :02, :07, ...
        delays.append((s - slot).total_seconds())
        if prev_end and s < prev_end:
            overlaps += 1
        if prev_end and s - prev_end > SLOT + timedelta(minutes=2):
            gaps.append((iso(prev_end), iso(s), round((s - prev_end).total_seconds() / 60, 1)))
        prev_end = e
        for a in json.loads(r["summary"])["actions"]:
            acts[(a["kind"], a["result"].split(":")[0])] += 1
    expected = int((end - start) / SLOT)
    R["runs"] = {"recorded": len(rs), "approx_expected_5min_slots": expected,
                 "actions": {f"{k}:{v}": n for (k, v), n in sorted(acts.items())},
                 "duration_s": _dist(durations), "start_delay_s_after_slot": _dist(delays),
                 "overlapping_runs": overlaps, "gaps_over_7min": gaps[:50], "n_gaps": len(gaps)}

    # ---- API requests / rate limits / auth
    raw = _q(c, "SELECT endpoint, http_status, count(*) n FROM raw_responses WHERE retrieved_at BETWEEN ? AND ? "
                "GROUP BY 1,2", lo, hi)
    ev = _q(c, "SELECT kind, event, detail FROM poll_events WHERE at BETWEEN ? AND ?", lo, hi)
    R["api"] = {"stored_pages_by_endpoint_status (lower bound)": {f"{r['endpoint']}:{r['http_status']}": r["n"] for r in raw},
                "rate_limit_429_pages (lower bound)": sum(r["n"] for r in raw if r["http_status"] == 429),
                "poll_events": dict(Counter(f"{e['kind']}:{e['event']}" for e in ev)),
                "auth_errors": sum(1 for e in ev if e["event"] == "failed" and (e["detail"] or "").startswith("AuthError")),
                "failed_detail": dict(Counter((e["detail"] or "")[:60] for e in ev if e["event"] == "failed")),
                "interrupted_polls": sum(1 for e in ev if e["event"] == "interrupted")}

    # ---- quotes: status, freshness, coverage
    qs = _q(c, "SELECT venue, status, flags, retrieved_at, source_updated_at, poll_id, fixture_uid, snapshot_kind "
               "FROM quotes WHERE retrieved_at BETWEEN ? AND ?", lo, hi)
    st = defaultdict(Counter)
    fresh = defaultdict(list)
    flags = Counter()
    books = defaultdict(lambda: defaultdict(int))
    for q in qs:
        st[q["venue"]][q["status"]] += 1
        flags.update(json.loads(q["flags"]))
        if q["status"] == "available" and q["source_updated_at"]:
            fresh[q["venue"]].append((parse_utc(q["retrieved_at"]) - parse_utc(q["source_updated_at"])).total_seconds() / 60)
        books[(q["poll_id"], q["fixture_uid"])][q["venue"]] += q["status"] == "available"
    cov = Counter()
    for _k, venues in books.items():
        cov["fixture_polls"] += 1
        for v in C.VENUES:
            cov[v] += venues.get(v, 0) == 3
    R["quotes"] = {"rows": len(qs), "status_by_venue": {v: dict(s) for v, s in st.items()},
                   "flags": dict(flags.most_common()),
                   "source_age_min_by_venue": {v: _dist(x) for v, x in fresh.items()},
                   "complete_book_coverage": {v: (round(cov[v] / cov["fixture_polls"], 4) if cov["fixture_polls"] else None)
                                              for v in C.VENUES},
                   "fixture_polls": cov["fixture_polls"]}

    # ---- targeted snapshots
    sched = _q(c, """SELECT f.* FROM fixture_obs f JOIN (SELECT fixture_uid, MAX(id) mx FROM fixture_obs
                     GROUP BY fixture_uid) l ON f.id = l.mx""")
    tg = defaultdict(Counter)
    elig = Counter()
    for f in sched:
        ko = parse_utc(f["kickoff"])
        for name, _m, lo_m, _hi_m in C.TARGETS:
            win_end = ko - timedelta(minutes=lo_m)
            if start < win_end <= end:
                elig[name] += 1
                stat = {r["status"] for r in _q(c, "SELECT status FROM targets WHERE fixture_uid=? AND target=? "
                                                   "AND kickoff=?", f["fixture_uid"], name, f["kickoff"])}
                tg[name]["captured" if "captured" in stat else ("missed" if "missed" in stat else "unrecorded")] += 1
    R["snapshots"] = {name: {"windows_closed_in_period": elig[name], **dict(tg[name])} for name, *_ in C.TARGETS}
    R["snapshots"]["note"] = ("no eligible fixture has passed a T-60/T-15 window yet" if not sum(elig.values())
                              else "counts cover fixtures whose observation window closed inside the period")
    R["superseded_targets"] = c.execute("SELECT count(*) FROM targets WHERE status='superseded'").fetchone()[0]
    R["rescheduled_fixtures"] = c.execute("""SELECT count(*) FROM (SELECT fixture_uid FROM fixture_obs
                                             GROUP BY fixture_uid HAVING COUNT(DISTINCT kickoff) > 1)""").fetchone()[0]

    # ---- storage / integrity
    R["integrity"] = {"quick_check": c.execute("PRAGMA quick_check").fetchone()[0],
                      "journal_mode": c.execute("PRAGMA journal_mode").fetchone()[0]}
    c.close()
    files = {p.name: p.stat().st_size for p in db_path.parent.glob(db_path.name + "*")}
    size = sum(files.values())
    elapsed_days = (end - start).total_seconds() / 86400
    free = shutil.disk_usage(db_path.parent).free
    R["storage"] = {"files": files, "total_bytes": size, "free_bytes": free,
                    "marker_present": (db_path.parent / ".soccerlab-expb-store").is_file()}
    if elapsed_days >= 0.25:
        # the first run loads the whole season schedule; growth after that is what scales
        per_day = size / elapsed_days
        R["storage"].update(bytes_per_day_upper_bound=int(per_day),
                            projected_bytes_at_pilot_end_14d_upper_bound=int(per_day * 14),
                            projected_bytes_to_2027_06_01_upper_bound=int(
                                per_day * max(0, (parse_utc("2027-06-01T00:00:00Z") - start).days)))
    else:
        R["storage"]["projection"] = "withheld: < 6h elapsed (initial schedule load would dominate the rate)"
    return R


def _dist(xs):
    if not xs:
        return None
    xs = sorted(xs)
    return {"n": len(xs), "median": round(statistics.median(xs), 2), "p90": round(xs[int(0.9 * (len(xs) - 1))], 2),
            "max": round(xs[-1], 2)}


def volume_health(path):
    """Windows volume health via Get-Volume (read-only). None elsewhere."""
    if os.name != "nt":
        return None
    import subprocess
    drive = os.path.splitdrive(str(Path(path).resolve()))[0].rstrip(":")
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command",
                              f"Get-Volume -DriveLetter {drive} | Select-Object FileSystem,HealthStatus,"
                              f"OperationalStatus,SizeRemaining | ConvertTo-Json"],
                             capture_output=True, text=True, timeout=60)
        return json.loads(out.stdout) if out.stdout.strip() else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def render(R, health=None):
    L = [f"# EXP-2026-11-B live pilot report  {R['window'][0]} -> {R['window'][1]}",
         f"window complete: {R['complete_window']} (generated {R['generated_at']})", ""]
    for k in ("runs", "api", "quotes", "snapshots", "integrity", "storage"):
        L.append(f"## {k}")
        L.append("```json")
        L.append(json.dumps(R[k], indent=1, default=str))
        L.append("```")
    L.append(f"rescheduled fixtures: {R['rescheduled_fixtures']}; superseded targets: {R['superseded_targets']}")
    if health is not None:
        L += ["", "## store volume health", "```json", json.dumps(health, indent=1), "```"]
    L += ["", "Notes: API counts are lower bounds (identical responses are stored once); skipped invocations are "
          "inferred from gaps; snapshot success is claimed only for windows that actually closed."]
    return "\n".join(L) + "\n"
