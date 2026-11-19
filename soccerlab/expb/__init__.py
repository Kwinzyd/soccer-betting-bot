"""EXP-2026-11-B research odds logger (logging only).

Hard isolation rules, enforced by tests/test_expb.py:

* imports nothing from the legacy bot (model/*, odds_logger/*, bot.py,
  telegram_alerts.py) and nothing from soccerlab's paper ledger, alerting or
  live-prediction modules;
* writes only to its own SQLite database under var/expb/;
* the logger records quotes only: no staking or alerting anywhere, and the
  single selection function (evaluate.select_entry) runs only in locked
  post-hoc analysis, never at collection time;
* treats every venue's price as observational (none is assumed executable).
"""
