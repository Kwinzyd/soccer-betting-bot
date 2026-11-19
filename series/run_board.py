"""Run the board: goals markets from the Dixon-Coles model, counting markets
from the per-team venue series.

Counting markets (corners, cards, offsides) use a league-normalised
multiplicative rate model, same shape as the goals model:

    E[home corners] = lg_avg_home * (home's home rate / lg_avg_home)
                                  * (away's away conceded / lg_avg_away_conc)

and are then given a NEGATIVE BINOMIAL distribution rather than Poisson.
That matters: Brighton's home corners are 4.95 +/- 2.70, where Poisson would
imply an SD of sqrt(4.95) = 2.22. Corners are overdispersed, and pricing them
as Poisson systematically overstates the probability of the middle and
understates both tails -- exactly where the over/under lines sit.

NOTHING HERE IS BACKTESTED. There is no historical corner/card/offside odds
archive to test against, so unlike the 1X2 model -- which was tested and
found to lose -- these numbers have never been checked against a real price.
Treat every counting-market flag as a hypothesis.
"""

import csv
import json
import math
import os
import sys
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "model"))

import numpy as np                                              # noqa: E402
import dcmodel as M                                             # noqa: E402
import predict as P                                             # noqa: E402

RICH_FROM = 2025
SERIES = os.path.join(HERE, "out", "team_match_series.csv")


# --------------------------------------------------------------- series data

def load_series():
    rows = []
    with open(SERIES, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if int(r["season"]) >= RICH_FROM:
                rows.append(r)
    return rows


def num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def rate_tables(rows, field):
    """Venue-split for/against means, plus league baselines."""
    acc = defaultdict(lambda: {"for": [], "against": []})
    lg = defaultdict(lambda: {"home_for": [], "away_for": []})
    for r in rows:
        f_, a_ = num(r[f"{field}_for"]), num(r[f"{field}_against"])
        k = (r["league"], r["team"], r["venue"])
        if f_ is not None:
            acc[k]["for"].append(f_)
            lg[r["league"]]["home_for" if r["venue"] == "home" else "away_for"].append(f_)
        if a_ is not None:
            acc[k]["against"].append(a_)
    means = {k: (float(np.mean(v["for"])) if v["for"] else None,
                 float(np.mean(v["against"])) if v["against"] else None,
                 len(v["for"]))
             for k, v in acc.items()}
    base = {l: (float(np.mean(v["home_for"])) if v["home_for"] else None,
                float(np.mean(v["away_for"])) if v["away_for"] else None)
            for l, v in lg.items()}
    # pooled dispersion: variance/mean of the per-match totals, league-wide
    disp = {}
    for l in base:
        tot = []
        seen = set()
        for r in rows:
            if r["league"] != l or r["venue"] != "home":
                continue
            f_, a_ = num(r[f"{field}_for"]), num(r[f"{field}_against"])
            if f_ is not None and a_ is not None and r["match_id"] not in seen:
                seen.add(r["match_id"])
                tot.append(f_ + a_)
        if len(tot) > 5:
            m, v = float(np.mean(tot)), float(np.var(tot, ddof=1))
            disp[l] = max(1.0, v / m) if m > 0 else 1.0
    return means, base, disp


def expected(means, base, league, home, away, field_default=None):
    """Multiplicative rate model -> (home expectation, away expectation)."""
    hm = means.get((league, home, "home"))
    am = means.get((league, away, "away"))
    bh, ba = base.get(league, (None, None))
    if not hm or not am or bh is None or ba is None:
        return None, None
    h_for, h_against, _ = hm
    a_for, a_against, _ = am
    if None in (h_for, h_against, a_for, a_against) or bh <= 0 or ba <= 0:
        return None, None
    eh = bh * (h_for / bh) * (a_against / bh)
    ea = ba * (a_for / ba) * (h_against / ba)
    return eh, ea


def expected_additive(means, league, home, away):
    """Cross-check: plain average of the two sides' observed rates.

    The multiplicative form multiplies a strong attack by a weak defence and
    can project a total ABOVE anything either team has actually produced --
    Man City home corners come out at 12.4 when City's home games averaged
    10.4 and Bournemouth's away games 11.7. Whenever the two specifications
    disagree materially, the "edge" is model uncertainty, not signal.
    """
    hm = means.get((league, home, "home"))
    am = means.get((league, away, "away"))
    if not hm or not am:
        return None, None
    h_for, h_against, _ = hm
    a_for, a_against, _ = am
    if None in (h_for, h_against, a_for, a_against):
        return None, None
    return (h_for + a_against) / 2.0, (a_for + h_against) / 2.0


def red_rate(rows, league, home, away):
    """Expected reds in the match: the two sides' venue-specific rates."""
    h = [num(r["red_cards_for"]) for r in rows
         if r["league"] == league and r["team"] == home and r["venue"] == "home"]
    a = [num(r["red_cards_for"]) for r in rows
         if r["league"] == league and r["team"] == away and r["venue"] == "away"]
    h = [x for x in h if x is not None]
    a = [x for x in a if x is not None]
    return (float(np.mean(h)) if h else 0.0) + (float(np.mean(a)) if a else 0.0)


def ref_stats(rows):
    """Per-referee yellow rate, with the league mean and a noise check."""
    per = defaultdict(list)
    lg = defaultdict(list)
    for r in rows:
        y = num(r["yellow_cards_for"])
        if y is None or not r["referee"]:
            continue
        per[(r["league"], r["referee"])].append(y)
        lg[r["league"]].append(y)
    league_mean = {l: float(np.mean(v)) for l, v in lg.items()}
    league_sd = {l: float(np.std(v, ddof=1)) for l, v in lg.items()}
    out = {}
    for (l, ref), v in per.items():
        n = len(v)
        m = float(np.mean(v))
        se = league_sd[l] / math.sqrt(n) if n > 1 else float("inf")
        # |deviation| > 2 SE before we treat a referee effect as real
        sig = abs(m - league_mean[l]) > 2 * se
        out[(l, _norm(ref))] = (m / league_mean[l] if league_mean[l] else 1.0,
                                n // 2, sig)
    return out


def _norm(s):
    import unicodedata
    return "".join(c for c in unicodedata.normalize("NFD", s)
                   if unicodedata.category(c) != "Mn").lower().strip()


def ref_multiplier(refstats, league, referee):
    """Multiplier on the card total. Referees whose deviation is inside noise
    are pulled to neutral rather than trusted -- one season is ~22 matches per
    official, which is not enough to price a referee off."""
    if not referee:
        return 1.0, 0, False
    hit = refstats.get((league, _norm(referee)))
    if not hit:
        for (l, name), v in refstats.items():
            if l == league and _norm(referee).split()[-1] in name:
                hit = v
                break
    if not hit:
        return 1.0, 0, False
    mult, n, sig = hit
    if not sig:
        mult = 1.0 + 0.5 * (mult - 1.0)      # shrink an unproven effect halfway
    return mult, n, sig


# ------------------------------------------------------- negative binomial

def nb_pmf(k, mu, disp):
    """Negative binomial with mean mu and variance disp*mu (disp >= 1).
    Falls back to Poisson when disp ~ 1."""
    if disp <= 1.0001:
        return math.exp(-mu + k * math.log(mu) - math.lgamma(k + 1)) if mu > 0 else (1.0 if k == 0 else 0.0)
    r = mu / (disp - 1.0)
    p = r / (r + mu)
    return math.exp(math.lgamma(k + r) - math.lgamma(r) - math.lgamma(k + 1)
                    + r * math.log(p) + k * math.log(1 - p))


def p_over(line, mu, disp, cap=60):
    """P(count > line) for a .5 line."""
    below = sum(nb_pmf(k, mu, disp) for k in range(0, int(math.floor(line)) + 1))
    return max(0.0, min(1.0, 1.0 - below))


def p_atleast(n, mu, disp, cap=60):
    return max(0.0, min(1.0, 1.0 - sum(nb_pmf(k, mu, disp) for k in range(0, n))))


# ----------------------------------------------------------------- odds math

def dec(a):
    a = float(a)
    return a / 100.0 + 1.0 if a > 0 else 100.0 / (-a) + 1.0


def two_way_fair(o):
    p = [1 / dec(o[0]), 1 / dec(o[1])]
    s = sum(p)
    return [x / s for x in p]


def kelly(p, d):
    b = d - 1.0
    return max(0.0, (p * b - (1 - p)) / b)


def line(label, odds, model_p, fair_p, flag_at=0.02, tested=True):
    ed = model_p - fair_p
    k = kelly(model_p, dec(odds))
    mark = ""
    if ed > flag_at:
        mark = "  <<FLAG" if tested else "  <<flag(untested)"
    print(f"    {label:<26} {str(odds):>7}  model {model_p*100:5.1f}%  "
          f"fair {fair_p*100:5.1f}%  {ed*100:+5.1f}pp  K {k*100:4.1f}%{mark}")


# ---------------------------------------------------------------------- main

def main():
    board = json.load(open(os.path.join(HERE, "board.json"), encoding="utf-8"))
    rows = load_series()

    tables = {}
    for fld in ("corners", "yellow_cards", "offsides", "shots"):
        tables[fld] = rate_tables(rows, fld)
    refstats = ref_stats(rows)

    for g in board:
        lg, home, away = g["league"], g["home"], g["away"]
        o = g["odds"]
        print("\n" + "=" * 92)
        print(f"{home}  vs  {away}   [{lg}]   {g.get('kickoff','')}")
        print("=" * 92)

        mk, err = P.predict(lg, home, away)
        if err:
            print(f"  model unavailable: {err}")
            continue
        print(f"  model goals: {mk['lam_h']:.2f} - {mk['lam_a']:.2f}"
              f"   (total {mk['lam_h']+mk['lam_a']:.2f})")

        # ---- 1X2 (tested, and known to lose to the close)
        if "1x2" in o:
            raw = np.array([1 / dec(x) for x in o["1x2"]])
            fair = M.devig_power(raw)
            mod = [mk["home"], mk["draw"], mk["away"]]
            print(f"  -- 1X2  (overround {100*(raw.sum()-1):.1f}%)")
            for i, lab in enumerate(("Home", "Draw", "Away")):
                line(lab, o["1x2"][i], mod[i], fair[i])
        elif "double_chance" in o:
            d = o["double_chance"]
            # P(X) = 1 - P(not X); normalise since the DC book carries its own vig
            nx = [1 - 1 / dec(d["draw_or_away"]),
                  1 - 1 / dec(d["home_or_away"]),
                  1 - 1 / dec(d["home_or_draw"])]
            s = sum(nx)
            fair = [x / s for x in nx]
            mod = [mk["home"], mk["draw"], mk["away"]]
            print("  -- 1X2  (derived from Double Chance; no direct 1X2 on the board)")
            for i, lab in enumerate(("Home", "Draw", "Away")):
                print(f"    {lab:<26} {'--':>7}  model {mod[i]*100:5.1f}%  "
                      f"fair {fair[i]*100:5.1f}%  {(mod[i]-fair[i])*100:+5.1f}pp")

        # ---- goals two-ways
        for key, labs, mkeys in (("ou25", ("Over 2.5", "Under 2.5"), ("over25", "under25")),
                                 ("btts", ("BTTS Yes", "BTTS No"), ("btts_yes", "btts_no"))):
            if key in o:
                fair = two_way_fair(o[key])
                print(f"  -- {key}")
                for i in range(2):
                    line(labs[i], o[key][i], mk[mkeys[i]], fair[i])

        # ---- counting markets (UNTESTED)
        print("  -- counting markets   [no historical odds exist -- untested]")
        for fld, lbl in (("corners", "corners"), ("yellow_cards", "cards"),
                         ("offsides", "offsides")):
            means, base, disp = tables[fld]
            eh, ea = expected(means, base, lg, home, away)
            if eh is None:
                print(f"    {lbl}: no rate data")
                continue
            mu = eh + ea
            d = disp.get(lg, 1.0)
            ah, aa = expected_additive(means, lg, home, away)
            alt = (ah + aa) if ah is not None else None
            spread = abs(mu - alt) if alt is not None else 0.0
            warn = "   <-- SPECIFICATIONS DISAGREE" if spread >= 0.75 else ""
            print(f"    {lbl:<10} multiplicative {mu:5.2f} | additive "
                  f"{alt if alt is None else round(alt,2)!s:>5}  "
                  f"(dispersion {d:.2f}){warn}")
            if alt is not None:
                mu = 0.5 * (mu + alt)     # average the two rather than trust one
                print(f"    {'':<10} -> using blend {mu:5.2f}")

            if fld == "corners" and "corners_10.5" in o:
                fair = two_way_fair(o["corners_10.5"])
                po = p_over(10.5, mu, d)
                line("  Corners Over 10.5", o["corners_10.5"][0], po, fair[0], tested=False)
                line("  Corners Under 10.5", o["corners_10.5"][1], 1 - po, fair[1], tested=False)

            if fld == "offsides":
                for n, key in ((3, "offsides_3plus"), (4, "offsides_4plus"),
                               (5, "offsides_5plus")):
                    if key in o:
                        pa = p_atleast(n, mu, d)
                        imp = 1 / dec(o[key])
                        print(f"      Offsides {n}+{'':<13} {str(o[key]):>7}  "
                              f"model {pa*100:5.1f}%  raw-implied {imp*100:5.1f}%  "
                              f"{(pa-imp)*100:+5.1f}pp   [one-way, vig not removable]")

            if fld == "yellow_cards" and "bookings_ou" in o:
                # "Bookings" usually counts a red as 2, so model yellows + 2*reds.
                mu_book = mu + 2.0 * red_rate(rows, lg, home, away)
                # Referee effect, measured on this ref's own matches.
                rmult, rn, rsig = ref_multiplier(refstats, lg, g.get("referee"))
                mu_ref = mu_book * rmult
                print(f"      bookings basis: yellows {mu:.2f} -> +2x reds "
                      f"{mu_book:.2f} -> ref x{rmult:.3f} = {mu_ref:.2f}")
                print(f"      referee {g.get('referee','?')}: {rn} matches, "
                      f"{'SIGNIFICANT' if rsig else 'within noise - treat as ~neutral'}")
                for ln, pair in o["bookings_ou"].items():
                    fair = two_way_fair(pair)
                    po = p_over(float(ln), mu_ref, d)
                    line(f"  Cards Over {ln}", pair[0], po, fair[0], tested=False)


if __name__ == "__main__":
    main()
