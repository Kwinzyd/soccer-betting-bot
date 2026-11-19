# Data lineage

## Sources

| source | raw location | produced by | consumed by | availability timestamp |
|---|---|---|---|---|
| BallDontLie matches (2022–2026) | `balldontlie_matches/*_page*.json` | `pull_balldontlie_v2.py` | `consolidate.py` | none from provider |
| consolidated matches | `balldontlie_consolidated/{lg}_{season}_matches.json` | `consolidate.py`; **2026 files overwritten every 6h by `log_odds.py --refresh-schedules`** | legacy model, `soccerlab.pit` | kickoff only; result time assumed kickoff + 3h |
| team match stats / xG (2025, 2026) | `balldontlie_team_match_stats/`, consolidated `*_team_match_stats.json` | `pull_balldontlie_v2.py` (run once, 2026-08-22) | legacy xG fit, `pit` (ablation) | none; ingestion 2026-08-22T04:44Z |
| teams (2025, 2026) | `balldontlie_teams/`, consolidated `*_teams.json` | same | names only | n/a |
| football-data.co.uk odds | `odds_hist/{E0,SP1,I1,F1}_{2223..2526}.csv` (+`SP1_2627`) | manual download | `model/joinodds.py` → `model/odds_joined.json` | closing = at kickoff; `PS*`/`Avg*`/`Max*` = Fri/Tue pre-closing snapshot |
| live odds log | `odds_logger/log/{lg}_{YYYY-MM}.jsonl` (gitignored) | `log_odds.py` every 3h | `settle.py`, `soccerlab.clv` | `observed_at` (run start, UTC) + `vendor_updated_at` |
| latest poll | `odds_logger/latest_poll.json` | `log_odds.py` | `generate_live_card.py`, `soccerlab shadow` | `observed_at` |
| api-football fixtures/xG (partial) | `fixtures/`, `xg/` | `pull_xg.py` | nothing in the model | none |
| FotMob rosters | `rosters/`, `active_rosters.json` | `fetch_rosters.py` daily | nothing in the model | none |

Season labels: BallDontLie season 2025 = 2025-26; football-data folder `2526`
maps to season 2025 (`joinodds.SEASON`).

## Point-in-time rules (`soccerlab/pit.py`)

1. **Results** are visible at `result_at = kickoff + 3h`. Scheduled fixtures
   carry `home_score = away_score = 0` in this feed and are never results.
2. **xG** is visible at `kickoff + 24h` (`provider_lag`, optimistic) or at the
   repository's actual ingestion time 2026-08-22 (`ingestion`, strict). The
   feed keeps no revision history, so revised values cannot be detected; any
   value whose availability time is after a cutoff is excluded.
3. **League membership** for season S = teams on S's fixture list (published
   pre-season). Teams first appearing in later seasons do not exist for a fit.
4. **Promoted** = on S's fixture list and absent from every earlier season in
   the data. The 2022 season is left-censored (everyone is "established").
5. **Promoted prior** for S: mean atk/def of promoted teams in each earlier
   cohort season c (2022 < c < S), each cohort fitted on its own season without
   decay; 0 if no earlier cohort.
6. **Decision times**: close = kickoff − 1h, early = kickoff − 4d. A fixture
   uses the latest weekly refit (00:00 UTC) whose cutoff ≤ decision time.
7. **Odds**: closing columns only for closing-time decisions; early columns
   only for early decisions; live quotes only if observed at or before the
   decision time and strictly before kickoff, and only if they pass
   `odds.validate_1x2`.

## Canonical identifiers

* `match_uid = "bdl:{league}:{match_id}"` (BallDontLie ids are unique per
  league feed; EPL lives in a different id space at v2).
* quotes keyed by (match_uid, market, selection, vendor, observed_at).
* predictions keyed by (namespace, model_id, match_uid, market, decision_time).
* one paper entry per (namespace, match_uid, market).

## Research store layers (`soccerlab/store.py`)

raw_responses → quotes / outcomes → predictions → decisions → paper_entries →
settlements. All append-only (SQLite triggers abort UPDATE/DELETE); outcome
and settlement corrections are new rows (`settlements.supersedes`).

## Fingerprints

Every evaluation report lists SHA-256 of each input file it read
(`reports/EXP-2026-11-A.md`, bottom). Model manifests record SHA-256 for all
code and data files at their commit; `verify` re-checks both the git history
and the working tree.
