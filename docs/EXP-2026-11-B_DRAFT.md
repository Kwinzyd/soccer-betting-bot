# EXP-2026-11-B — Forward early-price validation (DRAFT, not locked)

**Status: draft.** The machine-readable protocol is
`registry/experiments/EXP-2026-11-B.json` (`"status": "draft"`);
`soccerlab.expb.evaluate.primary()` refuses to run until it is locked.
`soccer-dc-legacy` and `soccer-dc-pit-v1` stay frozen and unpromoted; no
model selects anything in this experiment.

**Question.** Do accessible, executable early prices consistently beat later
market prices? This is a measurement of prices, not a strategy search.

## 1. Pilot-data rule (prevents retro-registration)

Everything the logger records before `locked_at` is **pilot data**. It may be
used only for the data-quality acceptance criteria in §7, which involve no
comparison of entry prices with later prices. The primary population is
fixtures whose entry time *and* kickoff are at or after `locked_at`
(enforced in code). Quotes collected before the lock must never be
described as prospectively pre-registered evidence.

## 2. Data sources

| role | source | status |
|---|---|---|
| entry venues (candidates) | BallDontLie `/odds`: DraftKings, FanDuel moneyline (1X2, 90') | integrated; **legal access and obtainability unverified** |
| observational only | BallDontLie `/odds`: Polymarket | recorded, never eligible (liquidity, fees, execution, legality unverified) |
| schedule / reschedules | BallDontLie `/matches` (season 2026) | integrated, refreshed every 3h |
| strong closing reference | football-data.co.uk Betfair Exchange close `BFECH/BFECD/BFECA` | available: 94% of 2025-26 fixtures through May 2026, present in the 2026-27 file; **capture time unpublished; back/lay/mid unspecified**; CSVs published with a lag |
| not available | Pinnacle | official API closed to the public 2025-07-23; football-data's Pinnacle columns stop Jan 2026 |
| candidate upgrades (not integrated, paid / unverified) | The Odds API historical snapshots (5-min since Sep 2022; Pinnacle coverage unconfirmed), Betfair Historical Data (timestamped exchange prices, Advanced/Pro tiers), third-party Pinnacle feeds (OddsPapi etc., self-reported, possibly scraped) | need cost, licensing and coverage checks |

Pilot freshness and coverage, from the existing legacy log (prices only):
DraftKings and FanDuel quoted 112/114 and 113/114 fixtures; vendor
`updated_at` was a median 1.0 / 0.8 minutes old at observation (p90 3 min);
book margins were 5.1% / 6.9% (median); DK vs FD implied probabilities
differed by a median of 0.78pp (p90 2.1pp). Polymarket quoted 85/114 fixtures
at ~0.5% margin, with prices that sometimes sum below 1.

## 3. Collection (implemented: `python -m soccerlab expb run`, every 5 min)

* Regular odds poll every 2h for fixtures kicking off within 7 days.
* Targeted snapshots at T−60 (window 45–75 min) and T−15 (5–25 min) before
  the *currently scheduled* kickoff; each captured once; a window that closes
  without a successful poll is recorded as `missed`.
* Schedule refresh every 3h per league; a kickoff change appends a new fixture
  row and marks earlier captures `superseded`.
* Every venue/selection in every poll gets a row: `available`, `suspended`
  (null price), `missing` (venue absent), `invalid` (degenerate price,
  incoherent book, fixture not scheduled) or `post_kickoff`. Failed requests
  write a `failed` poll event and no quotes. Nothing is carried forward.
* Quality flags: `source_stale` (>2h), `source_ts_in_future`,
  `partial_book`, `duplicate_vendor_row`, `unknown_venue`, ….

## 4. Entry rule (model-free, one entry per fixture)

At E = kickoff − 24h: each approved venue's latest complete, unflagged book
retrieved in [E − 3h, E]. Reference fair = mean of the venues' power-de-vigged
probabilities. For each selection, the best approved price; the single entry
is the selection maximising best_price × fair − 1. **No EV threshold** in the
primary analysis: an EV > 0 rule would admit too few fixtures (5–7% margins
vs 0.78pp disagreement) to reach the sample floor. Opposing or repeated
entries are impossible by construction. Fixtures whose kickoff moves > 48h
from the kickoff known at entry are excluded as void.

## 5. Primary endpoint

**Closing EV** = entry_price × fair_close(selection) − 1, with fair_close
from the strong closing reference (Betfair Exchange close, power de-vig).

Secondary (descriptive, not gating): % of entries beating the reference
close; odds ratio entry/close; selection probability move; the same
endpoint at E = kickoff − 72h and − 6h; the EV > 0 subgroup.

### Separate, weaker experiment: EXP-2026-11-B-weak

If the external close is unavailable or covers < 90% of fixtures, the same
entries are compared with the approved venues' **own** last complete books
within 30 minutes before kickoff. This only shows whether these books' early
prices are better than their own later prices (price drift). It cannot show
value against a sharp market and is reported under its own id, never as the
primary result.

## 6. Statistical methodology

* One entry per fixture, so fixtures are the unit. Mean closing EV with a
  95% percentile bootstrap (2,000 replicates, seed 0), clustered by fixture
  and, as a robustness check, by ISO week.
* Pass: lower bound > 0 under both clusterings with n ≥ 300. Below 300 the
  result is inconclusive. Upper bound < 0 = fail.
* Power: EXP-2026-11-A's comparable statistic had SD ≈ 0.086 per entry, giving
  SE ≈ 0.005 at n = 300 and ≈ 0.003 at n = 800. Roughly 1,000 fixtures remain
  in 2026-27, so n ≈ 600–800 after exclusions is plausible.
* One primary endpoint, so no multiplicity correction; secondaries are
  descriptive only.
* No outcomes are needed. Realised ROI is out of scope for this experiment.

## 7. Data-quality acceptance criteria (evaluated on pilot data, ≥ 14 days, before locking)

| criterion | threshold |
|---|---|
| logger liveness (`runs` heartbeat) | ≥ 99% of 5-min slots |
| regular-poll freshness | no league with upcoming fixtures > 3h without a successful poll on ≥ 98% of days |
| T−60 and T−15 capture | ≥ 95% of non-rescheduled fixtures |
| approved-venue complete, unflagged book at the T−24h entry window | ≥ 90% of fixtures, per venue |
| source freshness | ≥ 95% of available quotes with vendor timestamp ≤ 10 min old |
| invalid/degenerate pre-kickoff quotes | ≤ 2% |
| external close matched (after aliases are complete) | ≥ 90% of kicked-off fixtures |
| obtainability spot checks (`expb spotcheck`) | ≥ 30 per approved venue; ≥ 90% within 0.5pp implied of the logged quote; stake acceptance recorded |
| authentication failures | none for 7 consecutive days |

## 8. Expected volume and API budget

~35 fixtures a week (1,027 left in 2026-27 after 2026-11-20); about 12
kickoff slots a week.

| item | per day | notes |
|---|---|---|
| odds requests | ~55 | 48 regular (12/day × 4 leagues) + ~7 targeted |
| schedule requests | ~128 | 32 refreshes × ~4 pages |
| quote rows | ~4,000 | 35 fixtures × 3 venues × 3 selections × 12, plus targeted |
| storage | ~2 MB | ~15 MB/week, ~400 MB for the rest of the season |

All of this shares the key's 5 req/min limit with the legacy logger if that
task is re-enabled. Each invocation is capped at ~18 requests (4 min).

## 9. Decisions required

Before **activation** (registering the logger task):
1. Authorize `research_tasks/install_expb_task.ps1 -Install`.
2. Confirm the BallDontLie key is valid (17 HTTP 401s on 2026-11-18/19)
   and whether the legacy logger task stays disabled; both share one rate
   limit.

Before **locking**:
3. Approved venues: are DraftKings and FanDuel legally available to you and
   usable at the stakes you would test? Polymarket stays observational.
4. Closing reference: accept the football-data Betfair Exchange close
   (untimed), or buy a timestamped source (Betfair Historical Data / The Odds
   API historical).
5. Download the 2026-27 football-data CSVs into `var/expb/fd/` and rerun
   `expb aliases`. Six promoted teams have no evidence-based alias yet:
   Coventry City, Hull City, Málaga, Deportivo, Racing Santander, Le Mans.
6. Confirm the parameters: entry offset 24h, tolerance 3h, no threshold,
   close window 30 min, min_n 300.
