"""Data verification, completeness and health. Read-only everywhere.

    python -m soccerlab data verify         schema/consistency of repo data
    python -m soccerlab data completeness   coverage per league/season/source
    python -m soccerlab health              live pipeline health; set
                                            SOCCERLAB_PROD_ROOT to inspect the
                                            production checkout (read-only)
"""

import glob
import json
import os
import re
from collections import Counter, defaultdict
from datetime import timedelta
from pathlib import Path

from . import clv, pit
from .paths import DATA_DIR, LEGACY_MODEL_DIR, REPO_ROOT, prod_root
from .timeutil import parse_utc, utcnow

LEAGUES = ["epl", "laliga", "seriea", "ligue1"]


def verify():
    ok = True
    for lg in LEAGUES:
        try:
            ld = pit.load_league(lg, DATA_DIR)
        except Exception as exc:                                  # noqa: BLE001
            print(f"  FAIL {lg}: {exc}")
            ok = False
            continue
        st = Counter(m.status for m in ld.matches)
        bad_team = [m.uid for m in ld.matches if m.home == m.away]
        per = Counter(m.season for m in ld.matches)
        print(f"  {lg:7s} {dict(sorted(per.items()))}  status={dict(st)}")
        if bad_team:
            ok = False
            print(f"    FAIL self-fixtures: {bad_team[:5]}")
        for s, n in per.items():
            expected = 306 if (lg == "ligue1" and s >= 2023) else 380   # Ligue 1: 18 clubs from 2023-24
            if s < 2026 and n != expected:
                print(f"    WARN season {s} has {n} fixtures (expected {expected})")
    return ok


def completeness():
    with open(LEGACY_MODEL_DIR / "odds_joined.json", encoding="utf-8") as f:
        odds = json.load(f)
    print("  league  season  finals  xG%    odds_joined%  pinnacle_close%")
    for lg in LEAGUES:
        ld = pit.load_league(lg, DATA_DIR)
        by = defaultdict(list)
        for m in ld.matches:
            if m.final:
                by[m.season].append(m)
        for s, ms in sorted(by.items()):
            xg = sum(m.xgh is not None for m in ms) / len(ms)
            oj = [odds.get(lg, {}).get(str(m.mid)) for m in ms]
            j = sum(o is not None for o in oj) / len(ms)
            pc = sum(bool(o and "pinnacle_close" in o) for o in oj) / len(ms)
            print(f"  {lg:7s} {s}    {len(ms):5d}  {100 * xg:5.1f}  {100 * j:11.1f}  {100 * pc:14.1f}")
    return True


def health(root=None, now=None):
    """Red/amber/green checks over a (production) checkout, read-only."""
    root = Path(root or prod_root() or REPO_ROOT)
    now = now or utcnow()
    checks = []

    def add(level, name, detail):
        checks.append((level, name, detail))

    lp = root / "odds_logger" / "latest_poll.json"
    if lp.exists():
        snap = json.loads(lp.read_text(encoding="utf-8"))
        age = now - parse_utc(snap["observed_at"])
        n = sum(len(v) for v in snap.get("leagues", {}).values())
        add("red" if age > timedelta(hours=4) or n == 0 else "green", "latest odds poll",
            f"age {age}, {n} quotes")
    else:
        add("red", "latest odds poll", "missing")

    runlog = root / "odds_logger" / "runs.log"
    if runlog.exists():
        recent = []
        for line in runlog.read_text(encoding="utf-8", errors="replace").splitlines()[-400:]:
            mt = re.match(r"\[(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\]", line)
            if mt and now - parse_utc(mt.group(1).replace(" ", "T") + "Z") <= timedelta(hours=24):
                recent.append(line)
        auth = sum("HTTP 401" in x or "HTTP 403" in x for x in recent)
        rl = sum(" 429" in x for x in recent)
        zero = sum(", 0 quotes" in x for x in recent)
        add("red" if auth else ("amber" if rl or zero else "green"), "logger last 24h",
            f"{auth} auth failures, {rl} rate-limit backoffs, {zero} zero-quote league polls")

    logs = sorted(glob.glob(str(root / "odds_logger" / "log" / "*.jsonl")))
    if logs:
        months = defaultdict(int)
        for p in logs:
            months[os.path.basename(p).rsplit("_", 1)[1][:7]] += os.path.getsize(p)
        empty = [m for m, b in months.items() if b == 0]
        add("amber" if empty else "green", "odds log months", f"{len(months)} months; empty: {empty or 'none'}")
        qs = clv.load_quote_log(root / "odds_logger" / "log")
        bad = sum(not q.usable for q in qs)
        add("amber" if bad else "green", "odds log quote validity", f"{bad}/{len(qs)} quotes unusable")

    for lg in LEAGUES:
        f = root / "balldontlie_consolidated" / f"{lg}_2026_matches.json"
        st = root / "balldontlie_consolidated" / f"{lg}_2026_team_match_stats.json"
        if not f.exists():
            add("red", f"{lg} schedule", "missing")
            continue
        sched = json.loads(f.read_text(encoding="utf-8"))
        late = [m for m in sched if m.get("status_state") == "scheduled"
                and parse_utc(m["date"]) < now - timedelta(hours=6)]
        finals = sum(m.get("status_state") == "final" for m in sched)
        add("red" if late else "green", f"{lg} results feed", f"{finals} finals; {len(late)} past fixtures unresulted")
        if st.exists():
            nxg = len(json.loads(st.read_text(encoding="utf-8"))) // 2
            add("amber" if nxg < 0.8 * finals else "green", f"{lg} xG coverage 2026",
                f"{nxg}/{finals} finals have xG (legacy live model blends xG at 30%)")

    paper = root / "odds_logger" / "paper.sqlite3"
    if paper.exists():
        import sqlite3
        db = sqlite3.connect(f"file:{paper}?mode=ro", uri=True)
        try:
            n, s = db.execute("SELECT count(*), count(pnl) FROM picks").fetchone()
            add("green", "legacy paper ledger", f"{n} picks, {s} settled (read-only)")
        except sqlite3.Error as exc:
            add("amber", "legacy paper ledger", str(exc))
        finally:
            db.close()

    worst = "green"
    for level, name, detail in checks:
        print(f"  [{level.upper():5s}] {name}: {detail}")
        if level == "red" or (level == "amber" and worst == "green"):
            worst = level if worst != "red" else worst
    print(f"HEALTH {worst.upper()} ({root})")
    return worst, checks


def main(action):
    if action == "verify":
        ok = verify()
        print("DATA VERIFY", "PASSED" if ok else "FAILED")
        return 0 if ok else 1
    if action == "completeness":
        completeness()
        return 0
    worst, _ = health()
    return 0 if worst == "green" else (1 if worst == "red" else 0)
