# Architecture

## Before (legacy, still running on `main`)

```mermaid
flowchart LR
  TS[Task: balldontlie-odds-logger<br/>every 3h] --> BOT[bot.py]
  BOT --> LOG[odds_logger/log_odds.py<br/>--refresh-schedules]
  LOG -->|overwrites| SCHED[(balldontlie_consolidated/<br/>*_2026_matches.json)]
  LOG -->|append changed quotes| JSONL[(odds_logger/log/*.jsonl)]
  LOG -->|overwrite| POLL[(latest_poll.json)]
  BOT --> PRED[model/predict.py]
  POLL --> CARD[generate_live_card.py]
  SCHED --> CARD
  SCHED --> FIT[dcmodel.fit_asof<br/>refit from scratch each run]
  STATS[(team_match_stats 2025/2026<br/>pulled once, Aug 2026)] --> FIT
  CARD --> PRED
  FIT --> PRED
  PRED -->|INSERT OR IGNORE| PAPER[(odds_logger/paper.sqlite3)]
  PRED -->|every cycle, no dedup| TG[telegram_alerts.py]
  TS2[Task: balldontlie-odds-settle daily] --> SETTLE[settle.py --refresh]
  SETTLE --> RC[(results_cache.json, settled.jsonl)]
  TS3[Task: balldontlie-roster-sync daily] --> ROST[fetch_rosters.py] --> RJ[(rosters/, active_rosters.json)]
```

Properties: no model versioning; predictions are not stored, only flagged
picks; historical evaluation scripts (`backtest/validate/evaluate/strategy/
blend`) share one leaked promoted prior; the CLV report is zero by
construction.

## After (this branch; production untouched)

```mermaid
flowchart TB
  subgraph prod[Production checkout on main - unchanged, read-only to soccerlab]
    PJ[(odds log, latest_poll, schedules)]
  end
  PJ -- copy snapshot --> SNAP[(var/snap/*)]

  subgraph lab[soccerlab]
    PROV[provider.py<br/>auth/rate-limit/pagination failures explicit] --> RAW
    SNAP --> PITD[pit.py<br/>availability timestamps]
    RAW[(store: raw_responses)] --> Q[(store: quotes<br/>available/suspended/removed/invalid)]
    PITD --> DC[dcpit.py<br/>PIT Dixon-Coles]
    DC --> WF[walkforward.py<br/>decision-time cutoffs]
    WF --> EVAL[evaluation.py<br/>EXP-2026-11-A]
    EVAL --> REP[(reports/*.json, *.md, predictions.csv)]
    DC --> LIVE[live.py shadow/paper]
    Q --> LIVE
    REG[registry/index.json<br/>+ frozen manifests] --> LIVE
    LIVE --> RISK[ledger.RiskEngine<br/>dup/exposure/staleness/kill switch]
    RISK --> PE[(store: predictions, decisions,<br/>paper_entries - append-only)]
    PE --> SET[settlement.py + ledger.settle_entries] --> SE[(store: settlements<br/>correction history)]
    PE --> AL[alerts.py<br/>namespaces + dedup]
    AL -->|shadow: never| X[x]
    AL -->|paper: promoted model only| TGT[Transport: Null by default]
  end
```

### Namespaces

| namespace | who writes | may alert | may appear in official performance |
|---|---|---|---|
| legacy-production | legacy bot on `main` | yes (legacy Telegram path) | legacy only |
| shadow | any *challenger* via `soccerlab shadow` | **never** | no |
| paper | only a *promoted* model via `soccerlab paper` | yes, deduplicated, opt-in transport | yes |
| ops | health/ingestion checks | yes, deduplicated per kind/subject/day | n/a |

### Module map

| module | responsibility |
|---|---|
| `timeutil` | aware-UTC parsing; naive timestamps rejected |
| `odds` | conversions, power/proportional de-vig, EV, commission, settlement P&L, quote validity |
| `pit` | point-in-time match store: result_at, xg_at, league membership |
| `dcpit` | legacy likelihood with PIT inputs, PIT promoted prior |
| `walkforward` | weekly refit grid, decision-time selection of fits |
| `metrics` | log-loss, Brier, calibration, class-wise ECE, clustered bootstrap, drawdown |
| `clv` | quote events, closing proxy with age, selection-specific CLV |
| `settlement` | 90-minute 1X2 grading, void/review rules |
| `store` | append-only SQLite layers |
| `ledger` | risk engine, paper entries, settlement, performance |
| `alerts` | namespaced, deduplicated alert formatting/transport |
| `provider` | BallDontLie client (centralised endpoint versions) |
| `manifest`, `legacy_baseline`, `registry` | freeze/verify, model roles |
| `evaluation`, `report` | EXP-2026-11-A |
| `datacheck`, `audit` | data verification, health, static audit |
