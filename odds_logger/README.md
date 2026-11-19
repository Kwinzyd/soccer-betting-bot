# Odds logger

Builds the dataset that does not otherwise exist.

Every conclusion in `../model/` rests on football-data.co.uk having archived
historical closing odds for 1X2. That archive is why we know the Dixon-Coles
model *loses* to the market rather than merely suspecting it. For player props,
line movement, and multi-vendor pricing there is no equivalent archive — so
anything built on those is unfalsifiable until someone records it. This records
it.

## What it captures

`GET /{league}/{v}/odds` for fixtures kicking off within the next 4 days, across
`draftkings`, `fanduel` and `polymarket`. Moneyline (1X2) only — that endpoint
exposes nothing else. No props, no totals, no handicaps.

Note the version split: `laliga`/`seriea`/`ligue1` serve **v1**, `epl` serves
**v2**. Hitting the wrong one 404s and logs nothing silently.

## Running

Already installed as a Windows scheduled task, every 3 hours:

```
schtasks /Query /TN balldontlie-odds-logger     # check
schtasks /Run   /TN balldontlie-odds-logger     # run now
powershell -ExecutionPolicy Bypass -File install_task.ps1 -Uninstall
```

One run is ~4 API calls and ~55s, nowhere near the quota. The 13s spacing is the
key's 5 req/min ceiling.

## Storage

`log/{league}_{YYYY-MM}.jsonl`, append-only, one line per observation. A quote is
written **only when it changes** — re-logging identical prices every 3 hours
would bury the line movement this exists to capture. Dedup state is `seen.json`;
deleting it re-logs current prices once but never damages history.

## Reading

```
python read_log.py summary    # how much history exists
python read_log.py spread     # best price vs vendor average
python read_log.py moves      # fixtures whose price has moved
```

## First measurement

Mean best-vs-average premium across 105 outcomes / 36 fixtures: **+3.88%**.

That is the line-shopping edge, and it is roughly the size of the overround —
consistent with why shopping was the only strategy in `../model/strategy.py`
that returned positive ROI.

Treat it cautiously:

* Only 3 vendors, and `polymarket` is a prediction market, not a sportsbook.
  The largest gaps (Inter v Monza away +16.6%, Frosinone home +11.6%) are almost
  certainly thin/illiquid quotes rather than prices you could actually get size
  on. The median fixture is nearer +3%.
* A premium over the *average* is not profit. Profit requires beating the sharp
  fair price after vig, which tested at **t = 1.14** — not significant.
* Books limit accounts that consistently take best price. That is the binding
  constraint, and no dataset fixes it.

## What this is for

In a few months this becomes the thing currently missing: real historical odds
with line movement, from which you can measure closing-line value on your own
picks, test whether Polymarket systematically diverges from the books, and check
whether any future model has information before the market prices it in.

Until then it is just accumulating. Don't bet off a week of it.
