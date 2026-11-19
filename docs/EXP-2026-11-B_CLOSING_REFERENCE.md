# Closing reference for EXP-2026-11-B — what is documented, what is inferred

Status: the football-data.co.uk Betfair Exchange close is **provisional**. It
may be used as the strong reference only after the open questions in §4 are
answered, or with those limitations stated in the locked protocol.

## 1. What football-data.co.uk documents (fetched 2026-11-19)

From `https://www.football-data.co.uk/notes.txt` and `data.php` (data page
"Updated: 16/11/26"):

* `BFEH/BFED/BFEA` = "Betfair Exchange home/draw/away win odds". `BFH/BFD/BFA`
  = "Betfair" with no qualifier. `BFDH...` is **Betfred**, a different bookmaker.
* Closing columns follow a general rule: the same abbreviation "with an
  additional "C" character following the bookmaker abbreviation" (example
  given: B365CH). `BFECH/BFECD/BFECA` = closing Betfair Exchange is an
  application of that rule, not an explicitly listed column.
* Pre-closing odds are "collected Friday afternoons for weekend fixtures, and
  on Tuesday afternoons for midweek games". The closing set is collected
  separately (since 2019/20).
* Since 23/07/2025 Pinnacle's odds are described as unreliable
  ("systematically out of date") and are excluded from Max/Avg.

**Not stated anywhere:** whether Betfair Exchange prices are back, lay, mid
or last-traded; whether commission is deducted; at what time relative to
kickoff the closing set is captured; the timezone of the `Time` column.

## 2. What the data itself shows (inference, not documentation)

1,772 fixtures (2025-26 and 2026-27 to date) with `BFEC*`:

| statistic | value | reading |
|---|---|---|
| sum of implied probabilities | always > 1; overround median 0.53%, p5 0.12%, p95 0.99% | best **back** prices: lay prices would sum below 1 and mid prices would straddle 1 |
| overround level | < 1% | **before commission**: a 2–5% commission on winnings would push the effective margin well above this |
| pre-closing `BFE*` overround | median 0.77% | the market tightens towards close, as expected |
| vs Pinnacle close (2025-26, n=749) | max per-outcome fair-probability gap: median 0.74pp, p95 3.0pp | similar sharpness; no systematic disagreement |
| vs Bet365 close | median 1.05–1.09pp, p95 ~2.2pp | — |

Working assumption: `BFEC*` = best available back price on the Betfair
Exchange around the off, before commission, captured at an unpublished time.
The analysis de-vigs it (power method), which removes the back-side overround.

## 3. Independently timestamped Betfair alternatives

| source | what it contains | access |
|---|---|---|
| Betfair Historical Data (BASIC) | last traded price at 1-minute intervals | registered Betfair customers; purchase/download |
| Betfair Historical Data (ADVANCED) | adds best available back/lay prices and volumes, 1-second frequency | as above |
| Betfair Historical Data (PRO) / stream files | full market-stream data incl. BSP messages, back to 2016 (per Betfair developer docs) | as above, higher cost |
| Betfair Exchange Stream API (live) | live back/lay ladders, traded volume; record the last pre-off state yourself | Betfair account, legally available jurisdiction required |
| Betfair Starting Price (football) | patchy; historic football BSP not published per Betfair FAQ | — |

Only these give a **timestamped** back/lay/traded close with liquidity. Each
requires a Betfair account, and live access requires a jurisdiction where
Betfair Exchange is legal. Using exchange data *only as a reference* does not
require betting there, but the account and licensing terms still apply.

## 4. Open questions before the reference can be final

1. Confirm back vs lay vs last-traded and commission treatment with football-data
   (contact via the site) or by matching a sample against Betfair Historical Data.
2. Closing capture time relative to kickoff (needed to know whether it is a
   true close or a "near close").
3. Decide whether to buy Betfair Historical Data (ADVANCED is the cheapest
   tier with back/lay prices) to validate or replace the free column.
