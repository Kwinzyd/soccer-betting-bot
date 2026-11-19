"""soccerlab command line.  python -m soccerlab <command> ...

Every command here is research/paper-only. None of them sends a Telegram
message unless --send is passed AND alerts are explicitly enabled for the
namespace; none of them can place a wager.
"""

import argparse
import sys


def main(argv=None):
    ap = argparse.ArgumentParser(prog="soccerlab")
    sub = ap.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("baseline", help="legacy baseline manifest")
    b.add_argument("action", choices=["freeze", "verify"])
    b.add_argument("--golden", action="store_true", help="also re-run legacy model and compare")

    r = sub.add_parser("model", help="model registry: freeze / verify / list")
    r.add_argument("action", choices=["freeze", "verify", "list"])
    r.add_argument("model_id", nargs="?")
    r.add_argument("--golden", action="store_true")

    sub.add_parser("audit", help="static audit checks over the repository")

    d = sub.add_parser("data", help="data verification and completeness report")
    d.add_argument("action", choices=["verify", "completeness"])

    bt = sub.add_parser("backtest", help="offline point-in-time walk-forward evaluation")
    bt.add_argument("--out", default=None, help="write JSON results here (default reports/)")
    bt.add_argument("--quick", action="store_true", help="one league, for smoke testing")

    sp = sub.add_parser("shadow", help="generate shadow predictions (never alerts)")
    sp.add_argument("--poll", required=True, help="path to a latest_poll.json snapshot (read-only)")
    sp.add_argument("--model", default="soccer-dc-pit-v1")

    pp = sub.add_parser("paper", help="paper predictions with risk checks (production-paper namespace)")
    pp.add_argument("--poll", required=True)
    pp.add_argument("--model", default=None, help="must be a PROMOTED model id")
    for p_ in (sp, pp):
        p_.add_argument("--replay", action="store_true",
                        help="decide as of the poll's own timestamp (+1 min) instead of now")
        p_.add_argument("--schedule-dir", default=None, help="directory with *_2026_matches.json snapshots")

    st = sub.add_parser("settle", help="settle paper/shadow entries from a schedule file")
    st.add_argument("--schedule-dir", default=None)

    sub.add_parser("health", help="inspect data freshness and pipeline health")

    a = ap.parse_args(argv)

    if a.cmd == "baseline":
        from . import legacy_baseline as LB
        if a.action == "freeze":
            LB.freeze_cmd()
            return 0
        return LB.verify_cmd(golden=a.golden)

    if a.cmd == "model":
        from . import registry
        return registry.cli(a.action, a.model_id, golden=a.golden)

    if a.cmd == "audit":
        from . import audit
        return audit.main()

    if a.cmd == "data":
        from . import datacheck
        return datacheck.main(a.action)

    if a.cmd == "backtest":
        from . import evaluation
        return evaluation.main(out=a.out, quick=a.quick)

    if a.cmd in ("shadow", "paper"):
        from . import live
        return live.main(a.cmd, poll=a.poll, model_id=a.model, replay=a.replay, schedule_dir=a.schedule_dir)

    if a.cmd == "settle":
        from . import live
        return live.settle_main(schedule_dir=a.schedule_dir)

    if a.cmd == "health":
        from . import datacheck
        return datacheck.main("health")
    return 2


if __name__ == "__main__":
    sys.exit(main())
