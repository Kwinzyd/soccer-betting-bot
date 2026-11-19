# EXP-2026-11-B — 14-day observational pilot: readiness report

Prepared 2026-11-19 on `research/quant-audit` (not pushed, not merged).
**Update 2026-11-19 22:00 UTC: task installed with authorization; pilot started at 2026-11-19T21:58:29Z
(ends 2026-12-03T21:58:29Z); store on the USB drive `D:\soccerlab-research\expb`.**
Original readiness text follows. The pilot collects odds only: no
predictions, no picks, no Telegram, no staking, no real-money execution.
All pilot records are excluded from the eventual locked analysis.

## 1. What the pilot does

* Scheduled task `soccerlab-expb-odds-logger` (not registered) runs
  `python -m soccerlab expb run` from the research worktree every 5 minutes.
* It polls BallDontLie `matches` and `odds` for EPL, La Liga, Serie A and
  Ligue 1. Regular odds polls run every 2h, with targeted T−60/T−15 snapshots
  and schedule refreshes every 3h.
* It writes only `var\expb\exp_b_odds.sqlite3` in the research worktree
  (append-only). It refuses to write inside the production checkout.
* Every venue is recorded as observational.

## 2. Go / no-go checklist

### A. Verified now (2026-11-19)

| # | item | evidence | status |
|---|---|---|---|
| A1 | test suite | 149 passed (isolated venv) | GO |
| A2 | lint / static audit | `ruff` clean; `soccerlab audit` passed | GO |
| A3 | frozen models untouched | `baseline verify` and `model verify soccer-dc-pit-v1` passed | GO |
| A4 | API authentication | probe 2026-11-19T21:34:51Z: HTTP 200 for `matches` and `odds` in all 4 leagues; 8 requests total; key never printed | GO |
| A5 | rate limit | `x-ratelimit-limit: 5`; remaining 4 → 3 per league pair; reset ~60s ahead. Windows appear per league/route (inferred, unconfirmed); logger keeps global 13s spacing | GO (conservative) |
| A6 | vendors returned | DraftKings, FanDuel, Polymarket (Ligue 1 sample: no Polymarket) | GO |
| A7 | 2026-27 football-data CSVs | 4 files downloaded to `D:\soccerlab-research\expb\fd\`; 100/119/110/90 rows = BallDontLie finals; `BFEC*` 100% present; no Pinnacle columns; no duplicates | GO |
| A8 | team aliases | all 6 promoted teams resolved from 2026-27 results (5–10 unique matches each, 0 conflicting votes); every 2026 team mapped; evidence in `registry/experiments/team_aliases.json` | GO |
| A9 | closing-reference join | 419/419 rows matched to a unique fixture with agreeing scores; 0 quarantined | GO |
| A10 | protocol guard | `expb evaluate` → REFUSED (exit 3); no approved venues | GO |
| A11 | installer | dry run prints the plan, registers nothing; `schtasks` confirms the task is absent | GO |
| A12 | production isolation | main checkout clean at `e1b55fe`; `balldontlie-odds-logger` **Disabled**; no production file written | GO |

### B. Required before activation (your decision)

| # | item | status |
|---|---|---|
| B1 | explicit authorization to run `install_expb_task.ps1 -Install` | **PENDING** |
| B2 | legacy logger stays disabled during the pilot (it shares the 5/min key limit) | **CONFIRM** |
| B3 | PC on and online across kickoff windows (Fri–Mon afternoons/evenings UTC, Tue–Thu evenings). A sleeping machine shows up as missed snapshots, not invented data | **CONFIRM** |
| B4 | disk: pilot store moved to USB `D:` (NTFS, 112 GB free); C: holds only code and `var\venv` | DONE |
| B5 | USB `D:` must stay plugged in. If it is missing, every run exits 4 and writes nothing (marker check); monitor shows liveness red | CONFIRM |

### C. Pilot exit criteria (evaluated after 14 days; GO to lock only if all pass)

These are the data-quality criteria in [EXP-2026-11-B_DRAFT.md §8](EXP-2026-11-B_DRAFT.md):
- liveness;
- poll freshness;
- T−60/T−15 capture ≥ 95%;
- complete books in the T−24h window ≥ 90%;
- quote freshness;
- invalid quotes ≤ 2%;
- discrepancy flags ≤ 3%;
- external-close match ≥ 95%;
- ≥ 30 spot checks per candidate venue at ≥ 90% agreement;
- no authentication failures in the last 7 days.

The pilot also does **not** settle venue legality or obtainability. Locking
additionally requires the decisions in §9 of the draft and in
[EXP-2026-11-B_CLOSING_REFERENCE.md §4](EXP-2026-11-B_CLOSING_REFERENCE.md).

## 3. Resource requirements

| resource | requirement |
|---|---|
| API | ~180 requests/day (≤ ~18 per invocation); well inside 5/min |
| runtime | ≤ 4 min of API work per 5-min invocation; task time limit 6 min; no overlapping instances |
| disk | ~2 MB/day (~30 MB for 14 days) in `var\expb\` |
| software | `var\venv` (Python 3.13, numpy 2.4.1, scipy 1.17.1; 261 MB already present); the production interpreter is untouched |
| credentials | existing BallDontLie key (env or `%LOCALAPPDATA%\soccer-betting-bot\bdl.key`); no Telegram credentials used |
| human time | ~5 min/day for `monitor`; spot checks (≥ 30 per venue over 14 days); a weekly CSV download and import |

## 4. Exact commands (PowerShell)

### Pre-flight (read-only)

```powershell
cd C:\Users\louis\api-football-data-research
git status --short                                   # expect no output
git log --oneline -1
.\var\venv\Scripts\python.exe -m pytest -q           # expect all passed
.\var\venv\Scripts\python.exe -m soccerlab audit
.\var\venv\Scripts\python.exe -m soccerlab expb probe C:\Users\louis\api-football-data\balldontlie_consolidated
schtasks /Query /TN balldontlie-odds-logger /V /FO LIST | findstr /C:"Scheduled Task State"   # expect Disabled
```

### Install (only after B1)

```powershell
powershell -ExecutionPolicy Bypass -File research_tasks\install_expb_task.ps1             # dry run: review output
powershell -ExecutionPolicy Bypass -File research_tasks\install_expb_task.ps1 -Install
schtasks /Query /TN soccerlab-expb-odds-logger /V /FO LIST
.\var\venv\Scripts\python.exe -m soccerlab expb run        # optional immediate first run (lock-protected)
.\var\venv\Scripts\python.exe -m soccerlab expb monitor
```

Record the pilot window in `registry/experiments/EXP-2026-11-B.json`
(`pilot.start` = first run's UTC time, `pilot.end` = start + 14 days) in a
commit on this branch.

### During the pilot

```powershell
.\var\venv\Scripts\python.exe -m soccerlab expb monitor          # daily; exit 1 = red
.\var\venv\Scripts\python.exe -m soccerlab expb quality          # coverage/quality summary only
# source-to-screen spot check (prices you see on the venue's own screen at that moment):
.\var\venv\Scripts\python.exe -m soccerlab expb spotcheck --fixture bdl:epl:<id> --venue draftkings --selection home --american -110 --at 2026-11-22T14:05:00Z --stake-accepted 10
.\var\venv\Scripts\python.exe -m soccerlab expb spotcheck-report
# weekly closing-reference refresh:
foreach ($c in "E0","SP1","I1","F1") { Invoke-WebRequest "https://www.football-data.co.uk/mmz4281/2627/$c.csv" -OutFile "D:\soccerlab-research\expb\fd\${c}_2627.csv" }
.\var\venv\Scripts\python.exe -m soccerlab expb aliases
.\var\venv\Scripts\python.exe -m soccerlab expb closing-import D:\soccerlab-research\expb\fd\E0_2627.csv epl
.\var\venv\Scripts\python.exe -m soccerlab expb closing-import D:\soccerlab-research\expb\fd\SP1_2627.csv laliga
.\var\venv\Scripts\python.exe -m soccerlab expb closing-import D:\soccerlab-research\expb\fd\I1_2627.csv seriea
.\var\venv\Scripts\python.exe -m soccerlab expb closing-import D:\soccerlab-research\expb\fd\F1_2627.csv ligue1
```

### Pause, stop, roll back

```powershell
Disable-ScheduledTask -TaskName soccerlab-expb-odds-logger      # pause immediately (data kept)
Enable-ScheduledTask  -TaskName soccerlab-expb-odds-logger      # resume
Copy-Item D:\soccerlab-research\expb\exp_b_odds.sqlite3 "D:\soccerlab-research\expb\backup_$(Get-Date -Format yyyyMMdd).sqlite3"   # while disabled
powershell -ExecutionPolicy Bypass -File research_tasks\install_expb_task.ps1 -Uninstall        # remove the task
```

Rollback touches only the research task. The legacy tasks, the main
checkout, `telegram_config.json` and `paper.sqlite3` are never involved. The
pilot database is kept as data-quality evidence; delete `var\expb\` only if
you want to discard it.

## 5. What remains uncertain

* Whether `BFEC*` is a back price before commission (strongly suggested by the
  data), and when it is captured. It is provisional until confirmed.
* Whether BallDontLie's rate-limit windows are per league or per key.
* Whether DraftKings/FanDuel prices via BallDontLie match the venues' own
  screens, and whether they are obtainable at the test stake (spot checks).
* Legal eligibility of any venue for you. This is not something code can verify.
* The expected primary result is negative (5–7% book margins vs an exchange
  close). The experiment is designed to measure it, not to avoid it.
