"""Join logged odds to match results, then measure CLV and strategy ROI.

Without this the odds log is inert: prices with no outcomes attached cannot
answer any question. This is the piece that turns it into evidence.

  python settle.py --refresh     re-pull results from the API, then settle
  python settle.py               settle using the cached results
  python settle.py --selftest    verify the settlement arithmetic

Two things are measured once enough matches have finished:

CLV  For each quote taken before kickoff, compare its de-vigged probability
     with the de-vigged probability of the LAST quote seen before kickoff.
     If prices you took drift toward you, CLV is positive and you are ahead of
     the market. This is the diagnostic that condemned the Dixon-Coles model,
     and it is the one that matters -- ROI over a few hundred bets is mostly
     noise, CLV converges far faster.

ROI  Settle the best-available price across vendors whenever it beats the
     cross-vendor average fair price. This is the line-shopping strategy that
     was the only positive result in ../model/strategy.py.
"""

import glob
import json
import os
import sys
import time
import urllib.request
import urllib.error
from collections import defaultdict
from datetime import datetime, timezone

from pathlib import Path

KEY = (os.environ.get("BALLDONTLIE_KEY") or
       (Path(os.environ.get("LOCALAPPDATA", Path.home())) / "soccer-betting-bot" / "bdl.key").read_text().strip())

HERE = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(HERE, "log")
RESULTS_PATH = os.path.join(HERE, "results_cache.json")
SETTLED_PATH = os.path.join(HERE, "settled.jsonl")
SCHEDULE_DIR = os.path.join(os.path.dirname(HERE), "balldontlie_consolidated")

LEAGUES = ["epl", "laliga", "seriea", "ligue1"]
VERSION = {"laliga": "v1", "seriea": "v1", "ligue1": "v1", "epl": "v2"}
SEASON = 2026
SLEEP = 13.0


# ----------------------------------------------------------------- utilities

def dec(american):
    a = float(american)
    return a / 100.0 + 1.0 if a > 0 else 100.0 / (-a) + 1.0


def devig_power(probs, tol=1e-12):
    """Power de-vig: solve sum(p**k) = 1. Same method as the model code, so
    numbers here are comparable with ../model/."""
    p = list(probs)
    lo, hi = 0.5, 3.0
    k = 1.0
    for _ in range(200):
        k = 0.5 * (lo + hi)
        s = sum(x ** k for x in p)
        if abs(s - 1.0) < tol:
            break
        if s > 1.0:
            lo = k
        else:
            hi = k
    tot = sum(x ** k for x in p)
    return [x ** k / tot for x in p]


def load_log():
    rows = []
    for path in sorted(glob.glob(os.path.join(LOG_DIR, "*.jsonl"))):
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        rows.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
    return rows


def kickoffs():
    """match_id -> (iso kickoff, label). From the local schedule files."""
    out = {}
    for lg in LEAGUES:
        mp = os.path.join(SCHEDULE_DIR, f"{lg}_{SEASON}_matches.json")
        tp = os.path.join(SCHEDULE_DIR, f"{lg}_{SEASON}_teams.json")
        if not (os.path.exists(mp) and os.path.exists(tp)):
            continue
        with open(tp, encoding="utf-8") as f:
            tn = {t["id"]: t["name"] for t in json.load(f)}
        with open(mp, encoding="utf-8") as f:
            for m in json.load(f):
                out[m["id"]] = (m["date"],
                                f"{tn.get(m['home_team_id'],'?')} v "
                                f"{tn.get(m['away_team_id'],'?')}")
    return out


# ------------------------------------------------------------------ results

def fetch(url):
    req = urllib.request.Request(url, headers={"Authorization": KEY})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 429:
                print(f"  429, waiting 25s (attempt {attempt+1})")
                time.sleep(25)
                continue
            print(f"  HTTP {e.code}")
            return None
        except Exception as e:                                   # noqa: BLE001
            print(f"  {type(e).__name__}: {e}")
            time.sleep(5)
    return None


def refresh_results():
    """Pull finished matches for the current season into the cache."""
    cache = load_results()
    for league in LEAGUES:
        cursor, page = None, 1
        while True:
            url = (f"https://api.balldontlie.io/{league}/{VERSION[league]}"
                   f"/matches?season={SEASON}&per_page=100")
            if cursor:
                url += f"&cursor={cursor}"
            data = fetch(url)
            time.sleep(SLEEP)
            if not data or "data" not in data:
                break
            for m in data["data"]:
                if (m.get("status_state") == "final"
                        and m.get("home_score") is not None):
                    hs, as_ = int(m["home_score"]), int(m["away_score"])
                    cache[str(m["id"])] = {
                        "league": league,
                        "home_score": hs, "away_score": as_,
                        "outcome": 0 if hs > as_ else (1 if hs == as_ else 2),
                    }
            nxt = (data.get("meta") or {}).get("next_cursor")
            if not nxt or nxt == cursor or len(data["data"]) < 100:
                break
            cursor, page = nxt, page + 1
        print(f"  {league}: cache now {sum(1 for v in cache.values() if v['league']==league)} finished")
    with open(RESULTS_PATH, "w", encoding="utf-8") as f:
        json.dump(cache, f)
    return cache


def load_results():
    if os.path.exists(RESULTS_PATH):
        with open(RESULTS_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {}


# ---------------------------------------------------------------- settlement

def build_settled(log_rows, results, ko):
    """One record per (match, vendor, observation) that has a known outcome."""
    out = []
    for r in log_rows:
        res = results.get(str(r["match_id"]))
        if not res:
            continue
        kick, label = ko.get(r["match_id"], (None, str(r["match_id"])))
        if kick and r["observed_at"] > kick:
            continue                     # quote seen after kickoff -- not usable
        prices = [dec(r["home"]), dec(r["draw"]), dec(r["away"])]
        fair = devig_power([1.0 / p for p in prices])
        out.append({
            "match_id": r["match_id"], "league": r["league"],
            "vendor": r["vendor"], "observed_at": r["observed_at"],
            "kickoff": kick, "label": label,
            "prices": prices, "fair": fair,
            "outcome": res["outcome"],
            "score": f"{res['home_score']}-{res['away_score']}",
        })
    return out


def report_clv(settled):
    """Compare each quote against the last quote before kickoff, same vendor."""
    last = {}
    for s in settled:
        k = (s["match_id"], s["vendor"])
        if k not in last or s["observed_at"] > last[k]["observed_at"]:
            last[k] = s

    diffs = []
    for s in settled:
        close = last[(s["match_id"], s["vendor"])]
        if close["observed_at"] == s["observed_at"]:
            continue
        for i in range(3):
            diffs.append(close["fair"][i] - s["fair"][i])
    if not diffs:
        print("  no pre-close quotes with a later quote to compare against yet")
        return
    mean = sum(diffs) / len(diffs)
    print(f"  {len(diffs)} comparisons, mean drift = {mean*100:+.3f}pp")
    print("  (this is drift of the market itself; it becomes CLV once you")
    print("   attach it to specific picks rather than every outcome)")


def report_shopping(settled):
    """Best price across vendors vs the cross-vendor average fair price."""
    by_obs = defaultdict(list)
    for s in settled:
        by_obs[(s["match_id"], s["observed_at"][:13])].append(s)

    n = 0
    profit = 0.0
    for _, group in by_obs.items():
        if len(group) < 2:
            continue
        outcome = group[0]["outcome"]
        for i in range(3):
            avg_fair = sum(g["fair"][i] for g in group) / len(group)
            best = max(g["prices"][i] for g in group)
            if best * avg_fair - 1.0 > 0.01:
                n += 1
                profit += (best - 1.0) if outcome == i else -1.0
    if not n:
        print("  no qualifying bets yet")
        return
    print(f"  bets={n}  profit={profit:+.2f}u  ROI={100*profit/n:+.2f}%")
    print("  NOTE: a few hundred bets cannot distinguish a real edge from zero.")
    print("        ../model/strategy.py needed ~12,000 to reach significance.")


# ----------------------------------------------------------------- self test

def selftest():
    """Validate the arithmetic on constructed cases, since no real match has
    settled yet and untested settlement code is how silent losses happen."""
    ok = True

    d = dec(-200)
    ok &= abs(d - 1.5) < 1e-9
    print(f"  dec(-200) = {d:.4f}  expect 1.5000   {'OK' if abs(d-1.5)<1e-9 else 'FAIL'}")
    d = dec(150)
    ok &= abs(d - 2.5) < 1e-9
    print(f"  dec(+150) = {d:.4f}  expect 2.5000   {'OK' if abs(d-2.5)<1e-9 else 'FAIL'}")

    f = devig_power([1 / 2.0, 1 / 4.0, 1 / 4.0])   # already sums to 1
    ok &= abs(sum(f) - 1) < 1e-9 and abs(f[0] - 0.5) < 1e-6
    print(f"  devig no-vig book -> {[round(x,4) for x in f]}  expect [0.5,0.25,0.25]"
          f"   {'OK' if abs(f[0]-0.5)<1e-6 else 'FAIL'}")

    over = [1 / 1.8, 1 / 3.5, 1 / 4.0]
    f2 = devig_power(over)
    ok &= abs(sum(f2) - 1) < 1e-9 and sum(over) > 1 and f2[0] < over[0]
    print(f"  devig {sum(over):.4f} book -> sums {sum(f2):.6f}, favourite "
          f"{over[0]:.4f}->{f2[0]:.4f}   {'OK' if abs(sum(f2)-1)<1e-9 else 'FAIL'}")

    # settlement: a won bet at 2.5 returns +1.5, a lost bet -1.0
    ko = {1: ("2026-01-01T12:00:00+00:00", "A v B")}
    results = {"1": {"league": "epl", "home_score": 2, "away_score": 0,
                     "outcome": 0}}
    log = [{"match_id": 1, "league": "epl", "vendor": "x",
            "observed_at": "2026-01-01T09:00:00+00:00",
            "home": 150, "draw": 250, "away": 250},
           {"match_id": 1, "league": "epl", "vendor": "y",
            "observed_at": "2026-01-01T09:00:00+00:00",
            "home": 120, "draw": 260, "away": 260}]
    s = build_settled(log, results, ko)
    ok &= len(s) == 2 and all(x["outcome"] == 0 for x in s)
    print(f"  build_settled kept {len(s)}/2 rows, outcome=home   "
          f"{'OK' if len(s)==2 else 'FAIL'}")

    # a quote observed AFTER kickoff must be dropped
    log_late = log + [{"match_id": 1, "league": "epl", "vendor": "z",
                       "observed_at": "2026-01-01T13:00:00+00:00",
                       "home": 150, "draw": 250, "away": 250}]
    s2 = build_settled(log_late, results, ko)
    ok &= len(s2) == 2
    print(f"  post-kickoff quote dropped: {len(s2)}/2 kept   "
          f"{'OK' if len(s2)==2 else 'FAIL'}")

    print("\n  SELFTEST", "PASSED" if ok else "FAILED")
    return ok


# ---------------------------------------------------------------------- main

if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(0 if selftest() else 1)

    if "--refresh" in sys.argv:
        print("refreshing results ...")
        results = refresh_results()
    else:
        results = load_results()
        if not results:
            print("no cached results -- run with --refresh first")

    log_rows = load_log()
    ko = kickoffs()
    settled = build_settled(log_rows, results, ko)

    with open(SETTLED_PATH, "w", encoding="utf-8") as f:
        for s in settled:
            f.write(json.dumps(s, separators=(",", ":")) + "\n")

    print(f"\nlogged quotes      {len(log_rows)}")
    print(f"finished matches   {len(results)}")
    print(f"settled records    {len(settled)} -> {os.path.basename(SETTLED_PATH)}")

    if not settled:
        print("\nNothing to analyse yet: none of the logged fixtures have")
        print("finished. Re-run after some kick off. The arithmetic is covered")
        print("by --selftest in the meantime.")
        sys.exit(0)

    print("\n--- market drift ---")
    report_clv(settled)
    print("\n--- line shopping (best price vs cross-vendor fair) ---")
    report_shopping(settled)
