"""EXP-2026-11-A: point-in-time evaluation against market baselines.

    python -m soccerlab backtest            full run -> reports/
    python -m soccerlab backtest --quick    one league, smoke test

Implements docs/EXPERIMENT_PLAN.md exactly; gate thresholds are literals
copied from that plan and must not be edited under this experiment id.
"""

import csv
import dataclasses
import json
import time
from collections import defaultdict

import numpy as np

from . import clv, dcpit, legacy, metrics, pit
from .hashing import atomic_write_json, atomic_write_text, sha256_file
from .legacy_baseline import BASELINE_COMMIT
from .odds import fair_from_decimals
from .paths import DATA_DIR, LEGACY_MODEL_DIR, REPORTS_DIR
from .walkforward import refit_grid, walk_forward

EXP_ID = "EXP-2026-11-A"
LEAGUES = ["epl", "laliga", "seriea", "ligue1"]
PRIMARY = [2023, 2024, 2025]
SEASONS = [2023, 2024, 2025, 2026]
N_BOOT = 2000

CFG_V1 = dcpit.DCConfig()
CFG_XG = dataclasses.replace(CFG_V1, xg_alpha=0.3)
CFG_XG14 = dataclasses.replace(CFG_XG, refit_days=14)

# ---- locked thresholds (docs/EXPERIMENT_PLAN.md §5)
GATE_B_ECE = 0.020
GATE_B_BAND_GAP, GATE_B_BAND_N, GATE_B_MIN_N = 0.05, 200, 300
GATE_C_MIN_N = 1000
GATE_D_THR, GATE_D_MIN_N = 0.02, 1000
EV_THRESHOLDS = (0.02, 0.05)
BLEND_GRID = np.round(np.arange(0.0, 1.0001, 0.05), 2)


def _fair(o, key):
    return np.asarray(fair_from_decimals(o[key]), dtype=float) if key in o else None


def _week(kickoff):
    y, w, _ = kickoff.isocalendar()
    return y * 100 + w


# ------------------------------------------------------------------ build

def build_rows(leagues, log=print):
    with open(LEGACY_MODEL_DIR / "odds_joined.json", encoding="utf-8") as f:
        odds = json.load(f)
    lds = {lg: pit.load_league(lg, DATA_DIR, xg_mode="provider_lag") for lg in leagues}
    preds = {}
    for name, cfg in (("pit_v1", CFG_V1), ("pit_xg", CFG_XG), ("pit_xg_r14", CFG_XG14)):
        for lg in leagues:
            t = time.time()
            for p in walk_forward(lds[lg], SEASONS, cfg):
                preds[(name, p.policy, lg, p.mid)] = p
            log(f"  {name:10s} {lg:7s} {time.time() - t:5.1f}s")

    legacy_p = {}
    t = time.time()
    with legacy.commit_data(BASELINE_COMMIT) as mods:
        E = mods["evaluate"]
        for lg in leagues:
            for block in ([2022, 2023, 2024, 2025], [2026]):
                for mid, p, _res, _pov, _yov in E.walk_predictions(lg, block):
                    legacy_p[(lg, int(mid))] = np.asarray(p, dtype=float)
    log(f"  legacy     all     {time.time() - t:5.1f}s")

    rows = []
    for lg in leagues:
        ld = lds[lg]
        base = {}
        for s in SEASONS:
            grid = refit_grid(ld, s, 7)
            hist = [m.outcome for m in ld.available(grid[0])] if grid else []
            c = np.bincount(hist, minlength=3).astype(float) if hist else np.ones(3)
            base[s] = c / c.sum()
        for m in ld.matches:
            if not m.final or m.season not in SEASONS:
                continue
            r = {"league": lg, "season": m.season, "mid": m.mid, "kickoff": m.kickoff, "week": _week(m.kickoff),
                 "y": m.outcome, "odds": odds.get(lg, {}).get(str(m.mid), {}),
                 "p": {"base_rates": base[m.season], "uniform": np.full(3, 1 / 3)}}
            for name in ("pit_v1", "pit_xg", "pit_xg_r14"):
                for pol in ("close", "early"):
                    pr = preds.get((name, pol, lg, m.mid))
                    if pr is not None:
                        r["p"][f"{name}_{pol}"] = np.asarray(pr.p)
                        r.setdefault("meta", {})[f"{name}_{pol}"] = {"cutoff": pr.info_cutoff.isoformat(),
                                                                      "ok": pr.model_ok}
            if (lg, m.mid) in legacy_p:
                r["p"]["legacy"] = legacy_p[(lg, m.mid)]
            for key, lab in (("pinnacle_close", "mkt_close"), ("pinnacle_open", "mkt_early"),
                             ("avg_close", "mkt_avg_close")):
                f = _fair(r["odds"], key)
                if f is not None:
                    r["p"][lab] = f
            rows.append(r)
    fingerprints = {lg: lds[lg].fingerprint for lg in leagues}
    fingerprints["odds_joined.json"] = sha256_file(LEGACY_MODEL_DIR / "odds_joined.json")
    priors = {}
    for lg in leagues:
        for s in SEASONS:
            g = refit_grid(lds[lg], s, 7)
            priors[f"{lg}:{s}"] = list(dcpit.promoted_prior(lds[lg], s, g[0])) if g else None
    return rows, fingerprints, priors


# --------------------------------------------------------------- metrics

def _P(rows, key):
    return np.vstack([r["p"][key] for r in rows]), np.array([r["y"] for r in rows])


def forecast(rows, keys):
    out = {}
    for k in keys:
        P, y = _P(rows, k)
        out[k] = {"n": len(y), "logloss": float(metrics.logloss_rows(P, y).mean()),
                  "brier": float(metrics.brier_rows(P, y).mean()), "ece": metrics.ece_classwise(P, y)[0]}
    return out


def paired(rows, key, ref, seed=0):
    P, y = _P(rows, key)
    Q, _ = _P(rows, ref)
    d = metrics.logloss_rows(P, y) - metrics.logloss_rows(Q, y)
    fx = [f"{r['league']}:{r['mid']}" for r in rows]
    wk = [r["week"] for r in rows]
    return {"n": len(d), "fixture": metrics.bootstrap_mean(d, fx, N_BOOT, seed),
            "week": metrics.bootstrap_mean(d, wk, N_BOOT, seed)}


def calibration(rows, key):
    P, y = _P(rows, key)
    return {lab: metrics.calibration_table(P[:, k], (y == k).astype(float))
            for k, lab in enumerate(("home", "draw", "away"))}


def blend_test(rows, key, ref="mkt_close", fit_seasons=(2023, 2024), test_seasons=(2025,)):
    fit = [r for r in rows if r["season"] in fit_seasons]
    test = [r for r in rows if r["season"] in test_seasons]

    def ll(rs, w):
        P, y = _P(rs, key)
        Q, _ = _P(rs, ref)
        B = (1 - w) * Q + w * P
        return metrics.logloss_rows(B / B.sum(axis=1, keepdims=True), y)

    curve = {float(w): float(ll(fit, w).mean()) for w in BLEND_GRID}
    w_star = min(curve, key=lambda w: (curve[w], w))
    d = ll(test, w_star) - ll(test, 0.0)
    fx = [f"{r['league']}:{r['mid']}" for r in test]
    return {"fit_seasons": list(fit_seasons), "test_seasons": list(test_seasons), "w_star": w_star,
            "fit_curve": curve, "test_delta": metrics.bootstrap_mean(d, fx, N_BOOT, 0)}


# --------------------------------------------------------------- betting

def bets(rows, price_key, thr, prob_key=None, fair_ref=None, entry_fair_key=None):
    """One selection per fixture, argmax EV, flat 1u, chronological."""
    out, no_bet, no_price = [], 0, 0
    for r in sorted(rows, key=lambda r: (r["kickoff"], r["league"], r["mid"])):
        o = r["odds"]
        if price_key not in o or (prob_key and prob_key not in r["p"]) or (fair_ref and fair_ref not in o):
            no_price += 1
            continue
        price = np.asarray(o[price_key], dtype=float)
        prob = r["p"][prob_key] if prob_key else _fair(o, fair_ref)
        ev = prob * price - 1.0
        k = int(np.argmax(ev))
        if ev[k] <= thr:
            no_bet += 1
            continue
        pnl = price[k] - 1.0 if r["y"] == k else -1.0
        b = {"league": r["league"], "season": r["season"], "mid": r["mid"], "week": r["week"],
             "kickoff": r["kickoff"].isoformat(), "sel": k, "price": float(price[k]), "ev": float(ev[k]),
             "pnl": float(pnl), "stake": 1.0, "fixture": f"{r['league']}:{r['mid']}"}
        if "pinnacle_close" in o and entry_fair_key and entry_fair_key in o:
            fc = _fair(o, "pinnacle_close")
            fe = _fair(o, entry_fair_key)
            b.update(clv.selection_clv(price[k], fe[k], o["pinnacle_close"][k], fc[k]))
            b["close_price"] = float(o["pinnacle_close"][k])
        out.append(b)
    return out, {"no_bet": no_bet, "no_price": no_price}


def bet_summary(bs, extra=None):
    if not bs:
        return {"n": 0, "verdict": "inconclusive (no bets)", **(extra or {})}
    pnl = np.array([b["pnl"] for b in bs])
    st = np.ones(len(bs))
    fx = [b["fixture"] for b in bs]
    s = {"n": len(bs), "turnover": float(st.sum()), "profit_units": float(pnl.sum()),
         "roi": metrics.bootstrap_ratio(pnl, st, fx, N_BOOT, 0),
         "roi_week_blocks": metrics.bootstrap_ratio(pnl, st, [b["week"] for b in bs], N_BOOT, 0),
         "max_drawdown_units": metrics.max_drawdown(pnl),
         "mean_price": float(np.mean([b["price"] for b in bs])),
         "by_league": {}, "by_season": {}, "by_price_band": {}}
    for field, bucket in (("by_league", lambda b: b["league"]), ("by_season", lambda b: str(b["season"])),
                          ("by_price_band", lambda b: "<2" if b["price"] < 2 else "2-3" if b["price"] < 3
                           else "3-5" if b["price"] < 5 else ">=5")):
        g = defaultdict(list)
        for b in bs:
            g[bucket(b)].append(b["pnl"])
        s[field] = {k: {"n": len(v), "roi": float(np.mean(v))} for k, v in sorted(g.items())}
    top = sorted(pnl)[:-10] if len(pnl) > 10 else []
    s["roi_without_top10_winners"] = float(np.mean(top)) if len(top) else None
    if "odds_ratio" in bs[0]:
        with_close = [b for b in bs if "odds_ratio" in b]
        s["clv"] = clv.summarize(with_close, n_boot=N_BOOT)
    s.update(extra or {})
    return s


# ------------------------------------------------------------------ gates

def gates(res):
    g = {}
    pv = res["primary"]
    g["A"] = {"criterion": "pytest suite + baseline verify --golden + leakage tests",
              "status": "evaluated separately (see test run log)"}
    seasons_ok, band_fail = [], []
    for s, v in res["by_season_ece"].items():
        if int(s) not in PRIMARY:
            continue
        if v["n"] < GATE_B_MIN_N:
            seasons_ok.append(None)
        else:
            seasons_ok.append(v["ece"] <= GATE_B_ECE)
    for lab, tbl in res["calibration_pit_v1_close"].items():
        for b in tbl:
            if b["n"] >= GATE_B_BAND_N and abs(b["gap"]) > GATE_B_BAND_GAP:
                band_fail.append((lab, b["lo"], b["hi"], b["gap"]))
    g["B"] = {"status": "pass" if all(seasons_ok) and not band_fail else
              ("inconclusive" if None in seasons_ok else "fail"),
              "per_season_ece": res["by_season_ece"], "bands_failing": band_fail}

    d = pv["paired"]["pit_v1_close_vs_mkt_close"]
    bl = res["blend"]["pit_v1_close"]["test_delta"]
    c_ok = (d["n"] >= GATE_C_MIN_N and d["fixture"]["hi"] < 0 and d["week"]["hi"] < 0 and bl["hi"] < 0)
    c_fail = d["fixture"]["lo"] > 0
    g["C"] = {"status": "pass" if c_ok else ("fail" if c_fail else "inconclusive"),
              "delta_fixture_ci": d["fixture"], "delta_week_ci": d["week"], "blend_2025": bl,
              "w_star": res["blend"]["pit_v1_close"]["w_star"]}

    e = res["policies"][f"EARLY-PIN@{GATE_D_THR}"]
    if e["n"] < GATE_D_MIN_N:
        dstat = f"inconclusive (n={e['n']} < {GATE_D_MIN_N})"
    else:
        roi_lo = e["roi"]["lo"]
        clv_lo = e["clv"]["ev_vs_close_fair"]["lo"]
        dstat = "pass" if roi_lo > 0 and clv_lo > 0 else ("fail" if e["roi"]["hi"] < 0 or
                                                          e["clv"]["ev_vs_close_fair"]["hi"] < 0 else "inconclusive")
    g["D"] = {"status": dstat, "policy": f"EARLY-PIN@{GATE_D_THR}", "n": e["n"],
              "roi_ci": e.get("roi"), "clv_ev_vs_close_ci": (e.get("clv") or {}).get("ev_vs_close_fair")}

    lg_neg = sum(1 for v in res["by_league_delta"].values() if v["mean"] < 0)
    s_neg = all(v["mean"] < 0 for k, v in res["by_season_delta"].items() if int(k) in PRIMARY)
    g["E"] = {"status": "pass" if (lg_neg >= 3 and s_neg and g["D"]["status"] == "pass") else "fail",
              "leagues_with_negative_delta": lg_neg, "all_primary_seasons_negative": s_neg}
    g["F"] = {"status": "not started (prospective window opens 2026-11-20T00:00Z)"}
    g["G"] = {"status": "not evaluated (requires 14 days of shadow operation)"}
    eligible = g["C"]["status"] == "pass"
    g["promotion"] = ("ELIGIBLE for shadow forward test" if eligible else
                      "NOT ELIGIBLE: fails Gate C (no incremental value over the market)")
    return g


# ------------------------------------------------------------------- run

def analyse(rows, fingerprints, priors):
    res = {"experiment": EXP_ID, "fingerprints": fingerprints, "pit_promoted_priors": priors}
    need = ["pit_v1_close", "pit_v1_early", "pit_xg_close", "pit_xg_r14_close", "legacy", "mkt_close",
            "base_rates", "uniform"]
    prim = [r for r in rows if r["season"] in PRIMARY and all(k in r["p"] for k in need)]
    res["population"] = {"all_final_rows": len(rows), "primary_common": len(prim),
                         "primary_with_early_market": sum("mkt_early" in r["p"] for r in prim),
                         "seasons": PRIMARY}
    keys = need + ["pit_xg_early"]
    keys = [k for k in keys if all(k in r["p"] for r in prim)]
    res["primary"] = {"forecast": forecast(prim, keys), "paired": {}}
    for k in ("pit_v1_close", "pit_xg_close", "legacy", "base_rates"):
        res["primary"]["paired"][f"{k}_vs_mkt_close"] = paired(prim, k, "mkt_close")
    early = [r for r in prim if "mkt_early" in r["p"]]
    res["early_population"] = forecast(early, ["pit_v1_early", "pit_v1_close", "mkt_early", "mkt_close"])
    res["primary"]["paired"]["pit_v1_early_vs_mkt_early"] = paired(early, "pit_v1_early", "mkt_early")
    res["primary"]["paired"]["mkt_early_vs_mkt_close"] = paired(early, "mkt_early", "mkt_close")
    # leakage attribution: legacy vs its PIT twin on identical fixtures
    res["leakage"] = {
        "legacy_vs_pit_xg_r14": paired(prim, "legacy", "pit_xg_r14_close"),
        "pit_xg_r14_vs_pit_xg": paired(prim, "pit_xg_r14_close", "pit_xg_close"),
        "pit_xg_vs_pit_v1": paired(prim, "pit_xg_close", "pit_v1_close"),
    }

    res["by_season_delta"], res["by_league_delta"], res["by_season_ece"] = {}, {}, {}
    for s in SEASONS:
        rs = [r for r in rows if r["season"] == s and "pit_v1_close" in r["p"] and "mkt_close" in r["p"]]
        if rs:
            res["by_season_delta"][str(s)] = paired(rs, "pit_v1_close", "mkt_close")["fixture"]
        rs2 = [r for r in rows if r["season"] == s and "pit_v1_close" in r["p"]]
        if rs2:
            P, y = _P(rs2, "pit_v1_close")
            res["by_season_ece"][str(s)] = {"n": len(y), "ece": metrics.ece_classwise(P, y)[0],
                                            "logloss": float(metrics.logloss_rows(P, y).mean()),
                                            "logloss_base_rates": float(metrics.logloss_rows(*_P(rs2, "base_rates")).mean())}
    for lg in LEAGUES:
        rs = [r for r in prim if r["league"] == lg]
        if rs:
            res["by_league_delta"][lg] = paired(rs, "pit_v1_close", "mkt_close")["fixture"]
    res["calibration_pit_v1_close"] = calibration(prim, "pit_v1_close")
    res["calibration_mkt_close"] = calibration(prim, "mkt_close")
    res["blend"] = {k: blend_test(prim, k) for k in ("pit_v1_close", "legacy")}

    allp = [r for r in rows if r["season"] in PRIMARY]
    pol = {}
    for thr in EV_THRESHOLDS:
        b, x = bets(allp, "pinnacle_open", thr, prob_key="pit_v1_early", entry_fair_key="pinnacle_open")
        pol[f"EARLY-PIN@{thr}"] = bet_summary(b, x)
        b, x = bets(allp, "pinnacle_close", thr, prob_key="pit_v1_close")
        pol[f"CLOSE-PIN@{thr}"] = bet_summary(b, x)
        b, x = bets(allp, "max_open", thr, prob_key="pit_v1_early", entry_fair_key="pinnacle_open")
        pol[f"EARLY-MAX@{thr} (optimistic)"] = bet_summary(b, x)
        b, x = bets(allp, "pinnacle_close", thr, prob_key="legacy")
        pol[f"LEGACY-CLOSE-PIN@{thr}"] = bet_summary(b, x)
    for thr in (0.0, 0.02):
        b, x = bets(allp, "max_open", thr, fair_ref="pinnacle_open", entry_fair_key="pinnacle_open")
        pol[f"SHOP-EARLY@{thr} (no model)"] = bet_summary(b, x)
        b, x = bets(allp, "max_close", thr, fair_ref="pinnacle_close")
        pol[f"SHOP-CLOSE@{thr} (no model)"] = bet_summary(b, x)
    # sensitivity: early decision, but filled at the closing price (late execution)
    late = []
    for b in bets(allp, "pinnacle_open", GATE_D_THR, prob_key="pit_v1_early", entry_fair_key="pinnacle_open")[0]:
        if "close_price" in b:
            y = next(r["y"] for r in allp if r["league"] == b["league"] and r["mid"] == b["mid"])
            late.append(dict(b, price=b["close_price"], pnl=(b["close_price"] - 1) if y == b["sel"] else -1.0))
    pol[f"EARLY-PIN@{GATE_D_THR} filled at close (sensitivity)"] = bet_summary(
        [{k: v for k, v in b.items() if k not in ("odds_ratio",)} for b in late])
    res["policies"] = pol

    # README reproduction (legacy 2022-2025 vs Pinnacle close)
    rep = [r for r in rows if r["season"] in (2022, 2023, 2024, 2025)
           and "legacy" in r["p"] and "mkt_close" in r["p"]]
    if rep:
        f = forecast(rep, ["legacy", "mkt_close"])
        res["legacy_readme_reproduction"] = {"note": "README: n=3804, model 0.9982, market 0.9735 (2022-25)",
                                             "seasons_available_here": "2023-2025 only (2022 not in SEASONS)",
                                             **f}
    res["gates"] = gates(res)
    return res


def _jsonable(o):
    if isinstance(o, dict):
        return {str(k): _jsonable(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_jsonable(v) for v in o]
    if isinstance(o, np.ndarray):
        return [_jsonable(v) for v in o.tolist()]
    if isinstance(o, (np.floating,)):
        return float(o)
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, float) and not np.isfinite(o):
        return None
    return o


def write_predictions_csv(rows, path):
    keys = sorted({k for r in rows for k in r["p"]})
    lines = []
    header = ["league", "season", "mid", "kickoff", "y"] + [f"{k}_{s}" for k in keys for s in ("h", "d", "a")]
    for r in sorted(rows, key=lambda r: (r["kickoff"], r["league"], r["mid"])):
        vals = [r["league"], r["season"], r["mid"], r["kickoff"].isoformat(), r["y"]]
        for k in keys:
            vals += [f"{v:.10f}" for v in r["p"][k]] if k in r["p"] else ["", "", ""]
        lines.append(vals)
    import io
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(header)
    w.writerows(lines)
    atomic_write_text(path, buf.getvalue())


def main(out=None, quick=False):
    t0 = time.time()
    leagues = ["epl"] if quick else LEAGUES
    print(f"{EXP_ID}: building walk-forward predictions for {leagues}")
    rows, fp, priors = build_rows(leagues)
    res = analyse(rows, fp, priors)
    res["runtime_seconds"] = round(time.time() - t0, 1)
    res["leagues"] = leagues
    REPORTS_DIR.mkdir(exist_ok=True)
    tag = EXP_ID + ("-quick" if quick else "")
    out = out or REPORTS_DIR / f"{tag}.json"
    atomic_write_json(out, _jsonable(res))
    if not quick:
        write_predictions_csv(rows, REPORTS_DIR / f"{tag}-predictions.csv")
    from . import report
    atomic_write_text(REPORTS_DIR / f"{tag}.md", report.render(_jsonable(res)))
    g = res["gates"]
    print(f"wrote {out}  ({res['runtime_seconds']}s)")
    for k in "ABCDEFG":
        print(f"  Gate {k}: {g[k]['status']}")
    print(f"  => {g['promotion']}")
    return 0
