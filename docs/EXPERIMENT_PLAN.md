# Experiment plan — pre-registration EXP-2026-11-A

**Locked:** 2026-11-19 (UTC), committed to branch `research/quant-audit` *before*
any point-in-time (PIT) corrected evaluation was run. Changing any threshold
below requires a new experiment id; results under this id are reported
against these numbers whether they pass or fail.

## 0. What is already known (disclosed prior evidence)

`model/README.md` (legacy) reports that the legacy model loses to the
Pinnacle closing line on log-loss (gap +0.017 to +0.025), loses money at
every edge threshold, and shows slightly negative CLV. Those numbers came
from an evaluation with known look-ahead defects (see
[SYSTEM_AUDIT.md](SYSTEM_AUDIT.md)). **Prior expectation for this experiment:
Gate C fails.** The experiment exists to measure that properly, not to
reverse it.

Seasons 2022–2025 and 2026-to-date have all been looked at during legacy
development (xi/reg were tuned on 2024–2025, the xG weight on 2025). They are
therefore **retrospective, previously viewed data**. Nothing below is
described as an untouched holdout. Genuinely new evidence starts at the lock
date (§6).

## 1. Models under test

| id | role | definition |
|---|---|---|
| `soccer-dc-legacy` | legacy baseline | unchanged legacy code, run through the read-only adapter on baseline-commit data |
| `soccer-dc-pit-v1` | challenger (primary) | legacy likelihood/optimiser, PIT data rules (§2), goals only, **legacy hyperparameters unchanged** (xi=0.0015, reg=2.0, prior_reg=1.0), weekly refit |
| `soccer-dc-pit-xg` | ablation only | as pit-v1 plus xG blend alpha=0.3 with xG assumed available 24h after kickoff (`provider_lag`). Under the strict `ingestion` rule xG did not exist before 2026-08-22, so this ablation is optimistic by construction. Not eligible for promotion under this plan. |
| market-close | mandatory baseline | Pinnacle closing 1X2, power de-vig |
| market-early | mandatory baseline | Pinnacle pre-closing snapshot (football-data `PSH/PSD/PSA`, collected Fri/Tue), power de-vig |
| base-rates | naive baseline | league H/D/A frequencies from final matches before the season's first cutoff |
| uniform | naive baseline | (1/3, 1/3, 1/3) |

No hyperparameter is tuned in this experiment.

## 2. PIT rules

* Result available at kickoff + 3h; xG per model definition above.
* Decision times: **close** = kickoff − 1h; **early** = kickoff − 4 days (the
  football-data pre-closing snapshot is taken at most ~3.5 days before
  kickoff, so the model's information predates the early price).
* A fixture uses the latest weekly refit whose cutoff is <= its decision time.
* League membership = the season's fixture list; promoted-team prior from
  earlier cohorts only (§ docs/DATA_LINEAGE.md).

## 3. Evaluation population

* **Primary window:** seasons 2023, 2024, 2025; EPL, La Liga, Serie A, Ligue 1;
  final matches that have a Pinnacle closing price in `model/odds_joined.json`
  and a prediction from every compared model.
* Season 2022 is warm-up only. Season 2026 has no usable historical odds and is
  reported for forecast metrics vs base rates only.
* Join coverage is ~80% and the unjoined fixtures are not random (date or name
  mismatches); this is a disclosed limitation, not corrected.

## 4. Metrics (definitions fixed)

* Multiclass log-loss (natural log) and multiclass Brier (sum over 3 classes).
* Paired difference per fixture: Δ = LL(model) − LL(market-close).
  95% percentile bootstrap, 2000 replicates, seed 0, clustered by fixture;
  robustness: clustered by ISO week (time blocks).
* Class-wise ECE, 10 equal-width bins (methodology in `soccerlab/metrics.py`).
* Calibration tables per outcome in the legacy bands.
* Blend test: Q_w = (1−w)·market + w·model, w on a 0.05 grid, w* chosen on
  2023–2024, applied unchanged to 2025.
* Betting: one selection per fixture maximum (argmax EV), flat 1 unit, decimal
  price, ROI = profit / turnover, bootstrap clustered by fixture, max drawdown
  in chronological order.
* CLV (early policies): odds ratio entry/close − 1; closing-based EV proxy
  entry_price × fair_close(selection) − 1; selection fair-probability move
  close − early; % of selections whose entry price beats the close.

## 5. Gates and numerical criteria (primary model `soccer-dc-pit-v1`)

| gate | criterion | sample floor |
|---|---|---|
| A correctness | full pytest suite passes; `baseline verify --golden` passes; leakage tests pass | — |
| B calibration | ECE ≤ 0.020 in each primary season; no band with n ≥ 200 has \|gap\| > 0.05 | 300 fixtures/season |
| C incremental value | upper 95% CI of mean Δ (close decision vs market-close, primary window) **< 0**, under both fixture and week clustering; AND the 2025 blend at w* has upper 95% CI of Δ_blend < 0 | 1000 fixtures |
| D economic | policy **EARLY-PIN** (bet the argmax-EV selection at the Pinnacle early price when EV > 0.02): ROI lower 95% CI > 0 AND closing-based EV proxy lower 95% CI > 0 | 1000 bets |
| E robustness | Δ point estimate < 0 in ≥ 3 of 4 leagues and in every primary season; if D passes, ROI > 0 at both EV thresholds 0.02 and 0.05, in ≥ 3 of 4 leagues, and with the 10 most profitable bets removed | — |
| F forward | from 2026-11-20T00:00Z: ≥ 500 frozen shadow predictions with a logged pre-kickoff closing proxy (last quote ≤ 3h15m before kickoff, gap disclosed), upper 95% CI of Δ vs that proxy < 0; ≥ 300 paper selections with CLV-proxy lower 95% CI > 0 | as stated |
| G operational | 14 consecutive days of green `health`, zero duplicate alerts, settlement lag ≤ 48h | — |

Rules:

* Anything below its sample floor is **inconclusive**, never pass.
* A model failing C is ineligible for production paper picks regardless of
  calibration or ROI.
* Gates are sequential: D–G are only evaluated for promotion if C passes;
  they are still computed and reported.
* Secondary policies (reported, not gating): CLOSE-PIN (argmax EV at Pinnacle
  close, EV > 0.02/0.05), EARLY-MAX (early, market-max price — optimistic,
  not necessarily executable), and the no-model line-shopping replications
  SHOP-EARLY / SHOP-CLOSE (max price vs Pinnacle fair, EV > 0 / 0.02).

## 6. Forward (prospective) protocol

* Frozen spec: `registry/soccer-dc-pit-v1/manifest.json`; any change = new id.
* Shadow predictions are written to the research store only (namespace
  `shadow`), never alerted, never in the production paper ledger.
* Closing proxy: last BallDontLie quote before kickoff per vendor; the gap
  to kickoff is stored and reported. The logger currently runs every 3h, so
  most proxies will be 0–3h stale; this is disclosed, not hidden.
