"""
Rebuild Serie A + Ligue 1 card from raw odds logs (no kickoff-time filter)
and run Dixon-Coles model against those odds.
"""

import sys, os, json
import numpy as np
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))

import dcmodel as M
import backtest as B

XI, REG, ALPHA = 0.0015, 2.0, 0.3
ASOF = np.datetime64("2026-10-18")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
LOG_DIR = os.path.join(ROOT, "odds_logger", "log")
SCHEDULE_DIR = os.path.join(ROOT, "balldontlie_consolidated")

TARGET = {"seriea": "Serie A", "ligue1": "Ligue 1"}
MONTH_STR = "2026-10"

# Build card from logs for target leagues only
# Window: Oct 17 00:00 UTC to Oct 19 00:00 UTC
WINDOW_START = datetime(2026, 10, 17, tzinfo=timezone.utc)
WINDOW_END   = datetime(2026, 10, 19, tzinfo=timezone.utc)

card = []

for lg in TARGET:
    m_file = os.path.join(SCHEDULE_DIR, f"{lg}_2026_matches.json")
    t_file = os.path.join(SCHEDULE_DIR, f"{lg}_2026_teams.json")
    log_file = os.path.join(LOG_DIR, f"{lg}_{MONTH_STR}.jsonl")

    if not (os.path.exists(m_file) and os.path.exists(t_file)):
        print(f"  Missing schedule for {lg} - skipping")
        continue
    if not os.path.exists(log_file):
        print(f"  No odds log for {lg} {MONTH_STR} - skipping")
        continue

    with open(t_file, encoding="utf-8") as f:
        teams = {t["id"]: t["name"] for t in json.load(f)}
    with open(m_file, encoding="utf-8") as f:
        matches = {m["id"]: m for m in json.load(f)}

    latest_quotes = {}
    with open(log_file, encoding="utf-8") as f:
        for line in f:
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            mid = r.get("match_id")
            if mid not in latest_quotes:
                latest_quotes[mid] = {}
            latest_quotes[mid][r.get("vendor")] = r

    for mid, vendor_dict in latest_quotes.items():
        m = matches.get(mid)
        if not m:
            continue
        try:
            kickoff = datetime.fromisoformat(m["date"].replace("Z", "+00:00"))
        except Exception:
            continue

        # Accept games in Oct 17-18 window
        if not (WINDOW_START <= kickoff < WINDOW_END):
            continue

        home_team = teams.get(m.get("home_team_id"))
        away_team = teams.get(m.get("away_team_id"))
        if not (home_team and away_team):
            continue

        home_odds = [v["home"] for v in vendor_dict.values() if v.get("home") is not None]
        draw_odds = [v["draw"] for v in vendor_dict.values() if v.get("draw") is not None]
        away_odds = [v["away"] for v in vendor_dict.values() if v.get("away") is not None]

        if not (home_odds and draw_odds and away_odds):
            continue

        card.append({
            "league": lg,
            "home": home_team,
            "away": away_team,
            "kickoff": m["date"],
            "odds": {
                "home": max(home_odds),
                "draw": max(draw_odds),
                "away": max(away_odds),
            }
        })

card.sort(key=lambda x: (x["league"], x["kickoff"]))

if not card:
    print("No Serie A / Ligue 1 matches found in the Oct 17-18 window.")
    sys.exit(0)

# ── model ──────────────────────────────────────────────────────────────────────
CACHE = {}

def get(league):
    if league not in CACHE:
        CACHE[league] = B.prep(league)
    return CACHE[league]

def fits(league):
    k = ("fit", league)
    if k in CACHE:
        return CACHE[k]
    matches, names, index, n_teams, first_seen = get(league)
    pk = ("prior", league)
    if pk not in CACHE:
        CACHE[pk] = M.estimate_promoted_prior(matches, index, n_teams)
    pp = CACHE[pk]
    fg = M.fit_asof(matches, index, n_teams, ASOF, XI, REG, pp, first_seen, response="goals")
    fx = M.fit_asof(matches, index, n_teams, ASOF, XI, REG, pp, first_seen,
                    response="xg", min_matches=80)
    CACHE[k] = (fg, fx, pp)
    return CACHE[k]

def name_to_id(league, name):
    matches, names, index, n_teams, first_seen = get(league)
    for tid, nm in names.items():
        if nm == name and tid in index:
            return tid
    nl = name.lower()
    for tid, nm in names.items():
        if tid in index and (nl in nm.lower() or nm.lower() in nl):
            return tid
    return None

def predict(league, home_name, away_name):
    matches, names, index, n_teams, first_seen = get(league)
    fg, fx, pp = fits(league)
    h = name_to_id(league, home_name)
    a = name_to_id(league, away_name)
    if h is None or a is None:
        return None, f"team not found: {home_name if h is None else away_name}"
    lgh, lga = M.lambdas(fg, index, h, a)
    used_xg = False
    if fx is not None:
        xh, xa = M.lambdas(fx, index, h, a)
        lam_h = float(np.exp((1-ALPHA)*np.log(lgh) + ALPHA*np.log(xh)))
        lam_a = float(np.exp((1-ALPHA)*np.log(lga) + ALPHA*np.log(xa)))
        used_xg = True
    else:
        lam_h, lam_a = lgh, lga
    mk = M.markets_from_matrix(M.score_matrix(lam_h, lam_a, fg["rho"]))
    mk["lam_h"], mk["lam_a"] = lam_h, lam_a
    mk["used_xg"] = used_xg
    mk["new_home"] = first_seen.get(h, 9999) >= 2026
    mk["new_away"] = first_seen.get(a, 9999) >= 2026
    mc = lambda t: sum(1 for m in matches if m["season"] in (2024,2025,2026) and t in (m["home"],m["away"]))
    mk["hist_home"] = mc(h)
    mk["hist_away"] = mc(a)
    return mk, None

def kelly(p, dec):
    b = dec - 1.0
    f = (p * b - (1 - p)) / b
    return max(0.0, f)

print("=" * 96)
print("DIXON-COLES vs MARKET  (xi=%.4f half-life %.0fd, reg=%.1f, xG a=%.2f)"
      % (XI, np.log(2)/XI, REG, ALPHA))
print("as of %s  |  Serie A + Ligue 1  |  Bundesliga NOT in dataset" % ASOF)
print("=" * 96)

flagged = []

for league_key in ["seriea", "ligue1"]:
    lg_games = [g for g in card if g["league"] == league_key]
    if not lg_games:
        continue
    print(f"\n{'='*96}")
    print(f"  {TARGET[league_key]}  ({len(lg_games)} matches)")
    print(f"{'='*96}")

    for g in lg_games:
        mk, err = predict(league_key, g["home"], g["away"])
        if err:
            print(f"\n  ### {g['home']} vs {g['away']}  -- SKIPPED ({err})")
            continue

        ko = g["kickoff"][:16].replace("T", " ") + " UTC"
        print(f"\n  ### {g['home']} vs {g['away']}   [{ko}]")

        tags = []
        if mk["new_home"]: tags.append(f"{g['home']} promoted (prior-based)")
        if mk["new_away"]: tags.append(f"{g['away']} promoted (prior-based)")
        if mk["hist_home"] < 40: tags.append(f"{g['home']} thin ({mk['hist_home']} matches)")
        if mk["hist_away"] < 40: tags.append(f"{g['away']} thin ({mk['hist_away']} matches)")
        if tags:
            print("     !! " + "; ".join(tags))

        print(f"     model xG: {mk['lam_h']:.2f} - {mk['lam_a']:.2f}  "
              f"({'goals+xG' if mk['used_xg'] else 'goals only'})")

        o = g["odds"]
        if any(abs(v) >= 5000 for v in [o["home"], o["draw"], o["away"]]):
            print("     !! Corrupted odds line (>=5000) -- skipping")
            continue

        raw = np.array([M.american_to_prob(o[k]) for k in ("home","draw","away")])
        fair = M.devig_power(raw)
        model = np.array([mk["home"], mk["draw"], mk["away"]])
        print(f"     overround {100*(raw.sum()-1):.1f}%")
        print("     %-10s %8s %8s %8s %8s %9s" %
              ("1X2", "odds", "model", "fair", "edge", "kelly"))

        for i, lab in enumerate(("Home","Draw","Away")):
            ok = ["home","draw","away"][i]
            dec = M.american_to_decimal(o[ok])
            ed = model[i] - fair[i]
            k = kelly(model[i], dec)
            mark = " <<" if ed > 0.02 else ""
            if ed > 0.02:
                flagged.append((league_key, g["home"], g["away"], lab, ed, k,
                                mk["new_home"] or mk["new_away"], o[ok], ko))
            print("     %-10s %8s %7.1f%% %7.1f%% %+7.1fpp %8.1f%%%s" %
                  (lab, o[ok], 100*model[i], 100*fair[i], 100*ed, 100*k, mark))

        print(f"     {'─'*56}")
        print(f"     O2.5: {100*mk['over25']:.1f}%  U2.5: {100*mk['under25']:.1f}%  "
              f"BTTS Yes: {100*mk['btts_yes']:.1f}%  BTTS No: {100*mk['btts_no']:.1f}%")

print("\n" + "=" * 96)
print("VALUE EDGES  >2pp  (largest first)")
print("=" * 96)
flagged.sort(key=lambda r: -r[4])
for lg, hm, aw, lab, ed, k, promoted, odds, ko in flagged:
    warn = "  [promoted -- low confidence]" if promoted else ""
    print(f"  {ed*100:+5.1f}pp  half-Kelly {50*k:4.1f}%   {hm} v {aw} [{lg}]: {lab} @ {odds}{warn}")

if not flagged:
    print("  No edges > 2pp found.")

print("\n  Bundesliga: NOT in dataset. To add it:")
print("    1. python pull_balldontlie_v2.py --league bundesliga")
print("    2. Add 'bundesliga' to dcmodel.LEAGUES")
