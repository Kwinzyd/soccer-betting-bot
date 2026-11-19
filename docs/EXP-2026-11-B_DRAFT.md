# EXP-2026-11-B — Forward early-price validation (DRAFT, not locked)

Status: **draft**. Becomes a pre-registration only when §5 is filled in and the
file is committed as `EXPERIMENT_PLAN_B.md` before any prospective data is
analysed. `soccer-dc-legacy` and `soccer-dc-pit-v1` stay frozen; no model
selects bets in this experiment.

## 1. Hypothesis

Taking the best *executable* pre-kickoff price, when it exceeds a sharp fair
price known at that moment, yields selections that beat a valid closing
reference. Prior evidence (EXP-2026-11-A, SHOP-EARLY@0.0): EV at Pinnacle
closing fair +1.6% [+1.3%, +2.0%], 76% beat close, ROI −1.6% [−7.7%, +4.4%],
using the non-executable market-max column. That is a hypothesis, not proof.

## 2. Data collection

* Quotes at fixed offsets before kickoff: T−72h, −24h, −6h, −60m, −15m, and
  last-available before kickoff (closing reference), all UTC with request and
  provider timestamps, stored as quote events (`soccerlab.store.quotes`).
* Per quote: book/exchange identity, market (1X2, 90 min), selection, price,
  fee/commission, available liquidity where the venue reports it, and
  status (available/suspended/removed).
* Obtainability: a quote counts as executable only if its venue is on the
  approved list (§5) and, for exchanges, displayed liquidity ≥ the paper stake.

## 3. Decision rule (one entry per fixture)

At each offset, for each fixture with no existing entry: compute the fair
reference from the sharp venue's quote at that same moment (power de-vig);
take the single selection/venue with the highest EV = price × fair − 1 after
fees; enter 1 paper unit if EV > threshold. No opposing or repeat entries
(enforced by the store's unique key and the risk engine). Shadow namespace
only; no Telegram selection alerts; nothing in the production paper ledger.

## 4. Primary metric and gates (numbers to be fixed at lock)

* Primary: mean EV at closing fair per selection, fixture-clustered 95% CI;
  pass requires lower bound > 0 with n ≥ 300 entries.
* Secondary: % beating close, odds ratio entry/close, realised ROI (expected
  inconclusive at this n), closing-reference age distribution.
* Report regardless of outcome; a pass makes the strategy eligible only for a
  further pre-registered forward period, not for production.

## 5. Decisions required before locking

1. **Approved venues**: which bookmakers/exchanges are legally accessible and
   realistically usable for you (jurisdiction, account status, limits).
2. **Data source for those venues**: the current BallDontLie key exposes only
   DraftKings, FanDuel and Polymarket moneylines, with no liquidity; other
   venues need a new, timestamped source.
3. **Sharp fair reference** available at decision time (Pinnacle is not in
   the live feed).
4. **Collection cadence**: the −60m/−15m polls need either a change to the
   legacy logger task (production change) or a separate research logger task.
5. EV threshold(s) and the forward sample size/end date.
