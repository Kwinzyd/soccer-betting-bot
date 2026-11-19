# Match model

Dixon-Coles bivariate Poisson ratings for EPL / La Liga / Serie A / Ligue 1,
fitted on the data in `../balldontlie_consolidated/`.

```
log lambda_home = mu + home_adv + atk[home] - def[away]
log lambda_away = mu           + atk[away] - def[home]
```

Higher `atk` = scores more, higher `def` = concedes less. `mu` is the global
scoring rate; **it is load-bearing** — without it, `mean(atk)=mean(def)=0`
pins average away goals at 1.0 instead of ~1.23, and the ridge silently
distorts every rating that is prior-driven.

Fitted by penalised MLE with an exact analytic gradient (verified against a
numerical gradient to 1e-10; see the check in the git history / rerun it
before trusting any change to `_obj_grad`).

* **Time decay** `w = exp(-xi * days)`, `xi = 0.0015` (half-life 462d)
* **Ridge to prior** `reg = 2.0`. Prior is 0 for established teams and an
  empirically estimated promoted-team offset for sides in their first
  top-flight season, measured from the 2023-2026 promotion cohorts.
* **Dixon-Coles rho** fitted by 1-D search after the ratings (rho is nearly
  orthogonal to the rating block).
* **xG blend** `alpha = 0.3`, geometric in lambda. See caveat below.

## Files

| file | what |
|---|---|
| `dcmodel.py` | model, likelihood, gradient, de-vig helpers |
| `backtest.py` | walk-forward engine; `python backtest.py tune` sweeps xi/reg |
| `blend.py` | xG blend weight sweep |
| `validate.py` | calibration + early-season vs rest-of-season |
| `sharpen.py` | tests whether probabilities need sharpening (they don't) |
| `predict.py` | applies the model to `odds_today.json` |

## Measured performance (walk-forward, nothing leaks)

| | log-loss |
|---|---|
| base rates baseline | ~1.07 |
| **this model, seasons 2023-2025, n=4336** | **0.9874** |
| same, season 2025 only, n=1445 | 0.9993 |
| early season (matchdays 1-3), n=351 | 1.0003 |

Calibration is good: across 11 probability bands the largest gap between
predicted and realised frequency is 1.8pp, and a fitted sharpening exponent
comes out at gamma=1.02 (i.e. no compression to correct). Predicted total
goals are unbiased (+0.045 vs actual).

## Tested against the closing line — the model loses

Historical closing odds were obtained from football-data.co.uk (2022-23 to
2025-26, all four leagues) and joined onto the match records; see
`joinodds.py`, `evaluate.py`. Against **Pinnacle closing**:

| set | n | model | market | gap |
|---|---|---|---|---|
| season 2025 (100% joined) | 749 | 0.9892 | **0.9718** | +0.0173 |
| seasons 2022-25 | 3804 | 0.9982 | **0.9735** | +0.0247 |
| vs market-average closing | 4500 | 0.9998 | **0.9770** | +0.0229 |

Flat-stake betting of the model's disagreements at the closing price, season
2025: **-15.4% ROI** at a 2pp edge threshold, -13.9% at 3pp, -8.8% at 5pp,
-1.7% at 8pp. Over 2022-25: -6.4% / -9.7% / -9.4% / -7.7%. Every threshold
loses.

The blend sweep is the blunt summary: the log-loss-optimal weight on this
model, blended against the closing price, is **0%**. The market already
contains everything the model knows, and more.

So: the model is well calibrated, beats naive baselines comfortably, and is
strictly dominated by the closing line. It is a reasonable *description* of
team strength. It is not an edge, and its disagreements with the price are
model error, not value.

### Closing line value: negative

The decisive diagnostic (`strategy.py`). Taking the model's picks at the
*opening* price and watching where the line goes by kickoff:

| edge threshold | picks | mean move, open -> close |
|---|---|---|
| 2pp | 3799 | **-0.04pp** (away from us) |
| 3pp | 3076 | **-0.05pp** (away from us) |
| 5pp | 1963 | **-0.03pp** (away from us) |

A model with genuine information sees the market drift *toward* its picks.
This one drifts away. Negative CLV means no staking scheme, bankroll rule or
bet-selection filter turns it profitable — the picks are simply wrong.

### Strategies tested (2022-25, 4500 matches, flat stakes)

| strategy | ROI |
|---|---|
| model edge vs close, bet close | -6.4% to -9.4% |
| model edge vs open, bet best open price | -7.8% to -8.2% |
| no model, best open price > sharp fair | +0.1% to -1.9% |
| no model, best **close** price > sharp fair | +1.2% to +3.3% |

The only positive line uses **no model at all** — it is pure line shopping
against Pinnacle's de-vigged fair price. And it is *not* statistically
established: at its best threshold, ROI +3.31%, SE 2.91pp, **t = 1.14**,
95% CI **[-2.2%, +9.0%]**. The interval straddles zero. Confirming a true 3%
edge at this variance needs roughly **12,000 bets**, about 3x the sample here.

Other known limits:

* No lineups, injuries, suspensions, transfers, or managerial changes. In
  August this is severe — squads have turned over since the ratings' data.
* No European/cup fixture congestion or rotation.
* Promoted sides with no top-flight history (e.g. Hull City) are pure prior:
  the model knows only "a promoted team", nothing about *this* promoted team.
* The xG blend improves totals modestly but its 1X2 gain (1.0032 -> 1.0018)
  is within noise and is not consistent across leagues — Serie A gets worse.
  alpha=0.3 is a conservative compromise, not a strongly evidenced choice.
