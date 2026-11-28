from datetime import UTC, datetime, timedelta

import numpy as np
import polars as pl
import pytest
from scipy.optimize import approx_fprime

from fairline.core.config import get_config
from fairline.models.football import dixon_coles as dc
from fairline.models.football.scores import score_matrices, score_matrix
from tests.sim import two_tier_country

S = get_config().settings.models.dixon_coles


@pytest.fixture(scope="module")
def country():
    return two_tier_country(seed=3, seasons=3)


def test_score_matrix_normalised_and_grid_enforced():
    m = score_matrix(3.5, 2.8, -0.1, 10)
    assert m.shape == (11, 11)
    assert m.sum() == pytest.approx(1.0, abs=1e-12)
    with pytest.raises(ValueError):
        score_matrix(1.4, 1.1, -0.1, 8)


def test_vectorised_matrix_matches_scalar():
    lam, mu, rho = np.array([1.2, 2.4]), np.array([0.9, 1.7]), np.array([-0.1, 0.05])
    ms = score_matrices(lam, mu, rho, 10)
    for k in range(2):
        np.testing.assert_allclose(ms[k], score_matrix(lam[k], mu[k], rho[k], 10), atol=1e-14)


def test_analytic_gradient_matches_finite_differences(country):
    matches, *_ = country
    train = dc.training_frame(matches, datetime(2021, 6, 1, tzinfo=UTC), S)
    teams = sorted(set(train["home_id"]) | set(train["away_id"]))
    ti = {t: i for i, t in enumerate(teams)}
    data = dc.FitData(
        home=np.array([ti[t] for t in train["home_id"]]),
        away=np.array([ti[t] for t in train["away_id"]]),
        div=np.array([0 if c == "T1" else 1 for c in train["competition"]]),
        hg=train["home_goals"].to_numpy().astype(float),
        ag=train["away_goals"].to_numpy().astype(float),
        w=train["weight"].to_numpy(),
        n_teams=len(teams),
        n_div=2,
        group=np.array([t % 2 for t in teams]),
    )
    rng = np.random.default_rng(0)
    theta = rng.normal(0, 0.2, dc.n_params(len(teams), 2))
    theta[-1] = -0.1
    _, g = dc.neg_log_posterior(theta, data, S.prior_sd)
    num = approx_fprime(theta, lambda th: dc.neg_log_posterior(th, data, S.prior_sd)[0], 1e-7)
    np.testing.assert_allclose(g, num, rtol=1e-4, atol=1e-7)


def test_recovers_simulated_strengths(country):
    matches, att, dfn, _ = country
    p = dc.fit(matches, datetime(2023, 7, 1, tzinfo=UTC), S.model_copy(update={"half_life_days": 2000}))
    assert p.converged
    true_strength = np.array([att[t] + dfn[t] for t in p.teams])
    fitted = p.attack + p.defence
    assert np.corrcoef(true_strength, fitted)[0, 1] > 0.85
    assert p.home_adv == pytest.approx([0.25, 0.25], abs=0.08)
    assert -0.25 < p.rho < 0.1


def test_training_frame_cutoff_is_strict():
    t = datetime(2024, 1, 10, 12, tzinfo=UTC)
    m = pl.DataFrame(
        {
            "starts_at": [t - timedelta(hours=3), t - timedelta(hours=1), t + timedelta(hours=1)],
            "competition": ["X"] * 3,
            "home_id": [1, 2, 3],
            "away_id": [2, 3, 1],
            "home_goals": [1, 2, 0],
            "away_goals": [0, 2, 1],
        }  # fmt: skip
    ).with_columns(pl.col("home_goals").cast(pl.Int16), pl.col("away_goals").cast(pl.Int16))
    tr = dc.training_frame(m, t, S)  # result_delay_hours = 2
    assert tr.height == 1 and tr["starts_at"][0] == t - timedelta(hours=3)


def test_unknown_team_gets_bottom_quartile_prior(country):
    matches, *_ = country
    p = dc.fit(matches, datetime(2021, 6, 1, tzinfo=UTC), S)
    r = dc.team_rating(p, team_id=999, division="T1", s=S)
    assert r.cold_start
    t1 = np.array([ld == "T1" for ld in p.last_division])
    assert r.attack == pytest.approx(np.quantile(p.attack[t1], 0.25))
    assert r.attack < np.median(p.attack[t1])


def test_pooled_fit_puts_second_tier_below_top_tier(country):
    """The scale link through promoted/relegated clubs must place T2 teams below T1 on average."""
    matches, *_ = country
    p = dc.fit(matches, datetime(2022, 6, 1, tzinfo=UTC), S)
    s = p.attack + p.defence
    in_t1 = np.array([ld == "T1" for ld in p.last_division])
    assert s[in_t1].mean() - s[~in_t1].mean() > 0.3
