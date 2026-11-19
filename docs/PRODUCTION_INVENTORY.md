# Production inventory (observed 2026-11-19, read-only)

Production = the checkout `C:\Users\louis\api-football-data` on branch `main`
at `e1b55fe`. Nothing below was modified by this work.

## Scheduled tasks (Windows Task Scheduler)

| task | schedule | command | working dir | writes | external effects |
|---|---|---|---|---|---|
| `balldontlie-odds-logger` | every 3h (next 17:05 local at time of audit) | `python -u bot.py` | repo root | `odds_logger/log/*.jsonl`, `seen.json`, `latest_poll.json`, `runs.log`, `balldontlie_consolidated/*_2026_matches.json` (+`.refreshed` markers), `model/odds_today.json`, `odds_logger/paper.sqlite3` | BallDontLie API; **Telegram messages** for every flagged selection, every cycle |
| `balldontlie-odds-settle` | daily 04:30–05:30 local | `python -u settle.py --refresh` | `odds_logger` | `results_cache.json`, `settled.jsonl` | BallDontLie API |
| `balldontlie-roster-sync` | daily 03:00–04:00 local | `python -u fetch_rosters.py sync` | repo root | `rosters/*`, `active_rosters.json` | FotMob; Telegram roster message |

Interpreter: `C:\Users\louis\AppData\Local\Microsoft\WindowsApps\python.exe`
(Python 3.13.14, numpy 2.4.1, scipy 1.17.1).

Unrelated tasks seen on the machine (`SoccerBot_*` running from
`Downloads\soccer-bot-antigravity-flat`, `TennisTotalsBot`, etc.) belong to
other projects and were not inspected beyond their names.

## Credentials (locations only; contents never read or logged)

* BallDontLie key: `BALLDONTLIE_KEY` env or `%LOCALAPPDATA%\soccer-betting-bot\bdl.key`
* api-football key: `API_FOOTBALL_KEY` env or `...\api-football.key`
* Telegram: `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` env or `telegram_config.json`
  (gitignored, untracked — verified by `python -m soccerlab audit`)

## State at audit time

* Legacy paper ledger: **0 picks** (table exists, empty).
* Odds log: 811 quote rows, 114 fixtures, Aug–Nov 2026; **September empty**;
  23 rows fail validation (in-play prices).
* `runs.log`, last 24h: **17 HTTP 401**, 2 rate-limit backoffs; latest run
  (19:17 UTC) succeeded with 98 quotes.
* `health` overall: **RED** (auth failures).

## What the next successful cycle will do (unchanged legacy behaviour)

Replaying the 19:17 UTC poll in preview mode: 43 flagged selections on 29
fixtures (14 fixtures with two opposing selections; 31 at Polymarket). In
non-preview mode the bot would insert up to 43 paper picks and send them to
Telegram in chunks of 8, then re-send the still-qualifying ones every 3 hours.
Changing that requires authorization (see RUNBOOK §4).
