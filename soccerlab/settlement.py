"""1X2 (90-minute) settlement rules.

Market: full-time result after 90 minutes plus stoppage time. Extra time and
penalties do not count. The BallDontLie league feed reports STATUS_FULL_TIME
for completed league matches; any other "completed" variant (e.g. AET or
penalties, which only occur in cups) is sent to manual review rather than
graded from a score that may include extra time.

Void rules (typical bookmaker convention, applied uniformly):
* postponed / cancelled                                    -> void
* kickoff moved by more than VOID_IF_MOVED_HOURS from the
  kickoff known at entry time                              -> void
* abandoned                                                -> void (books differ;
  flagged so a real-money user would check the specific book's rule)
* suspended / scheduled / in play                          -> pending
"""

from datetime import timedelta

from .timeutil import parse_utc

VOID_IF_MOVED_HOURS = 48
GRADABLE_STATUS = {"STATUS_FULL_TIME"}
VOID_STATUS = {"STATUS_POSTPONED", "STATUS_CANCELED", "STATUS_CANCELLED"}
ABANDONED_STATUS = {"STATUS_ABANDONED"}
PENDING_STATE = {"scheduled", "in", "suspended", "pre"}


def grade_1x2(selection, match, entry_kickoff=None):
    """-> (state, outcome, note). state in settled|void|pending|review."""
    if selection not in ("home", "draw", "away"):
        raise ValueError(f"not a 1X2 selection: {selection!r}")
    status, state = match.get("status"), match.get("status_state")
    kickoff = parse_utc(match["date"])

    if entry_kickoff is not None:
        moved = abs(kickoff - parse_utc(entry_kickoff))
        if moved > timedelta(hours=VOID_IF_MOVED_HOURS):
            return "void", "void", f"kickoff moved {moved} from entry-time kickoff"
    if status in VOID_STATUS:
        return "void", "void", status
    if status in ABANDONED_STATUS:
        return "void", "void", "abandoned (check book-specific rule)"
    if state == "final":
        if status not in GRADABLE_STATUS:
            return "review", None, f"final with status {status}: may include extra time"
        hs, as_ = match.get("home_score"), match.get("away_score")
        if hs is None or as_ is None:
            return "review", None, "final without score"
        winner = "home" if hs > as_ else ("away" if as_ > hs else "draw")
        return "settled", ("win" if winner == selection else "loss"), f"{hs}-{as_}"
    if state in PENDING_STATE or state is None:
        return "pending", None, state or "unknown"
    return "review", None, f"unhandled state {state}/{status}"
