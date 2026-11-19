"""python -m soccerlab expb <action>

  run                      one logger invocation (what the scheduled task calls)
  monitor                  liveness / coverage / freshness checks (exit 1 on red)
  quality                  data-quality summary (allowed before the protocol lock)
  aliases                  rebuild football-data -> BallDontLie team alias table
  closing-import CSV LEAGUE   import Betfair Exchange closing prices from a downloaded football-data CSV
  spotcheck ...            record a manual venue price observation (obtainability evidence)
  spotcheck-report         agreement between spot checks and logged quotes
  evaluate                 primary analysis -- refuses until the protocol is locked
  probe [SCHEDULE_DIR]     minimal auth / permission / rate-limit probe (8 requests max)
"""

import argparse
import json
from datetime import timedelta

from ..odds import american_to_decimal
from ..timeutil import iso, parse_utc, utcnow
from .db import DB


def spotcheck_report(db, tolerance_minutes=10):
    rows = []
    for s in db.c.execute("SELECT * FROM spot_checks ORDER BY id"):
        t = parse_utc(s["observed_at"])
        lo = iso(t - timedelta(minutes=tolerance_minutes))
        hi = iso(t + timedelta(minutes=tolerance_minutes))
        q = db.c.execute("""SELECT american_odds, decimal_odds, retrieved_at FROM quotes WHERE fixture_uid=? AND venue=?
            AND selection=? AND status='available' AND retrieved_at BETWEEN ? AND ? ORDER BY retrieved_at DESC LIMIT 1""",
                         (s["fixture_uid"], s["venue"], s["selection"], lo, hi)).fetchone()
        obs = american_to_decimal(s["observed_american"])
        if q is None:
            rows.append({"id": s["id"], "result": "no logged quote within tolerance"})
            continue
        gap_pp = 100 * (1 / q["decimal_odds"] - 1 / obs)
        rows.append({"id": s["id"], "logged": q["american_odds"], "observed": s["observed_american"],
                     "implied_gap_pp": round(gap_pp, 3), "agree": abs(gap_pp) <= 0.5,
                     "stake_accepted": s["stake_accepted"]})
    return rows


def main(argv):
    ap = argparse.ArgumentParser(prog="soccerlab expb")
    ap.add_argument("action", choices=["run", "monitor", "quality", "aliases", "closing-import", "spotcheck",
                                       "spotcheck-report", "evaluate", "probe"])
    ap.add_argument("args", nargs="*")
    ap.add_argument("--db", default=None)
    ap.add_argument("--fixture"), ap.add_argument("--venue"), ap.add_argument("--selection")
    ap.add_argument("--american", type=float), ap.add_argument("--at"), ap.add_argument("--stake-accepted", type=float)
    ap.add_argument("--note")
    a = ap.parse_args(argv)

    if a.action == "run":
        from .logger import main as run_main
        return run_main(a.db)
    if a.action == "probe":
        from .probe import main as probe_main
        return probe_main(a.args[0] if a.args else "balldontlie_consolidated")
    if a.action == "aliases":
        from .closing import build_aliases
        res = build_aliases()
        for lg, m in res.items():
            print(f"  {lg}: {len(m)} team aliases")
        return 0
    with DB(a.db) as db:
        if a.action == "monitor":
            from .monitor import main as mon
            return mon(db)
        if a.action == "quality":
            from .evaluate import data_quality
            print(json.dumps(data_quality(db), indent=2))
            return 0
        if a.action == "closing-import":
            if len(a.args) != 2:
                ap.error("closing-import needs CSV LEAGUE")
            from .closing import import_closing
            rep, unmapped = import_closing(db, a.args[0], a.args[1])
            print(json.dumps({"result": rep, "unmapped_names": unmapped}, indent=2))
            return 0
        if a.action == "spotcheck":
            if not (a.fixture and a.venue and a.selection and a.american is not None and a.at):
                ap.error("spotcheck needs --fixture --venue --selection --american --at [--stake-accepted] [--note]")
            with db.tx() as c:
                c.execute("""INSERT INTO spot_checks (fixture_uid, venue, selection, observed_american, observed_at,
                    stake_accepted, note, recorded_at) VALUES (?,?,?,?,?,?,?,?)""",
                          (a.fixture, a.venue, a.selection, a.american, iso(parse_utc(a.at)), a.stake_accepted,
                           a.note, iso(utcnow())))
            print("recorded")
            return 0
        if a.action == "spotcheck-report":
            for r in spotcheck_report(db):
                print(f"  {r}")
            return 0
        if a.action == "evaluate":
            from .evaluate import ProtocolNotLocked, primary
            try:
                print(json.dumps(primary(db), indent=2, default=str))
            except ProtocolNotLocked as exc:
                print(f"REFUSED: {exc}")
                return 3
            return 0
    return 2
