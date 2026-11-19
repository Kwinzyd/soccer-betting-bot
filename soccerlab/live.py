"""Shadow / paper prediction runs and settlement, from read-only snapshots.

    python -m soccerlab shadow --poll var/snap/latest_poll.json [--replay] [--schedule-dir DIR]
    python -m soccerlab paper  --poll ... --model <PROMOTED id>
    python -m soccerlab settle [--schedule-dir DIR]

* shadow  writes predictions and simulated entries to the research store
          under namespace 'shadow'. It never alerts. Any registered
          challenger may run here.
* paper   identical pipeline under namespace 'paper', but refuses unless the
          model's registry status is 'promoted'. Today no model is.
Neither touches the legacy paper.sqlite3, telegram_config.json or the
legacy Telegram path.
"""

import glob
import json
import os
from collections import defaultdict
from datetime import timedelta

from . import clv, dcpit, ledger, pit, registry
from .hashing import sha256_file
from .odds import InvalidOdds, american_to_decimal, fair_from_decimals, validate_1x2
from .paths import DATA_DIR
from .settlement import grade_1x2
from .store import Store
from .timeutil import iso, parse_utc, utcnow

MAX_POLL_AGE = timedelta(minutes=30)
SEASON = 2026
SIDES = ("home", "draw", "away")
EXIT_REFUSED, EXIT_STALE = 3, 4


def _quotes_from_poll(rows, observed):
    by = defaultdict(dict)
    for r in rows:
        try:
            dec = tuple(american_to_decimal(r.get(f"moneyline_{s}_odds")) for s in SIDES)
            problems = validate_1x2(dec)
        except InvalidOdds as exc:
            dec, problems = None, [str(exc)]
        by[int(r["match_id"])][r["vendor"]] = {"dec": dec, "problems": problems,
                                               "vendor_updated_at": r.get("updated_at"), "observed_at": observed}
    return by


def _stale_schedule_flags(ld, decision):
    """Fixtures that should have finished hours ago but are still 'scheduled'
    mean the results feed is stale -> ratings would silently miss results."""
    late = [m for m in ld.matches if m.season == SEASON and m.status == "scheduled"
            and m.kickoff < decision - timedelta(hours=6)]
    return [f"{len(late)} past fixtures still unresulted in schedule ({ld.league})"] if late else []


def main(mode, poll, model_id=None, replay=False, schedule_dir=None, store=None, now=None):
    model_id = model_id or ("soccer-dc-pit-v1" if mode == "shadow" else None)
    status = registry.status_of(model_id) if model_id else "unregistered"
    if mode == "paper" and status != "promoted":
        print(f"REFUSED: paper namespace requires a promoted model; {model_id!r} is {status!r}. "
              f"Promoted models: {registry.load_index().get('promoted') or 'none'}.")
        return EXIT_REFUSED
    if mode == "shadow" and status not in ("challenger", "promoted"):
        print(f"REFUSED: {model_id!r} is {status!r}")
        return EXIT_REFUSED
    spec = registry.SPECS[model_id]
    cfg = spec["config"]

    with open(poll, encoding="utf-8") as f:
        snap = json.load(f)
    observed = parse_utc(snap["observed_at"])
    decision = observed + timedelta(minutes=1) if replay else (now or utcnow())
    if not timedelta(0) <= decision - observed <= MAX_POLL_AGE:
        print(f"STALE: poll observed {iso(observed)}, decision {iso(decision)}; refusing (fail closed)")
        return EXIT_STALE

    sdir = schedule_dir or DATA_DIR
    own = store is None
    store = store or Store()
    engine = ledger.RiskEngine(store)
    manifest = registry.manifest_sha(model_id)
    input_sha = sha256_file(poll)
    stats = defaultdict(int)
    try:
        for league, rows in sorted(snap["leagues"].items()):
            ld = pit.load_league(league, sdir, xg_mode=spec["xg_mode"])
            flags = _stale_schedule_flags(ld, decision)
            fit = dcpit.fit_pit(ld, SEASON, decision, cfg)
            quotes = _quotes_from_poll(rows, observed)
            events = []
            for mid, vend in quotes.items():
                for v, q in vend.items():
                    for i, s in enumerate(SIDES):
                        events.append({"match_uid": f"bdl:{league}:{mid}", "market": "1x2", "selection": s,
                                       "vendor": v, "decimal_odds": q["dec"][i] if q["dec"] and not q["problems"] else None,
                                       "status": "available" if q["dec"] and not q["problems"] else "invalid",
                                       "observed_at": observed,
                                       "vendor_updated_at": parse_utc(q["vendor_updated_at"]) if q["vendor_updated_at"] else None,
                                       "note": "; ".join(q["problems"]) or None})
            store.put_quote_events(events)
            for mid, vend in sorted(quotes.items()):
                uid = f"bdl:{league}:{mid}"
                try:
                    m = ld.get(mid)
                except KeyError:
                    store.log_decision(mode, model_id, uid, "1x2", decision, "no_bet", None, ["fixture not in schedule"])
                    stats["unknown_fixture"] += 1
                    continue
                if m.status != "scheduled" or m.kickoff <= decision:
                    stats["not_pregame"] += 1
                    continue
                if fit is None:
                    store.log_decision(mode, model_id, uid, "1x2", decision, "no_bet", None, ["no model fit"])
                    stats["no_bet: no model fit"] += 1
                    continue
                try:
                    r = dcpit.predict(fit, m.home, m.away, cfg)
                except dcpit.UnknownTeam as exc:
                    store.log_decision(mode, model_id, uid, "1x2", decision, "no_bet", None, [str(exc)])
                    stats["no_bet: unknown team"] += 1
                    continue
                p = r["p"]
                puid = store.put_prediction(mode, model_id, manifest, uid, "1x2", m.kickoff, decision, decision,
                                            {"home": p[0], "draw": p[1], "away": p[2], "lam": [r["lam_h"], r["lam_a"]]},
                                            input_sha)
                stats["predictions"] += 1
                best = None
                for v, q in sorted(vend.items()):
                    if not q["dec"] or q["problems"]:
                        continue
                    fair = fair_from_decimals(q["dec"])
                    for i, s in enumerate(SIDES):
                        ev = p[i] * q["dec"][i] - 1
                        if best is None or ev > best[0]:
                            best = (ev, s, v, q["dec"][i], fair[i], q["observed_at"])
                if best is None:
                    store.log_decision(mode, model_id, uid, "1x2", decision, "no_bet", None, ["no usable quote"])
                    stats["no_bet: no usable quote"] += 1
                    continue
                ev, s, v, d, fp, qt = best
                c = ledger.Candidate(namespace=mode, model_id=model_id, model_status=status, prediction_uid=puid,
                                     match_uid=uid, league=league, market="1x2", selection=s, vendor=v,
                                     decimal_odds=d, quote_observed_at=qt, decision_time=decision, kickoff=m.kickoff,
                                     model_prob=float(p[SIDES.index(s)]), fair_prob=float(fp),
                                     model_ok=r["model_ok"], data_flags=list(flags))
                entry, reasons = engine.consider(c)
                stats["entries" if entry else "no_bet"] += 1
                for reason in reasons:
                    stats["reason: " + reason.split("(")[0].strip()] += 1
    finally:
        if own:
            store.close()
    print(f"{mode} run {iso(decision)} model={model_id} ({status}) poll={iso(observed)}")
    for k, v in sorted(stats.items()):
        print(f"  {k:55s} {v}")
    print("  alerts: none (shadow never alerts)" if mode == "shadow" else "")
    return 0


def _schedule_rows(schedule_dir):
    out = {}
    for path in glob.glob(os.path.join(str(schedule_dir), f"*_{SEASON}_matches.json")):
        league = os.path.basename(path).split("_")[0]
        with open(path, encoding="utf-8") as f:
            for m in json.load(f):
                out[f"bdl:{league}:{m['id']}"] = m
    return out


def settle_main(schedule_dir=None, store=None):
    sched = _schedule_rows(schedule_dir or DATA_DIR)
    own = store is None
    store = store or Store()
    try:
        for ns in ("shadow", "paper"):
            def grader(e):
                m = sched.get(e["match_uid"])
                if m is None:
                    return "review", None, "fixture missing from schedule"
                return grade_1x2(e["selection"], m, entry_kickoff=e["kickoff"])
            n = ledger.settle_entries(store, ns, grader, source=f"schedule:{schedule_dir or DATA_DIR}")
            perf = ledger.performance(store, ns)
            clv_rows = []
            for e in store.entries(ns):
                ko = parse_utc(e["kickoff"])
                last = [q for q in store.quotes_asof(e["match_uid"], "1x2", ko - timedelta(microseconds=1))
                        if q["vendor"] == e["vendor"] and q["status"] == "available"]
                byside = {q["selection"]: q for q in last}
                if len(byside) == 3 and e["selection"] in byside:
                    close_dec = [byside[s]["decimal_odds"] for s in SIDES]
                    fair_c = fair_from_decimals(close_dec)
                    k = SIDES.index(e["selection"])
                    row = clv.selection_clv(e["decimal_odds"], e["fair_prob"] or 1 / e["decimal_odds"],
                                            close_dec[k], fair_c[k])
                    row.update(fixture=e["match_uid"], stake=e["stake"],
                               close_gap_seconds=(ko - parse_utc(byside[e["selection"]]["observed_at"])).total_seconds())
                    clv_rows.append(row)
            print(f"[{ns}] new settlement events {n}; {perf}")
            s = clv.summarize(clv_rows, n_boot=1000)
            if s["n"]:
                print(f"[{ns}] CLV vs last-observed proxy (NOT true close): n={s['n']} "
                      f"odds_ratio={s['odds_ratio']['mean']:+.4f} [{s['odds_ratio']['lo']:+.4f},{s['odds_ratio']['hi']:+.4f}] "
                      f"proxy gap median {s['close_gap_hours']['median']:.1f}h"
                      + ("  -- INCONCLUSIVE (n<300)" if s["n"] < 300 else ""))
    finally:
        if own:
            store.close()
    return 0
