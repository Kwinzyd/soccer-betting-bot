"""Logger configuration. Changing any of these during a locked experiment
requires a new experiment id."""

from datetime import timedelta

LEAGUES = ("epl", "laliga", "seriea", "ligue1")
SEASON = 2026
MARKET = "1x2_ft90"          # full-time result incl. stoppage time; no extra time/penalties
SELECTIONS = ("home", "draw", "away")

# Venue policy. 'candidate_unverified' = may become an executable venue once
# freshness, coverage and obtainability checks pass (docs/EXP-2026-11-B_DRAFT.md);
# 'observational' = recorded, never eligible for entries.
VENUES = {
    "draftkings": {"class": "sportsbook", "eligibility": "candidate_unverified"},
    "fanduel": {"class": "sportsbook", "eligibility": "candidate_unverified"},
    "polymarket": {"class": "prediction_market", "eligibility": "observational"},
}
EXPECTED_VENUES = tuple(VENUES)

REGULAR_INTERVAL = timedelta(hours=2)
LOOKAHEAD = timedelta(days=7)
SCHEDULE_REFRESH = timedelta(hours=3)

# Targeted snapshots: (name, minutes before scheduled kickoff, window lo, window hi).
# The runner is invoked every 5 minutes; a 20-30 minute window survives one
# skipped/blocked invocation.
TARGETS = (("T-60", 60, 45, 75), ("T-15", 15, 5, 25))

BATCH = 25                   # match ids per odds request
SPACING_SECONDS = 13.0       # 5 req/min key limit (shared with the legacy logger)
MAX_RUN_SECONDS = 240        # one invocation never exceeds ~4 minutes of API work
LOCK_STALE = timedelta(minutes=20)
INTERRUPTED_AFTER = timedelta(minutes=30)

# Quote quality thresholds
SOURCE_STALE = timedelta(hours=2)        # retrieved - vendor updated_at
SOURCE_FUTURE_SKEW = timedelta(minutes=5)
DEGENERATE_AMERICAN = 10000              # |odds| >= this is in-play/settled junk in this feed
