"""soccerlab -- point-in-time research, evaluation and paper-trading layer.

This package sits *beside* the legacy bot (bot.py, model/, odds_logger/). It
never imports anything that sends alerts or writes production state, and it
does not modify legacy modules: the legacy model is imported read-only for
regression comparisons.

Defaults are fail-closed and paper-only. There is no real-money execution
path anywhere in this package.
"""

__version__ = "0.1.0"
PAPER_ONLY = True
