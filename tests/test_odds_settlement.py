"""Odds arithmetic, de-vig, EV, settlement and CLV."""

import importlib.util
import math
from datetime import timedelta

import numpy as np
import pytest

from soccerlab import clv, odds, settlement
from soccerlab.paths import REPO_ROOT
from soccerlab.timeutil import iso, parse_utc


# ----------------------------------------------------------------- conversions

@pytest.mark.parametrize("a,d", [(100, 2.0), (-100, 2.0), (150, 2.5), (-200, 1.5), (-110, 1 + 100 / 110),
                                 (1000, 11.0), (-1000, 1.1)])
def test_american_to_decimal(a, d):
    assert odds.american_to_decimal(a) == pytest.approx(d, abs=1e-12)


@pytest.mark.parametrize("bad", [0, 50, -99.9, float("nan"), float("inf"), None, "x"])
def test_american_invalid_rejected(bad):
    with pytest.raises(odds.InvalidOdds):
        odds.american_to_decimal(bad)


@pytest.mark.parametrize("a", [100, 150, -110, -250, 400, -10000])
def test_american_roundtrip(a):
    back = odds.decimal_to_american(odds.american_to_decimal(a))
    assert back == pytest.approx(a if a != -100 else 100, rel=1e-12)


@pytest.mark.parametrize("bad", [1.0, 0.5, -2, float("nan")])
def test_decimal_invalid_rejected(bad):
    with pytest.raises(odds.InvalidOdds):
        odds.check_decimal(bad)


def test_legacy_american_conversions_agree():
    M = importlib.import_module("soccerlab.legacy").modules()["dcmodel"]
    for a in (100, 150, -110, -250, 400):
        assert M.american_to_decimal(a) == pytest.approx(odds.american_to_decimal(a))
        assert M.american_to_prob(a) == pytest.approx(1 / odds.american_to_decimal(a))


# --------------------------------------------------------------------- de-vig

def test_devig_noop_on_fair_book():
    assert np.allclose(odds.devig_power([0.5, 0.25, 0.25]), [0.5, 0.25, 0.25])


def test_devig_power_properties():
    imp = np.array([1 / 1.8, 1 / 3.5, 1 / 4.0])
    f = odds.devig_power(imp)
    assert f.sum() == pytest.approx(1.0, abs=1e-12)
    prop = odds.devig_proportional(imp)
    # power takes proportionally more margin from the longshot
    assert f[2] < prop[2] and f[0] > prop[0]
    assert np.all(f < imp)


def test_devig_power_negative_overround_and_wide_books():
    under = odds.devig_power([0.45, 0.25, 0.25])       # sums to 0.95 (cross-book best prices)
    assert under.sum() == pytest.approx(1.0) and np.all(under > [0.45, 0.25, 0.25])
    wide = odds.devig_power([0.7, 0.35, 0.3])           # 35% overround: needs bracket widening
    assert wide.sum() == pytest.approx(1.0)


def test_devig_rejects_invalid():
    with pytest.raises(odds.InvalidOdds):
        odds.devig_power([0.5, 0.0, 0.5])
    with pytest.raises(odds.InvalidOdds):
        odds.devig_power([1.2, 0.1, 0.1])


def test_legacy_devig_matches_on_normal_books():
    M = importlib.import_module("soccerlab.legacy").modules()["dcmodel"]
    imp = [1 / 2.1, 1 / 3.4, 1 / 3.6]
    assert np.allclose(M.devig_power(imp), odds.devig_power(imp), atol=1e-9)


# ------------------------------------------------------------------ EV / P&L

def test_ev_and_commission():
    assert odds.expected_value(0.5, 2.2) == pytest.approx(0.1)
    # 5% commission on winnings: net odds 1 + 1.2*0.95 = 2.14
    assert odds.expected_value(0.5, 2.2, commission=0.05) == pytest.approx(0.5 * 2.14 - 1)
    assert odds.kelly_fraction(0.4, 2.0) == 0.0
    assert odds.kelly_fraction(0.6, 2.0) == pytest.approx(0.2)


@pytest.mark.parametrize("outcome,expected", [("win", 1.5), ("loss", -1.0), ("void", 0.0)])
def test_settle_pnl(outcome, expected):
    assert odds.settle_pnl(1.0, 2.5, outcome) == pytest.approx(expected)


def test_settle_pnl_commission_and_errors():
    assert odds.settle_pnl(2.0, 3.0, "win", commission=0.02) == pytest.approx(2 * 2 * 0.98)
    with pytest.raises(ValueError):
        odds.settle_pnl(1.0, 2.0, "push?")
    with pytest.raises(ValueError):
        odds.settle_pnl(-1.0, 2.0, "win")


def test_legacy_paper_pnl_matches_american_settlement():
    # legacy paper.py: pnl = odds/100 if odds>0 else -100/odds for a winning 1u bet
    for a in (150, -150, 100, 340):
        legacy = a / 100.0 if a > 0 else -100.0 / a
        assert legacy == pytest.approx(odds.settle_pnl(1.0, odds.american_to_decimal(a), "win"))


def test_validate_1x2_rejects_degenerate_inplay_prices():
    deg = [odds.american_to_decimal(a) for a in (-100000, 25000, 35000)]
    assert odds.validate_1x2(deg)
    assert odds.validate_1x2([2.1, 3.4, 3.6]) == []
    assert odds.validate_1x2([3.0, 3.5, 4.0])          # arbitrage-negative book


# ---------------------------------------------------------------- settlement

def _m(status, state, hs=None, as_=None, date="2026-11-21T15:00:00.000Z"):
    return {"status": status, "status_state": state, "home_score": hs, "away_score": as_, "date": date}


def test_grade_full_time():
    assert settlement.grade_1x2("home", _m("STATUS_FULL_TIME", "final", 2, 1))[:2] == ("settled", "win")
    assert settlement.grade_1x2("draw", _m("STATUS_FULL_TIME", "final", 2, 1))[:2] == ("settled", "loss")
    assert settlement.grade_1x2("draw", _m("STATUS_FULL_TIME", "final", 1, 1))[:2] == ("settled", "win")


def test_grade_scheduled_zero_zero_is_pending_not_a_draw():
    assert settlement.grade_1x2("draw", _m("STATUS_SCHEDULED", "scheduled", 0, 0))[0] == "pending"


def test_grade_void_and_review_cases():
    assert settlement.grade_1x2("home", _m("STATUS_POSTPONED", "post"))[0] == "void"
    assert settlement.grade_1x2("home", _m("STATUS_ABANDONED", "abandoned"))[0] == "void"
    assert settlement.grade_1x2("home", _m("STATUS_SUSPENDED", "suspended"))[0] == "pending"
    assert settlement.grade_1x2("home", _m("STATUS_FINAL_AET", "final", 2, 1))[0] == "review"
    moved = _m("STATUS_FULL_TIME", "final", 1, 0, date="2026-12-09T20:00:00.000Z")
    assert settlement.grade_1x2("home", moved, entry_kickoff="2026-11-21T15:00:00.000Z")[0] == "void"


# ----------------------------------------------------------------------- CLV

def _row(mid, vendor, t, h, d, a, league="epl"):
    return {"observed_at": t, "league": league, "match_id": mid, "vendor": vendor, "home": h, "draw": d, "away": a}


def test_selection_clv_signs():
    better = clv.selection_clv(2.2, 0.44, 2.0, 0.48)
    assert better["odds_ratio"] == pytest.approx(0.1) and better["beat_close"]
    assert better["ev_vs_close_fair"] == pytest.approx(2.2 * 0.48 - 1)
    assert better["prob_move"] > 0
    worse = clv.selection_clv(2.0, 0.48, 2.2, 0.44)
    assert worse["odds_ratio"] < 0 and not worse["beat_close"] and worse["prob_move"] < 0


def test_legacy_report_clv_is_identically_zero():
    """Fair probabilities sum to 1, so averaging the move over all three
    outcomes cancels -- whatever the market does."""
    rng = np.random.default_rng(3)
    rows = []
    for mid in range(40):
        for t in ("2026-11-20T09:00:00+00:00", "2026-11-21T12:00:00+00:00"):
            p = rng.dirichlet([4, 3, 3]) * 1.06
            rows.append({"match_id": mid, "vendor": "x", "observed_at": t,
                         "fair": list(odds.devig_power(np.clip(p, 0.02, 0.95)))})
    assert abs(clv.legacy_report_clv_value(rows)) < 1e-12


def test_legacy_settle_report_clv_prints_zero(monkeypatch, capsys):
    monkeypatch.setenv("BALLDONTLIE_KEY", "dummy-not-a-key")
    spec = importlib.util.spec_from_file_location("legacy_settle", REPO_ROOT / "odds_logger" / "settle.py")
    S = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(S)
    log = [_row(1, "x", "2026-11-20T09:00:00+00:00", 150, 240, 200),
           _row(1, "x", "2026-11-21T12:00:00+00:00", -150, 300, 450)]     # big move to home
    s = S.build_settled(log, {"1": {"league": "epl", "home_score": 1, "away_score": 0, "outcome": 0}},
                        {1: ("2026-11-21T15:00:00.000Z", "A v B")})
    S.report_clv(s)
    out = capsys.readouterr().out
    assert "mean drift = +0.000pp" in out or "mean drift = -0.000pp" in out, out


def test_legacy_settle_report_shopping_counts_repeat_observations(monkeypatch, capsys):
    monkeypatch.setenv("BALLDONTLIE_KEY", "dummy-not-a-key")
    spec = importlib.util.spec_from_file_location("legacy_settle2", REPO_ROOT / "odds_logger" / "settle.py")
    S = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(S)
    log = []
    for hour in ("06", "09", "12"):                          # same fixture, same prices, 3 polls
        t = f"2026-11-21T{hour}:00:00+00:00"
        log += [_row(1, "a", t, 200, 240, 150), _row(1, "b", t, 150, 240, 150)]
    s = S.build_settled(log, {"1": {"league": "epl", "home_score": 1, "away_score": 0, "outcome": 0}},
                        {1: ("2026-11-21T15:00:00.000Z", "A v B")})
    S.report_shopping(s)
    out = capsys.readouterr().out
    assert "bets=3" in out, out          # one opportunity counted three times


def test_quotes_asof_ignores_future_and_postkickoff():
    ko = parse_utc("2026-11-21T15:00:00Z")
    rows = [_row(7, "dk", "2026-11-20T09:00:00+00:00", 150, 240, 200),
            _row(7, "dk", "2026-11-21T12:00:00+00:00", 140, 240, 210),
            _row(7, "dk", "2026-11-21T15:30:00+00:00", -100000, 25000, 35000)]   # in-play junk
    qs = [clv.quote_from_log_row(r) for r in rows]
    assert not qs[2].usable
    at_t = clv.quotes_asof(qs, "epl", 7, parse_utc("2026-11-21T10:00:00Z"), ko)
    assert at_t["dk"].observed_at == parse_utc("2026-11-20T09:00:00Z")
    # adding a later quote cannot change what was visible at t
    qs2 = qs + [clv.quote_from_log_row(_row(7, "dk", "2026-11-21T09:59:59+00:00", 300, 300, 300))]
    assert clv.quotes_asof(qs2, "epl", 7, parse_utc("2026-11-21T09:00:00Z"), ko) == \
        clv.quotes_asof(qs, "epl", 7, parse_utc("2026-11-21T09:00:00Z"), ko)
    cp = clv.closing_proxy(qs, "epl", 7, ko, "dk")
    assert cp["kind"] == "last_observed_proxy" and cp["gap_seconds"] == 3 * 3600


def test_clv_summary_ci_and_clustering():
    rows = [dict(clv.selection_clv(2.1, 0.46, 2.0, 0.48), fixture=i // 2, stake=1.0) for i in range(60)]
    s = clv.summarize(rows, n_boot=300)
    assert s["n"] == 60 and s["n_fixtures"] == 30 and s["pct_beat_close"] == 1.0
    assert s["odds_ratio"]["lo"] == pytest.approx(0.05) == s["odds_ratio"]["hi"]


def test_iso_roundtrip_and_naive_rejected():
    t = parse_utc("2026-11-21T15:00:00.000Z")
    assert parse_utc(iso(t)) == t
    assert parse_utc("2026-11-21T10:00:00-05:00") == t
    with pytest.raises(ValueError):
        parse_utc("2026-11-21T15:00:00")
    assert math.isclose((t + timedelta(hours=1) - t).total_seconds(), 3600)
