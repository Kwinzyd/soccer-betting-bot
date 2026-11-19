"""EXP-2026-11-B research odds logger (logging only).

Hard isolation rules, enforced by tests/test_expb.py:

* imports nothing from the legacy bot (model/*, odds_logger/*, bot.py,
  telegram_alerts.py) and nothing from soccerlab's paper ledger, alerting or
  live-prediction modules;
* writes only to its own SQLite database under var/expb/;
* contains no selection, staking or alerting logic -- it records quotes.
"""
