"""Apply the fitted model to today's card and compare with the offered prices.

Reads odds from odds_today.json.
"""

import json
import os
import numpy as np
import dcmodel as M
import backtest as B
from datetime import datetime, timezone

XI, REG, ALPHA = 0.0015, 2.0, 0.3
ASOF = np.datetime64(datetime.now(timezone.utc).strftime("%Y-%m-%d"))

HERE = os.path.dirname(os.path.abspath(__file__))
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
    fg = M.fit_asof(matches, index, n_teams, ASOF, XI, REG, pp, first_seen,
                    response="goals")
    fx = M.fit_asof(matches, index, n_teams, ASOF, XI, REG, pp, first_seen,
                    response="xg", min_matches=80)
    CACHE[k] = (fg, fx, pp)
    return CACHE[k]


def name_to_id(league, name):
    matches, names, index, n_teams, first_seen = get(league)
    for tid, nm in names.items():
        if nm == name and tid in index:
            return tid
    return None


def match_count(league, tid, season):
    matches, _, _, _, _ = get(league)
    return sum(1 for m in matches
               if m["season"] == season and tid in (m["home"], m["away"]))


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
        lam_h = float(np.exp((1 - ALPHA) * np.log(lgh) + ALPHA * np.log(xh)))
        lam_a = float(np.exp((1 - ALPHA) * np.log(lga) + ALPHA * np.log(xa)))
        used_xg = True
    else:
        lam_h, lam_a = lgh, lga

    mk = M.markets_from_matrix(M.score_matrix(lam_h, lam_a, fg["rho"]))
    mk["lam_h"], mk["lam_a"] = lam_h, lam_a
    mk["used_xg"] = used_xg
    # how much top-flight evidence stands behind each side
    mk["hist_home"] = sum(match_count(league, h, s) for s in (2024, 2025, 2026))
    mk["hist_away"] = sum(match_count(league, a, s) for s in (2024, 2025, 2026))
    mk["new_home"] = first_seen.get(h, 9999) >= 2026
    mk["new_away"] = first_seen.get(a, 9999) >= 2026
    return mk, None


def kelly(p, dec):
    b = dec - 1.0
    f = (p * b - (1 - p)) / b
    return max(0.0, f)


def main(preview=False):
    import generate_live_card
    card = generate_live_card.build_live_card()

    print("=" * 96)
    print("MODEL vs MARKET   (Dixon-Coles, xi=%.4f half-life %.0fd, reg=%.1f, "
          "xG blend a=%.2f)" % (XI, np.log(2) / XI, REG, ALPHA))
    print("as of %s   |   market de-vigged by power method" % ASOF)
    print("=" * 96)

    flagged = []
    paper_picks = []
    for g in card:
        mk, err = predict(g["league"], g["home"], g["away"])
        if err:
            print(f"\n### {g['home']} vs {g['away']}  --  SKIPPED ({err})")
            continue

        print(f"\n### {g['home']} vs {g['away']}   [{g['league']}]")
        tag = []
        if mk["new_home"]:
            tag.append(f"{g['home']} promoted - no top-flight history, prior-based")
        if mk["new_away"]:
            tag.append(f"{g['away']} promoted - no top-flight history, prior-based")
        if mk["hist_home"] < 40:
            tag.append(f"{g['home']} thin history ({mk['hist_home']} matches since 2024)")
        if mk["hist_away"] < 40:
            tag.append(f"{g['away']} thin history ({mk['hist_away']} matches since 2024)")
        if tag:
            print("   !! " + "; ".join(tag))
        print(f"   model xG: {mk['lam_h']:.2f} - {mk['lam_a']:.2f}")

        o = g["odds"]
        # ---- 1X2
        if all(k in o for k in ("home", "draw", "away")):
            sides = ("home", "draw", "away")
            fair_by_book = {
                book: M.devig_power([M.american_to_prob(prices[s]) for s in sides])
                for book, prices in g["books"].items()
            }
            fair = [fair_by_book[g["best_book"][s]][i] for i, s in enumerate(sides)]
            model = np.array([mk["home"], mk["draw"], mk["away"]])

            print("   %-9s %8s %8s %8s %8s %9s" %
                  ("1X2", "odds", "model", "fair", "edge", "kelly"))
            for i, lab in enumerate(("Home", "Draw", "Away")):
                dec = M.american_to_decimal(o[["home", "draw", "away"][i]])
                ed = model[i] - fair[i]
                k = kelly(model[i], dec)
                mark = ""
                if ed > 0.02 and k > 0:
                    mark = " <<"
                    side = sides[i]
                    paper_picks.append((g["league"], g["match_id"], side, g["best_book"][side],
                                        o[side], float(model[i]), g["kickoff"]))
                    flagged.append((g.get("league", ""), g["home"], g["away"], f"{lab} ({g['best_book'][sides[i]]})", ed, k,
                                    mk["new_home"] or mk["new_away"],
                                    o[["home", "draw", "away"][i]], g.get("kickoff", "")))
                print("   %-9s %8s %7.1f%% %7.1f%% %+7.1fpp %8.1f%%%s" %
                      (lab, o[["home", "draw", "away"][i]],
                       100 * model[i], 100 * fair[i], 100 * ed, 100 * k, mark))

        # ---- two-way markets
        for key, labs, mkeys in (
            ("ou25", ("Over 2.5", "Under 2.5"), ("over25", "under25")),
            ("btts", ("BTTS Yes", "BTTS No"), ("btts_yes", "btts_no")),
        ):
            if key in o:
                raw = np.array([M.american_to_prob(o[key][0]),
                                M.american_to_prob(o[key][1])])
                fair = raw / raw.sum()
                model = np.array([mk[mkeys[0]], mk[mkeys[1]]])
                for i in range(2):
                    dec = M.american_to_decimal(o[key][i])
                    ed = model[i] - fair[i]
                    k = kelly(model[i], dec)
                    mark = ""
                    if ed > 0.02 and k > 0:
                        mark = " <<"
                        flagged.append((g.get("league", ""), g["home"], g["away"], labs[i], ed, k,
                                        mk["new_home"] or mk["new_away"],
                                        o[key][i], g.get("kickoff", "")))
                    print("   %-9s %8s %7.1f%% %7.1f%% %+7.1fpp %8.1f%%%s" %
                          (labs[i], o[key][i], 100 * model[i], 100 * fair[i],
                           100 * ed, 100 * k, mark))

    print("\n" + "=" * 96)
    print("MODEL DISAGREEMENTS, largest first  (edge = model%% - de-vigged market%%)")
    print("=" * 96)
    if not flagged:
        print("NO BET" if card else "NO BET - no eligible pregame prices")
    flagged.sort(key=lambda r: -r[4])
    for lg, hm, aw, lab, ed, k, promoted, odds, ko in flagged:
        warn = "  [involves promoted side - low confidence]" if promoted else ""
        print(f"  {ed*100:+5.1f}pp  half-Kelly {50*k:4.1f}%  {hm} v {aw} [{lg}]: {lab} @ {odds}{warn}")

    if preview:
        return

    import paper
    paper.record(paper_picks)

    # Send alerts to Telegram if configured
    try:
        import sys
        sys.path.insert(0, os.path.dirname(HERE))
        import telegram_alerts
        if flagged:
            bets = [{
                "league": lg, "home": hm, "away": aw, "selection": lab,
                "edge": ed, "kelly": k, "promoted": promoted,
                "odds": odds, "kickoff": ko
            } for lg, hm, aw, lab, ed, k, promoted, odds, ko in flagged]
            telegram_alerts.send_value_bets_alert(bets)
    except Exception as e:
        print(f"Telegram alert failed: {type(e).__name__}")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--preview", action="store_true")
    main(parser.parse_args().preview)
