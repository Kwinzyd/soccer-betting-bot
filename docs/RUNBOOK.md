# Operational runbook (Windows)

All commands run from the repository root in PowerShell. Everything here is
offline or read-only against production unless stated. Nothing here places a
wager; there is no code path that can.

## 1. Setup

```powershell
git worktree list                                  # production = main checkout; research = separate worktree
python -m pip install -r requirements-lock.txt     # numpy/scipy exact versions
python -m pip install -r requirements-dev.txt      # + pytest, ruff, pip-audit (optional; use a venv)
```

## 2. Commands

| purpose | command | exit codes |
|---|---|---|
| audit (secrets, hosts, execution primitives) | `python -m soccerlab audit` | 0 pass, 1 fail |
| data verification | `python -m soccerlab data verify` | 0 / 1 |
| data completeness | `python -m soccerlab data completeness` | 0 |
| offline backtest (EXP-2026-11-A) | `python -m soccerlab backtest` (`--quick` = EPL only) | 0 |
| tests | `python -m pytest -q` | 0 / 1 |
| verify legacy baseline | `python -m soccerlab baseline verify --golden` | 0 / 1 |
| list models | `python -m soccerlab model list` | 0 |
| freeze model (new id only; refuses overwrite) | `python -m soccerlab model freeze <id>` | 0 / error |
| verify model | `python -m soccerlab model verify soccer-dc-pit-v1 --golden` | 0 / 1 |
| shadow predictions | `python -m soccerlab shadow --poll <snapshot> [--replay] [--schedule-dir <dir>]` | 0; 3 refused; 4 stale input |
| paper predictions | `python -m soccerlab paper --poll <snapshot> --model <promoted id>` | 3 until a model is promoted |
| settle paper/shadow | `python -m soccerlab settle [--schedule-dir <dir>]` | 0 |
| system health | `$env:SOCCERLAB_PROD_ROOT="C:\Users\louis\api-football-data"; python -m soccerlab health` | 0 green/amber, 1 red |

Snapshot production inputs without touching them:

```powershell
$snap = "var\snap"; New-Item -ItemType Directory -Force $snap | Out-Null
Copy-Item C:\Users\louis\api-football-data\odds_logger\latest_poll.json $snap
Copy-Item C:\Users\louis\api-football-data\balldontlie_consolidated\*_2026_matches.json $snap
python -m soccerlab shadow --poll $snap\latest_poll.json --schedule-dir $snap
```

A fresh (non-`--replay`) shadow run refuses polls older than 30 minutes.

## 3. Optional: scheduling shadow mode (not installed)

Shadow mode is not scheduled. To start forward evidence collection
(Gate F), register a *separate* task that runs from the research worktree and
never touches the legacy tasks:

```powershell
$wt = "C:\Users\louis\api-football-data-research"
$act = New-ScheduledTaskAction -Execute python -WorkingDirectory $wt `
  -Argument "-u -m soccerlab shadow --poll C:\Users\louis\api-football-data\odds_logger\latest_poll.json --schedule-dir C:\Users\louis\api-football-data\balldontlie_consolidated"
$trg = New-ScheduledTaskTrigger -Once -At (Get-Date).Date.AddMinutes(20) -RepetitionInterval (New-TimeSpan -Hours 3)
Register-ScheduledTask -TaskName soccerlab-shadow -Action $act -Trigger $trg -Description "soccerlab shadow (no alerts)"
```

It only *reads* production files (the store refuses to write inside a
checkout containing `odds_logger\seen.json`) and writes `var\research.sqlite3`
in the worktree. Offset it ~15 minutes after the legacy logger so the poll is
fresh. Remove with `Unregister-ScheduledTask soccerlab-shadow`.

## 4. Changing production (requires explicit authorization)

The highest-value production fixes (SYSTEM_AUDIT H1–H3) change what the live
bot records and sends. Suggested order, each step reversible:

1. Back up: `Copy-Item odds_logger\paper.sqlite3 odds_logger\paper.sqlite3.bak-<date>`.
2. Stop alerts first without touching scheduling: set `"enabled": false` in
   `telegram_config.json` (legacy `send_message` honours it).
3. Fail loudly on 401: run `python -m soccerlab health` after each cycle (or
   as its own task) and route RED to an ops alert.
4. Only after a model passes every gate: replace `predict.py`'s paper/alert
   tail with `soccerlab paper` + `alerts.Alerter` (one selection per fixture,
   deduplicated).

### Rollback

* Research branch: `git worktree remove ..\api-football-data-research` and
  `git branch -D research/quant-audit` — production is unaffected.
* Production change: `git checkout main -- <file>` (or `git revert`), restore
  `paper.sqlite3.bak-<date>`, set `telegram_config.json` back, and
  `schtasks /Run /TN balldontlie-odds-logger` to confirm a clean cycle.
* Research store: `Store.backup()` before migrations; the store is
  append-only, so rollback means restoring the backup file.

## 5. Kill switch

Create `var\PAPER_KILL_SWITCH` (any content) or set
`$env:SOCCERLAB_PAPER_KILL="1"`; every subsequent candidate is logged as a
no-bet with reason "kill switch engaged". Delete the file to resume.

## 6. Model promotion procedure

1. Freeze a new model id (`model freeze`); never edit an existing manifest.
2. Write a new experiment plan with numerical gates *before* evaluation and
   commit it.
3. Run the evaluation; publish the report regardless of outcome.
4. If Gates A–E pass: run shadow ≥ the Gate F sample; evaluate F and G.
5. Only a human edits `registry/index.json` to set `"status": "promoted"`
   and add the id to `"promoted"`, in a reviewed commit.

## 7. EXP-2026-11-B research odds logger (logging only; not activated)

| purpose | command |
|---|---|
| one invocation (what the task runs) | `python -m soccerlab expb run` |
| monitor (exit 1 on red) | `python -m soccerlab expb monitor` |
| data-quality summary (allowed pre-lock) | `python -m soccerlab expb quality` |
| rebuild team aliases (after downloading 2026-27 CSVs into `var\expb\fd\`) | `python -m soccerlab expb aliases` |
| import Betfair Exchange closes | `python -m soccerlab expb closing-import var\expb\fd\E0_2627.csv epl` |
| record an obtainability spot check | `python -m soccerlab expb spotcheck --fixture bdl:epl:3818288 --venue draftkings --selection home --american -105 --at 2026-11-21T14:00:00Z --stake-accepted 10` |
| primary analysis (refuses until locked) | `python -m soccerlab expb evaluate` (exit 3 while draft) |
| minimal auth / rate-limit probe (8 requests max) | `python -m soccerlab expb probe <schedule dir>` |

Pilot preparation, the go/no-go checklist and the exact install/rollback commands are in
[EXP-2026-11-B_PILOT_READINESS.md](EXP-2026-11-B_PILOT_READINESS.md).

Install (only after authorization), check, remove:

```powershell
powershell -ExecutionPolicy Bypass -File research_tasks\install_expb_task.ps1            # dry run
powershell -ExecutionPolicy Bypass -File research_tasks\install_expb_task.ps1 -Install   # registers soccerlab-expb-odds-logger
schtasks /Query /TN soccerlab-expb-odds-logger
powershell -ExecutionPolicy Bypass -File research_tasks\install_expb_task.ps1 -Uninstall
```

The task runs from the research worktree (using `var\venv` if present),
every 5 minutes, at most 6 minutes per run, with no overlapping instances. It
writes only `var\expb\exp_b_odds.sqlite3`. A killed run leaves a lock and an
open poll. The next run breaks the lock after 20 minutes and marks the poll
`interrupted` after 30. Rollback = uninstall. The database is append-only;
back it up by copying the file while no run is active.
