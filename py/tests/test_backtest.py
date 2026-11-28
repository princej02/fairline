from datetime import UTC, datetime

import numpy as np
import polars as pl
import pytest

from fairline.backtest import metrics as M
from fairline.backtest.data import outcome
from fairline.backtest.engine import STRATEGIES, bankroll_path, fit_blend_weight
from fairline.core.config import get_config
from fairline.edge.board import Candidate, select

S = get_config().settings.edge


def test_log_loss_and_brier():
    assert M.log_loss(np.array([1.0]))[0] == pytest.approx(0)
    assert M.log_loss(np.array([0.5]))[0] == pytest.approx(np.log(2))
    b = M.brier(np.array([[0.5, 0.3, 0.2]]), np.array([0]))
    assert b[0] == pytest.approx(0.25 + 0.09 + 0.04)


def test_wilson_and_calibration():
    lo, hi = M.wilson(50, 100)
    assert lo < 0.5 < hi and hi - lo == pytest.approx(0.192, abs=0.005)
    rng = np.random.default_rng(0)
    p = rng.uniform(0.05, 0.95, 20000)
    y = (rng.uniform(size=p.size) < p).astype(float)
    bins = M.calibration(p, y)
    assert sum(b.consistent for b in bins) >= len(bins) - 1  # a well-calibrated source passes


def test_cluster_bootstrap_is_wider_than_naive_for_correlated_data():
    rng = np.random.default_rng(1)
    clusters = np.repeat(np.arange(100), 10)
    shocks = rng.normal(0, 1, 100)[clusters]  # same-day shared shock
    v = shocks + rng.normal(0, 0.1, clusters.size)
    est, lo, hi = M.cluster_bootstrap(v, clusters, n_boot=1000)
    naive_se = v.std() / np.sqrt(v.size)
    assert (hi - lo) / 3.92 > 2 * naive_se
    w_est, *_ = M.cluster_bootstrap(np.array([1.0, -1.0]), np.array([0, 1]), weights=np.array([3.0, 1.0]))
    assert w_est == pytest.approx(0.5)


def test_max_drawdown():
    assert M.max_drawdown(np.array([1.0, 1.2, 0.9, 1.3, 0.65])) == pytest.approx(0.5)
    assert M.max_drawdown(np.array([1.0, 1.1, 1.2])) == 0


def test_bankroll_compounds_per_slate():
    b = pl.DataFrame({"slate": ["a", "a", "b"], "kelly_pnl": [0.02, -0.01, 0.05]})
    assert bankroll_path(b).tolist() == pytest.approx([1.01, 1.01 * 1.05])


def test_blend_weight_prefers_better_source():
    rng = np.random.default_rng(2)
    n = 6000
    p_true = rng.uniform(0.2, 0.8, n)
    won = rng.uniform(size=n) < p_true
    sharp = np.clip(p_true + rng.normal(0, 0.02, n), 0.01, 0.99)
    noisy = np.clip(p_true + rng.normal(0, 0.15, n), 0.01, 0.99)
    df = pl.DataFrame({"p_model": noisy, "p_ref": sharp, "won": won})
    assert fit_blend_weight(df) <= 0.15
    df2 = pl.DataFrame({"p_model": sharp, "p_ref": noisy, "won": won})
    assert fit_blend_weight(df2) >= 0.85
    assert np.isnan(fit_blend_weight(df.head(100)))  # too little prior data


def test_outcome_settlement():
    assert outcome("1X2", "draw", None, 1, 1) and not outcome("1X2", "home", None, 1, 1)
    assert outcome("OU", "over", 2.5, 2, 1) and outcome("OU", "under", 2.5, 1, 1)


def cand(sel, price, p_model, p_ref, book="bet365", ref_book="pinnacle", flags=()):
    t = datetime(2025, 3, 8, 13, tzinfo=UTC)
    return Candidate(
        event_id=1, label="x", starts_at=t, decision_ts=t, market="1X2", line=None, selection=sel, bookmaker=book,
        odds_ts=t, price=price, effective_price=price, p_model=p_model, p_conservative=None, p_ref=p_ref,
        p_ref_conservative=p_ref, ref_book=ref_book, book_overround=1.05, ev_model=p_model * price - 1,
        ev_vs_ref=p_ref * price - 1, ev_vs_ref_conservative=p_ref * price - 1, ev_conservative=None,
        kelly_full=0, prediction_id=None, slate="d", flags=list(flags),
    )  # fmt: skip


def test_strategies_pick_by_their_own_probability():
    cs = [cand("home", 2.10, 0.55, 0.46), cand("away", 4.00, 0.24, 0.27)]  # model likes home, sharp likes away
    by = {s.name: s for s in STRATEGIES}

    def picked(name, w=0.5):
        st = by[name]
        return [p.candidate.selection for p in select(cs, S, prob_of=lambda c: st.prob_of(c, w), keep=st.keep)]

    assert picked("model") == ["home"]
    assert picked("reference") == ["away"]
    assert picked("model_beats_ref") == []  # home is -3.4% vs sharp
    assert picked("blend", w=0.0) == ["away"] and picked("blend", w=1.0) == ["home"]


def test_reference_never_bets_the_reference_book_itself():
    st = next(s for s in STRATEGIES if s.name == "reference")
    cs = [cand("away", 4.2, 0.24, 0.27, book="betfair_ex_uk", ref_book="betfair_ex_uk")]
    assert select(cs, S, prob_of=lambda c: st.prob_of(c, 0.5), keep=st.keep) == []
