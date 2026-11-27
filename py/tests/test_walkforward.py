from datetime import UTC, datetime

import polars as pl
import pytest

from fairline.core.config import get_config
from fairline.models.football.walkforward import walk_forward
from tests.sim import two_tier_country

CFG = get_config().settings.models
S, WF = CFG.dixon_coles, CFG.walkforward
START, END = datetime(2021, 8, 1, tzinfo=UTC), datetime(2021, 11, 1, tzinfo=UTC)


@pytest.fixture(scope="module")
def matches():
    return two_tier_country(seed=5, seasons=2)[0]


@pytest.fixture(scope="module")
def pooled(matches):
    return walk_forward(matches, "SIM", ["T1", "T2"], S, WF, start=START, end=END)


def test_every_prediction_is_after_its_fit(pooled):
    p = pooled.predictions
    assert p.height > 0
    assert (p["starts_at"] >= p["as_of"]).all()
    assert ((p["starts_at"] - p["as_of"]).dt.total_days() < WF.retrain_every_days).all()


def test_probabilities_valid(pooled):
    p = pooled.predictions
    assert ((p["p_home"] + p["p_draw"] + p["p_away"]) - 1).abs().max() < 1e-9
    assert ((p["p_over25"] + p["p_under25"]) - 1).abs().max() < 1e-9


def test_future_results_cannot_change_predictions(matches, pooled):
    """Scramble every result after END: predictions made before END must be identical."""
    tampered = matches.with_columns(
        pl.when(pl.col("starts_at") >= END).then(9).otherwise(pl.col("home_goals")).cast(pl.Int16).alias("home_goals")
    )
    again = walk_forward(tampered, "SIM", ["T1", "T2"], S, WF, start=START, end=END)
    a = pooled.predictions.sort("event_id")["p_home"].to_numpy()
    b = again.predictions.sort("event_id")["p_home"].to_numpy()
    assert (a == b).all()


def test_same_window_results_cannot_leak(matches):
    """Changing the result of a match being predicted must not change its own prediction."""
    t = datetime(2021, 9, 6, tzinfo=UTC)
    base = walk_forward(matches, "SIM", ["T1", "T2"], S, WF, start=t, end=t.replace(day=13))
    target = base.predictions["event_id"][0]
    tampered = matches.with_columns(
        pl.when(pl.col("event_id") == target).then(7).otherwise(pl.col("home_goals")).cast(pl.Int16).alias("home_goals")
    )
    again = walk_forward(tampered, "SIM", ["T1", "T2"], S, WF, start=t, end=t.replace(day=13))
    assert base.predictions["p_home"].to_list() == again.predictions["p_home"].to_list()


def test_prior_mode_flags_promoted_teams(matches):
    s = S.model_copy(update={"cold_start": "prior"})
    res = walk_forward(matches, "SIM", ["T1", "T2"], s, WF, start=START, end=END)
    t1 = res.predictions.filter(pl.col("competition") == "T1")
    cold = set(t1.filter("home_cold_start")["home_id"]) | set(t1.filter("away_cold_start")["away_id"])
    # exactly the three promoted clubs have no T1 history in a T1-only fit
    seasons = matches.filter(pl.col("competition") == "T1").group_by("season").agg(pl.col("home_id").unique())
    by = {r["season"]: set(r["home_id"]) for r in seasons.iter_rows(named=True)}
    assert cold == by["2021-22"] - by["2020-21"]


def test_pooled_mode_uses_second_tier_history(pooled):
    first_week = pooled.predictions.filter(
        (pl.col("competition") == "T1") & (pl.col("as_of") == pooled.predictions["as_of"].min())
    )
    assert not first_week["home_cold_start"].any() and not first_week["away_cold_start"].any()


def test_score_live_scores_the_horizon_from_a_fit_at_now(matches):
    from datetime import timedelta

    from fairline.models.football.walkforward import score_live

    now, h = datetime(2021, 10, 15, 12, tzinfo=UTC), timedelta(hours=72)
    unplayed = matches.with_columns(
        [
            pl.when(pl.col("starts_at") > now).then(None).otherwise(pl.col(c)).alias(c)
            for c in ("home_goals", "away_goals")
        ]
    )
    live = score_live(unplayed, "SIM", ["T1", "T2"], S, now, h)
    p = live.predictions
    assert p.height > 0 and (p["as_of"] == now).all()
    expected = unplayed.filter((pl.col("starts_at") > now) & (pl.col("starts_at") <= now + h)).height
    assert p.height == expected
    # no leakage: knowing the future results changes nothing (the fit only uses what was known at now)
    peek = score_live(matches, "SIM", ["T1", "T2"], S, now, h)
    assert peek.predictions.height == 0  # those fixtures are already played in `matches`, so nothing to score
    same = score_live(unplayed.vstack(matches.filter(pl.col("starts_at") > now + h)), "SIM", ["T1", "T2"], S, now, h)
    assert same.predictions.select("event_id", "p_home").equals(p.select("event_id", "p_home"))
