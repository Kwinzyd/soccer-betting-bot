"""Read the odds log.

  python read_log.py spread     best price across vendors vs the vendor average
                                -- this is the line-shopping edge, measured on
                                   your own data instead of assumed
  python read_log.py moves      fixtures whose price has actually moved
  python read_log.py summary    how much history has accumulated so far
"""

import glob
import json
import os
import sys
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(HERE, "log")
SCHEDULE_DIR = os.path.join(os.path.dirname(HERE), "balldontlie_consolidated")
SEASON = 2026


def load():
    rows = []
    for p in sorted(glob.glob(os.path.join(LOG_DIR, "*.jsonl"))):
        with open(p, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        rows.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
    return rows


def names():
    """match_id -> 'Home v Away' from the local schedule files."""
    out = {}
    for lg in ("epl", "laliga", "seriea", "ligue1"):
        mp = os.path.join(SCHEDULE_DIR, f"{lg}_{SEASON}_matches.json")
        tp = os.path.join(SCHEDULE_DIR, f"{lg}_{SEASON}_teams.json")
        if not (os.path.exists(mp) and os.path.exists(tp)):
            continue
        with open(tp, encoding="utf-8") as f:
            tn = {t["id"]: t["name"] for t in json.load(f)}
        with open(mp, encoding="utf-8") as f:
            for m in json.load(f):
                out[m["id"]] = (f"{tn.get(m['home_team_id'], '?')} v "
                                f"{tn.get(m['away_team_id'], '?')}", m["date"][:16])
    return out


def dec(american):
    a = float(american)
    return a / 100.0 + 1.0 if a > 0 else 100.0 / (-a) + 1.0


def latest_by_vendor(rows):
    """(match_id, vendor) -> most recent quote."""
    best = {}
    for r in rows:
        k = (r["match_id"], r["vendor"])
        if k not in best or r["observed_at"] > best[k]["observed_at"]:
            best[k] = r
    return best


def cmd_spread():
    rows = load()
    nm = names()
    latest = latest_by_vendor(rows)
    by_match = defaultdict(dict)
    for (mid, vendor), r in latest.items():
        by_match[mid][vendor] = r

    print("Best-available price vs vendor average, per outcome.")
    print("This gap is the line-shopping edge. It needs to exceed the vig to")
    print("be worth anything -- and remember it tested at t=1.14, not proven.\n")
    tot, n = 0.0, 0
    for mid, vend in sorted(by_match.items()):
        if len(vend) < 2:
            continue
        label, when = nm.get(mid, (str(mid), ""))
        parts = []
        for i, side in enumerate(("home", "draw", "away")):
            ds = [dec(v[side]) for v in vend.values() if v.get(side) is not None]
            if len(ds) < 2:
                continue
            gap = (max(ds) / (sum(ds) / len(ds)) - 1.0) * 100
            tot += gap
            n += 1
            parts.append(f"{side[0].upper()} best {max(ds):5.2f} (+{gap:.1f}%)")
        if parts:
            print(f"  {when}  {label[:42]:42s} {' | '.join(parts)}")
    if n:
        print(f"\n  mean best-vs-average premium: +{tot/n:.2f}% "
              f"over {n} outcomes, {len(by_match)} fixtures, "
              f"{len({r['vendor'] for r in rows})} vendors")


def cmd_moves():
    rows = load()
    nm = names()
    seq = defaultdict(list)
    for r in rows:
        seq[(r["match_id"], r["vendor"])].append(r)
    shown = 0
    for (mid, vendor), lst in sorted(seq.items()):
        if len(lst) < 2:
            continue
        lst.sort(key=lambda r: r["observed_at"])
        label, when = nm.get(mid, (str(mid), ""))
        print(f"\n  {label[:50]} [{vendor}]")
        for r in lst:
            print(f"    {r['observed_at'][:19]}  "
                  f"H {r['home']:>6}  D {r['draw']:>6}  A {r['away']:>6}")
        shown += 1
    if not shown:
        print("No line movement recorded yet -- only one observation per quote.")
        print("Movement appears once the logger has run across several hours.")


def cmd_summary():
    rows = load()
    if not rows:
        print("Log is empty. Run log_odds.py (or wait for the scheduled task).")
        return
    obs = sorted(r["observed_at"] for r in rows)
    per_lg = defaultdict(int)
    for r in rows:
        per_lg[r["league"]] += 1
    print(f"  rows            {len(rows)}")
    print(f"  first observed  {obs[0][:19]}")
    print(f"  last observed   {obs[-1][:19]}")
    print(f"  fixtures        {len({r['match_id'] for r in rows})}")
    print(f"  vendors         {sorted({r['vendor'] for r in rows})}")
    for lg, c in sorted(per_lg.items()):
        print(f"    {lg:8s} {c}")
    print("\n  Backtesting props or line movement needs months of this, not days.")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "summary"
    {"spread": cmd_spread, "moves": cmd_moves,
     "summary": cmd_summary}.get(cmd, cmd_summary)()
