# System audit — soccer betting bot

Audit of commit `e1b55fe` (baseline `soccer-dc-legacy`), performed 2026-11-19 on
branch `research/quant-audit` in a separate git worktree. Production files on
`main` were read, never modified.

Severity scale: **Critical** = invalidates a core conclusion or diagnostic, or
can mislead decisions at scale; **High** = materially biases results or
production output; **Medium** = correctness/robustness gap with bounded
effect; **Low** = hygiene.

Evidence classes: **[code]** verified by inspection · **[test]** pinned by an
automated test · **[data]** verified on the repository's historical/production
data · **[unverified]** · **[blocked]**.

## Summary

The legacy conclusion survives correction: **the model has no edge over the
closing market.** Under point-in-time rules it loses to Pinnacle close by
+0.023 log-loss (95% CI +0.017 to +0.030; every league, every season), its
optimal blend weight against the market is 0, its picks lose 11.6% (CI −17.7%
to −5.5%) and the market moves away from them. The look-ahead defects were
real but their measured effect on the historical numbers is small. The most
consequential problems are in the *live* path: a forward CLV diagnostic that
is zero by construction, edges measured against each side's own best book,
opposing selections on the same fixture, no alert deduplication, and silent
authentication failures.

## Findings

### Critical

**C1 — Forward CLV diagnostic is identically zero.**
`odds_logger/settle.py:report_clv` averages `close.fair[i] − entry.fair[i]`
over all three 1X2 outcomes. De-vigged probabilities sum to 1 at both times,
so the mean is 0 for any market movement. The one forward diagnostic the
project relies on ("CLV converges far faster") cannot detect anything.
*Evidence:* [test] `test_legacy_report_clv_is_identically_zero`,
`test_legacy_settle_report_clv_prints_zero` (a large move to the home side
prints `mean drift = -0.000pp`).
*Remediation:* selection-specific CLV (`soccerlab/clv.py`): entry/close odds
ratio, EV at closing fair, selection probability move, % beating close,
fixture-clustered CIs, closing-proxy age disclosed. *Verify:*
`test_selection_clv_signs`, `test_clv_summary_ci_and_clustering`.

**C2 — Promoted-team prior uses future seasons.**
`model/dcmodel.py:estimate_promoted_prior` fits every promoted cohort in the
data (through 2026) and the single result is reused by `backtest.py`,
`validate.py`, `evaluate.py`, `blend.py` and `strategy.py` for predictions in
2022–2025. *Evidence:* [test] `test_legacy_defect_promoted_prior_uses_future_cohorts`.
*Measured impact:* [data] legacy vs its point-in-time twin on 2,940 fixtures:
Δ log-loss −0.0005 (CI −0.0013 to +0.0004) — not material.
*Remediation:* `soccerlab/dcpit.py:promoted_prior` uses only cohorts from
seasons before the target season and results available at the season's first
cutoff (prior = 0 when no earlier cohort exists). *Verify:*
`test_pit_future_promoted_cohort_cannot_alter_prior`.

### High

**H1 — Live edge is measured against each side's own best book.**
`model/predict.py:main` picks the book with the best price for each side, then
de-vigs *that book* and uses its probability for that side. The best-priced
book is the one implying the lowest probability, so `model − fair` is biased
upward, and the three "fair" values come from different books and do not sum
to 1. The prediction market (Polymarket) dominates. *Evidence:* [data] a
preview replay of the 2026-11-19 19:17 UTC production poll flags **43
selections on 29 fixtures; 14 fixtures get two opposing selections** (e.g.
Parma v Roma: Draw *and* Home); 31/43 are Polymarket prices. *Remediation:*
one argmax-EV selection per fixture, explicit fair reference, quote validity
and exposure checks (`soccerlab/ledger.py`, `soccerlab/live.py`). Not wired
into production — needs authorization (see "Production isolation").

**H2 — Telegram alerts are not deduplicated and lack required fields.**
`predict.py` sends every flagged selection on every 3-hourly cycle; nothing
records what was sent. Alerts omit model version, model probability,
implied vs de-vigged probability, prediction timestamp and data freshness, and
display "half-Kelly %" for a model that fails calibration and market tests.
[code] *Remediation:* `soccerlab/alerts.py` (dedup ledger, namespaces, full
field set, shadow can never alert). *Verify:* `test_selection_alert_dedup_and_fields`,
`test_shadow_never_alerts`.

**H3 — Authentication failures are silent.** [data] `odds_logger/runs.log`:
17 HTTP 401 responses in the last 24h (2026-11-18 22:44 → 2026-11-19 19:06
UTC), each logged as "N fixtures, 0 quotes"; the scheduled task still reports
`Last Result: 0`. The legacy paper ledger is empty (0 picks) because the card
was empty, not because of selectivity. *Remediation:* `soccerlab/provider.py`
raises `AuthError` on 401/403; `python -m soccerlab health` reports RED.
*Verify:* `test_provider_explicit_failures`.

**H4 — Line-shopping "ROI" counts repeated observations as bets.**
`settle.py:report_shopping` groups by (fixture, observation hour), so a
fixture polled every 3h is bet once per poll, and several outcomes per
fixture can qualify. [test] `test_legacy_settle_report_shopping_counts_repeat_observations`
(one opportunity, three polls → `bets=3`). *Remediation:* one entry per
fixture/market enforced by the store's unique key and the risk engine.

**H5 — Benchmarks and tuning use the evaluation seasons.**
`backtest.baselines` trains on every non-test season, including later ones
([test] `test_legacy_defect_baselines_train_on_future_seasons`). xi/reg were
tuned on 2024–2025 (`backtest.py tune`) and the xG weight on 2025
(`blend.py`); README then reports 2023–2025 as out-of-sample. [code]
*Remediation:* PIT base rates from data before each season; EXP-2026-11-A
re-uses legacy hyperparameters without retuning and labels all seasons as
previously viewed.

**H6 — Live xG input is stale and unchecked.** [data] 2026 team-match stats
were pulled once (2026-08-22): xG covers 6/100 EPL, 11/119 La Liga, 0/110
Serie A and 1/90 Ligue 1 finals. The live model still blends xG at 30%, so
that component reflects last season. Nothing detects it. *Remediation:*
`health` reports it; the challenger is goals-only.

**H7 — Season-boundary promoted flag.** `dcmodel.fit_asof` sets
`asof_season = max(season in training data)`; at each season opener that is
the previous season, so last season's promoted sides keep the promoted prior
and the new season's are treated the same. [test]
`test_legacy_defect_season_opener_keeps_last_seasons_promoted_flag`.
*Remediation:* season taken from the fixture (`test_pit_season_opener_promoted_flag`).

**H8 — Production picks are not auditable.** `model/paper.py` stores league,
match, side, book, odds, probability, kickoff — no model version, decision
timestamp, input snapshot, quote timestamp or no-bet log; void/postponed
handling is absent (open forever). [code] *Remediation:* `soccerlab/store.py`
predictions/decisions/paper_entries/settlements tables (append-only triggers,
correction history) and `soccerlab/settlement.py` void rules.

### Medium

| id | finding | evidence | remediation |
|---|---|---|---|
| M1 | Team universe includes future entrants (`backtest.prep` adds 2026 teams to every historical fit; they sit on the prior and shift centring) | [test] `test_pit_future_league_entrant_is_invisible` | PIT universe = training teams ∪ target-season fixture list |
| M2 | Odds log has no September 2026 data (all four files empty) | [data] `health` | gap disclosed; no backfill exists |
| M3 | Odds log contains degenerate in-play quotes (−100000/+35000) from an earlier logger version; legacy settlement drops only quotes later than the *current* kickoff via string compare | [data] 23/811 quotes fail validation | `odds.validate_1x2`, `clv.quotes_asof` (aware datetimes, strictly pre-kickoff) |
| M4 | Logger stores only changed quotes; suspensions/removals are invisible; one `observed_at` per run | [code] | `quotes.status` ∈ available/suspended/removed/invalid |
| M5 | football-data `PSH/PSD/PSA` are pre-closing snapshots (Fri/Tue), labelled "open" | [code] | reported as "early", decision time kickoff−4d |
| M6 | Optimiser convergence (`res.success`) never checked by callers | [code] | `model_ok` → risk engine rejects |
| M7 | `settle.py`/`log_odds.py` read the API key at import; even `--selftest` needs a secret | [code] | CI sets a dummy key; new client takes the key explicitly |
| M8 | Historical join covers 63–90% of fixtures per league-season, not at random; 2025 Pinnacle close ~50% | [data] `data completeness` | disclosed; evaluation restricted to joined fixtures |
| M9 | Legacy strategy ROI allowed several selections per fixture and reported no intervals except one t-stat | [code] | one selection per fixture, fixture- and week-clustered bootstrap |
| M10 | Pre-registered Gate B ECE threshold (0.020) is close to finite-sample ECE noise at n≈1,450/season (this audit's own plan) | [data] market ECE 0.014 pooled | kept as locked for EXP-2026-11-A; next plan should benchmark ECE against the market's |

### Low

| id | finding | evidence |
|---|---|---|
| L1 | Score matrix truncated at 11 goals/side then renormalised: market distortion < 1e-4 for λ ≤ 3.5, < 1e-3 at 4.5 | [test] `test_score_matrix_truncation_and_dc_coherence` |
| L2 | log-λ is clipped to [−6, 3] in the objective but the gradient ignores the clip (only matters at the clip) | [code] |
| L3 | `generate_live_card` accepts any \|American\| ≥ 100 with no upper bound or book-coherence check | [code] |
| L4 | `pull_xg.py` deletes its own scheduled task on completion | [code] |
| L5 | Provider gaps: 2022 seasons have 370 fixtures; Serie A 2023 has 379 | [data] `data verify` |
| L6 | README claims "nothing leaks" | [code] |

### Verified correct (no defect)

* Analytic gradient of `_obj_grad` matches central differences (max relative
  error 6.8e-10) [test] `test_legacy_gradient_matches_numerical`.
* American→decimal/probability conversions, power de-vig on normal books and
  `paper.py` P&L arithmetic agree with independent implementations [test].
* Dixon-Coles cells stay non-negative after clipping; 1X2 sums to 1 [test].
* Legacy coefficients and golden predictions reproduce exactly from the
  baseline commit's data [test] `baseline verify --golden`.

## Production isolation

Nothing in production was changed: no edits on `main`, no scheduled task
touched, no Telegram message sent, no production database written (the
legacy paper ledger was opened read-only). All new code lives in
`soccerlab/`; research state goes to `var/` (self-gitignored) and the store
refuses to open inside any checkout carrying the live logger's state file.

Fixing H1–H3 *in production* means changing what the live bot sends and
records. That is a production change and is left for explicit authorization;
the replacement path (`soccerlab paper`) refuses to run until a model is
promoted, and none is.
