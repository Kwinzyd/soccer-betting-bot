# EXP-2026-11-B — Forward early-price validation (DRAFT, not locked)

**Status: draft.** The machine-readable protocol is
`registry/experiments/EXP-2026-11-B.json` (`"status": "draft"`). Every
analysis function refuses to run until it is locked *and* every approved entry
venue is verified. Today the approved-venue list is empty. `soccer-dc-legacy`
and `soccer-dc-pit-v1` stay frozen and unpromoted; no model selects anything.

**Question.** Do accessible, executable early prices consistently beat later
market prices? This measures prices; it is not a strategy search.

## 1. Pilot and pilot-data rule

A 14-day observational pilot comes first (readiness:
[EXP-2026-11-B_PILOT_READINESS.md](EXP-2026-11-B_PILOT_READINESS.md)).
Pilot records are used only for the data-quality acceptance criteria (§8).
The lock must happen after the pilot ends. Every analysis uses only quotes
retrieved at or after `locked_at`, for fixtures kicking off after it
(enforced in `books_at(..., not_before=locked_at)` and tested). Pilot data
must never be described as prospectively pre-registered evidence.

## 2. Data sources

| role | source | status |
|---|---|---|
| observational feeds (entry *candidates*) | BallDontLie `/odds`: DraftKings, FanDuel moneyline (1X2, 90') | **observational**. Not executable until legal eligibility, market access, obtainability and source-to-screen agreement are verified (`venue_verification` in the protocol) |
| observational only | BallDontLie `/odds`: Polymarket | never an entry venue (liquidity, fees, execution, legality unverified) |
| schedule / reschedules | BallDontLie `/matches` (season 2026) | refreshed every 3h |
| strong closing reference | football-data.co.uk Betfair Exchange close `BFEC*` | **provisional**: see [EXP-2026-11-B_CLOSING_REFERENCE.md](EXP-2026-11-B_CLOSING_REFERENCE.md). 100% coverage of 2026-27 to date (419/419 fixtures matched, 0 quarantined) |
| not available | Pinnacle | public API closed 2025-07-23; absent from 2026-27 CSVs |
| timestamped upgrade | Betfair Historical Data (ADVANCED/PRO) | paid; needs a Betfair account |

The key was probed on 2026-11-19 (HTTP 200 for `matches` and `odds` in all
four leagues; declared limit 5 requests). Details are in the readiness report.

## 3. Collection (implemented: `python -m soccerlab expb run`, every 5 min)

* Regular odds poll every 2h for fixtures within 7 days.
* Targeted snapshots: T−60 (window 45–75 min) and T−15 (5–25 min) before the
  currently scheduled kickoff. Each is captured once; a window that closes
  without a successful poll is recorded as `missed`.
* Schedule refresh every 3h. A kickoff change appends a fixture row and marks
  earlier captures `superseded`.
* Every venue/selection in every poll gets a row: available, suspended,
  missing, invalid or post_kickoff. Failed requests write no quotes, and
  nothing is carried forward.
* Quality flags include `source_stale` (> 2h), `partial_book` and
  `cross_venue_discrepancy` (sportsbooks > 5pp apart on a selection; the
  pilot p90 is 2.1pp). Flagged books never enter an analysis.

## 4. Selection function (entry-time information only)

`soccerlab.expb.evaluate.select_entry(entry_books, threshold=None)`

* **Inputs:** the approved venues' complete, unflagged books retrieved in
  [E − 3h, E] (and ≥ `locked_at`), with E = kickoff − 24h. Nothing later, no
  outcomes, no model.
* **Rule:** for each selection s, best_s = the highest approved price and
  fair_s = the mean power-de-vigged probability across venues; score_s =
  best_s × fair_s − 1. Pick the argmax. Ties go home > draw > away, then by
  venue name. The primary analysis uses no threshold.
* Exactly one entry per fixture. Opposing or repeated entries are impossible.
* Tested: deterministic, order-independent, and unchanged when later quotes
  are added.

## 5. Three separate analyses

| analysis | what | role |
|---|---|---|
| `price_drift()` | for every selection, each venue's T−24h price vs its own last pre-kickoff price (mean log price ratio) | descriptive; no selection rule |
| `closing_line()` | selected entries vs the strong close: **closing EV = entry_price × fair_close − 1**; the weak version (vs the venues' own close) is reported as EXP-2026-11-B-weak | **primary endpoint** |
| `simulated_roi()` | the same entries settled on 90-minute results at the entry price, flat 1u | secondary only; never a lock or promotion criterion |

Exclusions are counted and reported:
- void or postponed fixtures;
- kickoff moved > 48h from the one known at entry;
- no entry snapshot (missing, partial or flagged books);
- no closing reference;
- **closing-reference mismatch** (external fair differs from the venues' close by > 10pp on any outcome; quarantined as a likely mis-join).

## 6. Statistical methodology

* Unit = fixture (one entry each). Mean closing EV, 95% percentile bootstrap
  (2,000 replicates, seed 0), clustered by fixture and by ISO week.
* Pass: lower bound > 0 under both clusterings with n ≥ 300. Below that the
  result is inconclusive. Upper bound < 0 = fail.
* No outcomes are needed for the primary endpoint.

## 7. Feasibility and sample size (draft settings kept: T−24h, T−60/T−15, min_n 300)

* **Eligible sample:** ~35 fixtures a week. Fixtures remaining after a lock on
  2026-12-07 / 12-21 / 2027-01-04: 900 / 829 / 769, giving an expected
  eligible n of about 765–828 / 704–762 / 653–707 (allowing 8–15% loss to
  coverage, flags, voids and missing closes).
* **Per-entry SD:** comparable statistics in EXP-2026-11-A had SD 0.086–0.099.
* **Minimum detectable effect** (two-sided α = 0.05, 80% power): 1.4–1.6% at
  n = 300; 0.9–1.1% at n = 700. min_n = 300 is a floor for interpretability,
  not the planned n. The design uses every eligible fixture to season end.
* **Expected direction:** DraftKings and FanDuel carry 5–7% margins against
  ~0.5% on the exchange close. Unless early prices move strongly, closing EV
  should be clearly *negative* (roughly −2% to −4%). A positive result would
  therefore be informative; a negative one is the expected default. The
  price-drift analysis can still be informative when the closing-line result
  is negative.

## 8. Data-quality acceptance criteria (evaluated on pilot data, before locking)

| criterion | threshold |
|---|---|
| logger liveness (`runs` heartbeat) | ≥ 99% of 5-min slots while the machine is on; downtime logged |
| regular-poll freshness | no league with upcoming fixtures > 3h without a successful poll on ≥ 98% of days |
| T−60 and T−15 capture | ≥ 95% of non-rescheduled fixtures |
| candidate-venue complete, unflagged book in the T−24h window | ≥ 90% of fixtures, per venue |
| source freshness | ≥ 95% of available quotes ≤ 10 min old (vendor timestamp) |
| invalid/degenerate pre-kickoff quotes | ≤ 2% |
| cross-venue discrepancy flags | ≤ 3% of sportsbook selections |
| external close matched (unique fixture, scores agree) | ≥ 95% of kicked-off fixtures; quarantine reviewed |
| source-to-screen spot checks (`expb spotcheck`) | ≥ 30 per candidate venue, ≥ 90% within 0.5pp implied, stake acceptance recorded |
| authentication failures | none in the last 7 days |

## 9. Decisions still required before locking

1. Venue verification for DraftKings and FanDuel: legal eligibility for you,
   account/market access, obtainability at the intended stake, and
   source-to-screen agreement. Without it, `approved_entry_venues` stays empty
   and no analysis can run.
2. Closing reference: accept the provisional `BFEC*` with stated limitations,
   or validate/replace it with Betfair Historical Data.
3. Confirm the draft settings (T−24h ± 3h, no threshold, 30-min weak close
   window, min_n 300) and the lock date (after the pilot).
