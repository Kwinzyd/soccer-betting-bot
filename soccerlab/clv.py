"""Quote events and selection-specific closing-line value.

Terminology (kept strict on purpose):

* entry quote       the price a paper selection was recorded at
* closing proxy     the LAST quote observed before kickoff for that vendor.
                    It is *not* the true closing line: the logger polls every
                    few hours, so the proxy can be hours stale. Its age at
                    kickoff (`close_gap`) is carried on every comparison.
* true close        only where a source documents a closing price
                    (football-data.co.uk PSC*/AvgC* columns).

The legacy settle.report_clv() averaged (close - entry) fair probability
over all three 1X2 outcomes of each quote. Fair probabilities sum to 1 at
both times, so that average is identically zero -- it measures nothing.
Everything here is per *selection*.
"""

import glob
import json
import os
from dataclasses import dataclass

import numpy as np

from . import metrics
from .odds import InvalidOdds, american_to_decimal, fair_from_decimals, validate_1x2
from .timeutil import parse_utc

SIDES = ("home", "draw", "away")


@dataclass(frozen=True)
class Quote:
    league: str
    mid: int
    vendor: str
    observed_at: object
    dec: tuple                    # decimal (home, draw, away)
    vendor_updated_at: object = None
    problems: tuple = ()

    @property
    def usable(self):
        return not self.problems

    @property
    def fair(self):
        return tuple(fair_from_decimals(self.dec))


def quote_from_log_row(r) -> Quote:
    problems = []
    try:
        dec = tuple(american_to_decimal(r[s]) for s in SIDES)
        problems += validate_1x2(dec)
    except (InvalidOdds, KeyError, TypeError) as exc:
        dec = (float("nan"),) * 3
        problems.append(f"unparseable: {exc}")
    vu = r.get("vendor_updated_at")
    return Quote(league=r["league"], mid=int(r["match_id"]), vendor=r["vendor"],
                 observed_at=parse_utc(r["observed_at"]), dec=dec,
                 vendor_updated_at=parse_utc(vu) if vu else None, problems=tuple(problems))


def load_quote_log(log_dir):
    out = []
    for path in sorted(glob.glob(os.path.join(str(log_dir), "*.jsonl"))):
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        out.append(quote_from_log_row(json.loads(line)))
                    except (json.JSONDecodeError, KeyError, ValueError):
                        continue
    return sorted(out, key=lambda q: (q.observed_at, q.league, q.mid, q.vendor))


def quotes_asof(quotes, league, mid, t, kickoff):
    """Latest usable quote per vendor observed at or before t AND strictly
    before kickoff. Future quotes are invisible by construction."""
    latest = {}
    for q in quotes:
        if q.league != league or q.mid != mid or not q.usable:
            continue
        if q.observed_at > t or q.observed_at >= kickoff:
            continue
        if q.vendor not in latest or q.observed_at > latest[q.vendor].observed_at:
            latest[q.vendor] = q
    return latest


def closing_proxy(quotes, league, mid, kickoff, vendor):
    q = quotes_asof(quotes, league, mid, kickoff, kickoff).get(vendor)
    if q is None:
        return None
    return {"quote": q, "gap_seconds": (kickoff - q.observed_at).total_seconds(), "kind": "last_observed_proxy"}


def selection_clv(entry_dec, entry_fair_k, close_dec, close_fair_k):
    """CLV of one selection. Positive = entry was better than the close."""
    return {
        "odds_ratio": entry_dec / close_dec - 1.0,
        "ev_vs_close_fair": entry_dec * close_fair_k - 1.0,
        "prob_move": close_fair_k - entry_fair_k,
        "beat_close": bool(entry_dec > close_dec),
    }


def summarize(rows, cluster_key="fixture", stake_key="stake", n_boot=2000, seed=0):
    """Unweighted and stake-weighted CLV with fixture-clustered CIs.

    rows: dicts carrying odds_ratio, ev_vs_close_fair, prob_move, beat_close,
    stake and a cluster id.
    """
    if not rows:
        return {"n": 0}
    cl = [r[cluster_key] for r in rows]
    st = np.array([r.get(stake_key, 1.0) for r in rows], dtype=float)
    out = {"n": len(rows), "n_fixtures": len(set(cl)),
           "pct_beat_close": float(np.mean([r["beat_close"] for r in rows]))}
    for k in ("odds_ratio", "ev_vs_close_fair", "prob_move"):
        v = np.array([r[k] for r in rows], dtype=float)
        out[k] = metrics.bootstrap_mean(v, cl, n_boot=n_boot, seed=seed)
        out[k + "_stake_weighted"] = metrics.bootstrap_ratio(v * st, st, cl, n_boot=n_boot, seed=seed)
    if "close_gap_seconds" in rows[0]:
        gaps = np.array([r["close_gap_seconds"] for r in rows]) / 3600.0
        out["close_gap_hours"] = {"median": float(np.median(gaps)), "p90": float(np.quantile(gaps, 0.9)),
                                  "max": float(gaps.max())}
    return out


def legacy_report_clv_value(settled_rows):
    """Re-implementation of legacy settle.report_clv's statistic, for the
    regression test proving it is identically ~0."""
    last = {}
    for s in settled_rows:
        k = (s["match_id"], s["vendor"])
        if k not in last or s["observed_at"] > last[k]["observed_at"]:
            last[k] = s
    diffs = []
    for s in settled_rows:
        close = last[(s["match_id"], s["vendor"])]
        if close["observed_at"] == s["observed_at"]:
            continue
        diffs.extend(close["fair"][i] - s["fair"][i] for i in range(3))
    return (sum(diffs) / len(diffs)) if diffs else None
