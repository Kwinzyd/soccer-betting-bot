"""Paper ledger and risk engine. Paper-only: there is no execution path.

Every candidate goes through `RiskEngine.consider()`, which either records
an immutable paper entry or a no-bet decision, always with reasons. Checks
(any failure -> no bet):

  kill switch           file var/PAPER_KILL_SWITCH or env SOCCERLAB_PAPER_KILL=1
  namespace/model       'paper' requires a PROMOTED model; 'shadow' any registered one
  model health          model_ok (optimiser converged), probabilities coherent
  data quality          caller-supplied data flags must be empty
  quote validity        decimal > 1, coherent book, observed before kickoff
  quote staleness       decision_time - quote.observed_at <= max_quote_age
  lead time             decision_time <= kickoff - min_lead
  duplicates            one entry per (namespace, fixture, market): repeated
                        polling cannot re-enter, and opposing selections on the
                        same fixture are rejected rather than treated as
                        independent value
  exposure caps         stake, daily, concurrent (unsettled), league, vendor
"""

import os
from dataclasses import dataclass, field
from datetime import timedelta

from .hashing import sha256_json
from .odds import InvalidOdds, check_decimal, expected_value, settle_pnl
from .paths import var_dir
from .timeutil import iso, utcnow

KILL_FILE = "PAPER_KILL_SWITCH"


@dataclass(frozen=True)
class RiskLimits:
    stake: float = 1.0
    max_stake: float = 1.0
    max_daily_units: float = 20.0
    max_concurrent_units: float = 40.0
    max_league_units: float = 12.0
    max_vendor_units: float = 25.0
    max_quote_age: timedelta = timedelta(minutes=30)
    min_lead: timedelta = timedelta(minutes=5)
    min_ev: float = 0.02
    min_prob: float = 0.02


@dataclass
class Candidate:
    namespace: str
    model_id: str
    model_status: str            # registry status of model_id
    prediction_uid: str
    match_uid: str
    league: str
    market: str
    selection: str
    vendor: str
    decimal_odds: float
    quote_observed_at: object
    decision_time: object
    kickoff: object
    model_prob: float
    fair_prob: float = None
    model_ok: bool = True
    data_flags: list = field(default_factory=list)
    rule: str = "argmax_ev>min_ev"


def kill_switch_on():
    return os.environ.get("SOCCERLAB_PAPER_KILL") == "1" or (var_dir() / KILL_FILE).exists()


class RiskEngine:
    def __init__(self, store, limits: RiskLimits = None):
        self.store = store
        self.limits = limits or RiskLimits()

    # -------------------------------------------------------------- exposure

    def _open_units(self, namespace, where="", params=()):
        # `where` is only ever one of the constant clauses in check(); values are bound.
        if where not in ("", "AND e.league=?", "AND e.vendor=?"):
            raise ValueError("unexpected exposure filter")
        rows = self.store.db.execute(f"""
            SELECT e.stake FROM paper_entries e
            LEFT JOIN (SELECT entry_uid, state FROM settlements s1 WHERE id = (
                SELECT MAX(id) FROM settlements s2 WHERE s2.entry_uid = s1.entry_uid)) s
            ON s.entry_uid = e.entry_uid
            WHERE e.namespace = ? AND (s.state IS NULL OR s.state IN ('pending','review')) {where}""",  # noqa: S608
                                     (namespace, *params)).fetchall()
        return sum(r[0] for r in rows)

    def _daily_units(self, namespace, decision_time):
        day = iso(decision_time)[:10]
        r = self.store.db.execute("SELECT COALESCE(SUM(stake),0) FROM paper_entries WHERE namespace=? "
                                  "AND substr(decision_time,1,10)=?", (namespace, day)).fetchone()
        return r[0]

    # -------------------------------------------------------------- decision

    def check(self, c: Candidate):
        L, reasons = self.limits, []
        if kill_switch_on():
            reasons.append("kill switch engaged")
        if c.namespace not in ("shadow", "paper"):
            reasons.append(f"namespace {c.namespace!r} cannot hold paper entries")
        if c.namespace == "paper" and c.model_status != "promoted":
            reasons.append(f"model {c.model_id} is {c.model_status!r}, not promoted: paper namespace refused")
        if not c.model_ok:
            reasons.append("model unavailable or did not converge")
        if not (L.min_prob <= c.model_prob <= 1 - L.min_prob):
            reasons.append(f"model probability {c.model_prob:.4f} outside sane range")
        reasons += [f"data: {f}" for f in c.data_flags]
        try:
            d = check_decimal(c.decimal_odds)
        except InvalidOdds as exc:
            reasons.append(f"invalid price: {exc}")
            d = None
        if c.quote_observed_at >= c.kickoff:
            reasons.append("quote observed at/after kickoff")
        if c.quote_observed_at > c.decision_time:
            reasons.append("quote from the future relative to decision time")
        if c.decision_time - c.quote_observed_at > L.max_quote_age:
            reasons.append(f"stale quote ({c.decision_time - c.quote_observed_at} old)")
        if c.decision_time > c.kickoff - L.min_lead:
            reasons.append("too close to (or after) kickoff")
        ev = expected_value(c.model_prob, d) if d else None
        if ev is not None and ev <= L.min_ev:
            reasons.append(f"EV {ev:+.4f} <= {L.min_ev}")
        dup = self.store.db.execute("SELECT selection FROM paper_entries WHERE namespace=? AND match_uid=? "
                                    "AND market=?", (c.namespace, c.match_uid, c.market)).fetchone()
        if dup:
            same = dup[0] == c.selection
            reasons.append("duplicate entry for this fixture/market" if same else
                           f"fixture already has an opposing/correlated entry ({dup[0]})")
        s = L.stake
        if s > L.max_stake:
            reasons.append("stake above maximum")
        if self._daily_units(c.namespace, c.decision_time) + s > L.max_daily_units:
            reasons.append("daily exposure cap")
        if self._open_units(c.namespace) + s > L.max_concurrent_units:
            reasons.append("concurrent exposure cap")
        if self._open_units(c.namespace, "AND e.league=?", (c.league,)) + s > L.max_league_units:
            reasons.append(f"league exposure cap ({c.league})")
        if self._open_units(c.namespace, "AND e.vendor=?", (c.vendor,)) + s > L.max_vendor_units:
            reasons.append(f"vendor exposure cap ({c.vendor})")
        return reasons, ev

    def consider(self, c: Candidate):
        """Record an entry or a no-bet decision. Returns (entry_uid|None, reasons)."""
        reasons, ev = self.check(c)
        if reasons:
            self.store.log_decision(c.namespace, c.model_id, c.match_uid, c.market, c.decision_time, "no_bet",
                                    c.selection, reasons)
            return None, reasons
        uid = sha256_json({"ns": c.namespace, "match": c.match_uid, "market": c.market})[:32]
        with self.store.tx() as db:
            db.execute("""INSERT INTO paper_entries (entry_uid, namespace, prediction_uid, model_id, match_uid,
                league, market, selection, vendor, decimal_odds, quote_observed_at, stake, model_prob, fair_prob,
                implied_prob, ev, rule, decision_time, kickoff, created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                       (uid, c.namespace, c.prediction_uid, c.model_id, c.match_uid, c.league, c.market,
                        c.selection, c.vendor, float(c.decimal_odds), iso(c.quote_observed_at), self.limits.stake,
                        c.model_prob, c.fair_prob, 1.0 / c.decimal_odds, ev, c.rule, iso(c.decision_time),
                        iso(c.kickoff), iso(utcnow())))
        self.store.log_decision(c.namespace, c.model_id, c.match_uid, c.market, c.decision_time, "bet",
                                c.selection, [])
        return uid, []


def settle_entries(store, namespace, grader, source):
    """grader(entry) -> (state, outcome, note). Appends settlement events."""
    n = 0
    for e in store.entries(namespace):
        state, outcome, note = grader(e)
        pnl = settle_pnl(e["stake"], e["decimal_odds"], outcome) if state in ("settled", "void") else None
        if store.put_settlement(e["entry_uid"], state, outcome, pnl, source, note):
            n += 1
    return n


def performance(store, namespace):
    """Realised P&L from settled entries at their quoted entry price; open
    exposure reported separately (never marked to model)."""
    rows = store.db.execute("""
        SELECT e.stake, s.state, s.pnl FROM paper_entries e LEFT JOIN (
            SELECT entry_uid, state, pnl FROM settlements s1 WHERE id = (
                SELECT MAX(id) FROM settlements s2 WHERE s2.entry_uid = s1.entry_uid)) s
        ON s.entry_uid = e.entry_uid WHERE e.namespace = ?""", (namespace,)).fetchall()
    settled = [(st, p) for st_, state, p in rows for st in [st_] if state == "settled"]
    void = sum(1 for _, state, _ in rows if state == "void")
    open_units = sum(st for st, state, _ in rows if state not in ("settled", "void"))
    turnover = sum(st for st, _ in settled)
    profit = sum(p for _, p in settled)
    return {"entries": len(rows), "settled": len(settled), "void": void,
            "open_units": open_units, "realised_turnover": turnover, "realised_profit": profit,
            "realised_roi": (profit / turnover) if turnover else None}
