# Known limitations

## Evidence

* **No untouched holdout.** Every season with historical odds (2022–2025) was
  viewed during legacy development. EXP-2026-11-A is retrospective. Genuinely
  new evidence starts with frozen shadow predictions after 2026-11-20T00:00Z.
* **Season 2026 has almost no historical odds** (7 La Liga rows, no
  Pinnacle). It can only be scored against base rates.
* **Historical odds join is partial and non-random**: 63–90% of fixtures per
  league-season; 2025 Pinnacle closing ~50%. Unjoined fixtures (date or name
  mismatches) are excluded.
* **"Early" prices are not opening prices**: football-data's pre-closing
  columns are collected Friday/Tuesday; exact timestamps are unknown, hence the
  conservative kickoff − 4 day decision time.
* **Market-max prices are not executable evidence**: `Max*` columns mix books,
  times and limits. EARLY-MAX and SHOP-* results are upper bounds.
* **No liquidity, limits or account-restriction modelling.** Flat 1-unit
  stakes; Pinnacle prices assumed fillable at quoted odds.
* **Gate B threshold** (ECE ≤ 0.020) was set without accounting for
  finite-sample ECE bias; the market itself shows 0.014 pooled. The gate is
  reported as locked; the next plan should compare against the market's ECE
  on the same fixtures.

## Data

* No provider timestamps for results or xG; availability lags are assumptions
  (result +3h, xG +24h or strict ingestion time). xG revisions are undetectable.
* 2026 xG covers 0–9% of finals (pulled once in August).
* No lineups, injuries, suspensions, transfers, managerial changes, cup
  congestion or travel. None of these can be added without a timestamped
  source (FotMob rosters carry no history).
* Live odds log: 3-hourly polling (closing proxy usually 0–3h stale, here up
  to ~46h for the latest snapshot), September 2026 missing, three vendors only
  (one a prediction market), moneyline only.
* Provider gaps: 2022 seasons have 370 fixtures; Serie A 2023 has 379.

## Model

* The challenger reuses the legacy likelihood; the corrected engine removes
  look-ahead but adds no new information, so it was never expected to beat
  the market.
* Optimiser failure is detected (`model_ok`) and rejected by the risk engine,
  but there is no automatic retry or alternate solver.
* Probabilities are not recalibrated; Kelly is a diagnostic only.

## Operations

* The new paper/alert path is not connected to the scheduler; production
  continues to run legacy code (by design, pending authorization).
* Linting (`ruff`) and dependency audit (`pip-audit`) are configured in CI but
  were not executed locally (tools not installed on this machine; not
  installed globally to avoid altering the production interpreter).
* CI has not been run on GitHub: the branch was not pushed.
* Golden tolerances (1e-6) assume the locked numpy/scipy versions; another
  BLAS/platform may need a documented tolerance change (new version id).
