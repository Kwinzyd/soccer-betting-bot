# Final report and handoff — quantitative audit, 2026-11-19

Branch `research/quant-audit` (git worktree `..\api-football-data-research`),
Commits on top of `e1b55fe` only on this branch. Not pushed, not merged. Production untouched.

## Answer

**The system has no demonstrated forecasting or pricing advantage.** After
removing look-ahead, the Dixon-Coles model is strictly worse than the
Pinnacle closing line, adds nothing in a blend, and its picks lose money with
negative closing-line value. Prior negative findings are reproduced and now
carry confidence intervals. No model is eligible for production paper picks.

| question | result (EXP-2026-11-A, seasons 2023-25, n=2,940 fixtures) |
|---|---|
| Model vs Pinnacle close, log-loss | 0.9970 vs 0.9736; Δ +0.0234, 95% CI [+0.0167, +0.0298] (fixture), [+0.0165, +0.0303] (week blocks) |
| Worse than market in every league? | yes: EPL +0.023, La Liga +0.014, Serie A +0.029, Ligue 1 +0.028 (all CIs > 0) |
| Worse in every season? | yes: 2023 +0.027, 2024 +0.024, 2025 +0.018 (all CIs > 0) |
| Blend weight on model (fit 2023-24) | 0.00 → no incremental information |
| vs naive base rates | much better (1.0764 → 0.9970) — it is a sound description of team strength |
| Pre-registered policy EARLY-PIN@0.02 | 2,484 bets, ROI −11.6% [−17.7%, −5.5%] |
| CLV of those picks vs Pinnacle close | odds ratio −1.17% [−1.54, −0.81]; EV at closing fair −5.2% [−5.6, −4.8]; market moves away (−0.15pp [−0.26, −0.06]); 42.7% beat close |
| Effect of fixing look-ahead | legacy vs PIT twin Δ −0.0005 [−0.0013, +0.0004]: real defect, immaterial effect |
| Legacy "only positive strategy" (line shopping) | does not reproduce with one entry per fixture: ROI −1.6% [−7.7%, +4.4%] |
| Gates | A pass · B fail · **C fail** · D fail · E fail · F not started · G not evaluated |

Full report: [reports/EXP-2026-11-A.md](../reports/EXP-2026-11-A.md); machine-readable:
`reports/EXP-2026-11-A.json`; every prediction: `reports/EXP-2026-11-A-predictions.csv`.
A second full run from committed code reproduced the JSON exactly (except
runtime) and the predictions CSV byte-for-byte.

## Time-sensitive: what production will do next

The legacy bot is unchanged and runs every 3h (next at 17:05 local / 22:05 UTC
at the time of writing). Most runs in the last 24h hit HTTP 401, so it posted
nothing; the 19:17 UTC poll succeeded. On the next successful cycle, legacy
logic will record and alert **43 selections on 29 fixtures, including 14
fixtures with two opposing selections, 31 of them at Polymarket prices**, and
re-alert qualifying ones every cycle. Changing that is a production change I
did not make. The least invasive stop is `"enabled": false` in
`telegram_config.json` (RUNBOOK §4).

## Deliverables

| # | deliverable | where |
|---|---|---|
| 1 | severity-ranked source audit | [SYSTEM_AUDIT.md](SYSTEM_AUDIT.md) |
| 2 | before/after architecture and data flow | [ARCHITECTURE.md](ARCHITECTURE.md), [DATA_LINEAGE.md](DATA_LINEAGE.md) |
| 3 | immutable baseline | `registry/soccer-dc-legacy/manifest.json` (sha256 `caacd19f…`), `python -m soccerlab baseline verify --golden` |
| 4 | corrected historical evaluation framework | `soccerlab/pit.py`, `dcpit.py`, `walkforward.py`, `evaluation.py` |
| 5 | reproducible tests | `tests/` — 105 tests, offline (sockets blocked) |
| 6 | market benchmark and model comparison | `reports/EXP-2026-11-A.md` |
| 7 | per-selection CLV and ROI | `soccerlab/clv.py`; report "Betting policies" |
| 8 | model registry with gates | `registry/index.json`, `registry/soccer-dc-pit-v1/manifest.json`, [EXPERIMENT_PLAN.md](EXPERIMENT_PLAN.md) |
| 9 | separate shadow / production-paper paths | `soccerlab/live.py`, `ledger.py`, `alerts.py` (shadow never alerts; paper requires promotion) |
| 10 | runbook and rollback | [RUNBOOK.md](RUNBOOK.md) |
| 11 | remaining limitations | [KNOWN_LIMITATIONS.md](KNOWN_LIMITATIONS.md) |
| 12 | next experiment | below |

Also: [PRODUCTION_INVENTORY.md](PRODUCTION_INVENTORY.md), CI workflow
`.github/workflows/ci.yml`, `requirements-lock.txt`.

## Commands run (final pass, research worktree) and exit status

| command | exit | result |
|---|---|---|
| `python -m pytest -q` | 0 | 105 passed |
| `python -m soccerlab audit` | 0 | 378 tracked files, no secrets / unexpected hosts / execution primitives |
| `python -m soccerlab data verify` | 0 | passed (warns: provider fixture gaps) |
| `python -m soccerlab baseline verify --golden` | 0 | 337/337 files match `e1b55fe`; coefficients and golden predictions reproduce |
| `python -m soccerlab model verify soccer-dc-pit-v1 --golden` | 0 | 43/43 files match `36001ae`; golden ok |
| `python -m soccerlab model list` | 0 | promoted: none |
| `python odds_logger/settle.py --selftest` (legacy) | 0 | passed |
| `python -m soccerlab backtest` (twice) | 0 | identical results |
| `python -m soccerlab shadow --poll var/snap/latest_poll.json --replay --schedule-dir var/snap` | 0 | 36 predictions, 20 entries, 16 rejected by daily cap, 0 alerts |
| `python -m soccerlab paper ...` | 3 | refused: no promoted model (intended) |
| `python -m soccerlab settle --schedule-dir var/snap` | 0 | 20 open; CLV vs 46h-old proxy, inconclusive (intended labelling) |
| `python -m soccerlab health` (production, read-only) | 1 | RED: 17 auth failures in 24h (a real finding) |
| `ruff check soccerlab tests` (isolated venv) | 0 | clean after fixing 16 findings (zip length checks in golden verification, SQL-filter allowlist, unused vars) |
| `pip_audit -r requirements-lock.txt` (isolated venv) | 0 | no known vulnerabilities |
| full suite + both `--golden` verifications re-run in the venv | 0 | 105 passed; both manifests verify |
| GitHub CI | not run | branch not pushed |

## Evidence classification

* **Verified by tests:** C1, C2, H4, H5 (baselines), H7, M1; PIT invariance
  (future results, future cohorts, future entrants, post-cutoff xG revisions,
  post-kickoff quotes, future quotes); deterministic reruns; odds/de-vig/EV/
  commission/settlement/void arithmetic; duplicate and opposing-selection
  rejection; exposure caps; kill switch; alert dedup; provider retry/auth/
  pagination failures; manifest tamper detection; atomic writes; gradient check.
* **Verified by historical/production data:** model-vs-market gap and its CIs;
  leakage magnitude; policy ROI and CLV; legacy card behaviour on the latest
  poll (H1); 401 run history (H3); xG staleness (H6); odds-log gaps (M2, M3);
  join coverage (M8).
* **Verified by code inspection only:** H2, H5 (tuning on evaluation seasons),
  H8, M4–M7, L2–L4, L6.
* **Not verified:** whether football-data closing prices were executable at
  size; exact timing of football-data pre-closing snapshots; whether the
  provider revises xG/results after first publication.
* **Blocked:** prospective evidence (Gate F) — needs weeks of frozen shadow
  operation; true closing lines for the live log — the logger polls every 3h;
  lineup/injury features — no timestamped source exists in the repo.

## Recommended next experiment

Stop iterating on the standalone ratings model: it is dominated by the closing
line on every slice, and the blend weight is zero. The one signal with
tight, positive evidence is the *no-model* comparison of the best early price
against Pinnacle's closing fair price: EV +1.6% [+1.3%, +2.0%], 76% of
selections beat the close (SHOP-EARLY@0.0, n=2,033). Its ROI is still
indistinguishable from zero and the market-max column is not executable, so
it is a lead, not an edge.

**EXP-B (pre-register before running): executable line shopping.**
1. Use football-data's *named-book* early columns (B365, BW, WH, 1XB, PS and
   Betfair exchange with commission) instead of `Max*`; restrict to books the
   user can actually use; decide at kickoff − 4d against Pinnacle *early* fair.
2. Primary metric: selection CLV (EV at Pinnacle closing fair) with fixture-
   and week-clustered CIs; ROI secondary. One entry per fixture.
3. In parallel, make forward CLV measurable: add polls at kickoff − 60 and
   − 15 minutes to the logger (a production change: needs authorization), so
   the closing proxy is minutes, not hours, old.
4. Only if CLV is positive both historically (named books) and prospectively
   (≥ 300 shadow entries) consider a paper strategy, expecting account limits
   to be the binding constraint.
