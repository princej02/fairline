from datetime import UTC, datetime, timedelta

import polars as pl
import pytest

from fairline.core.config import get_config
from fairline.models.football.bayes import BayesResult, fit_and_predict
from tests.sim import two_tier_country

CFG = get_config().settings.models


@pytest.mark.slow
def test_bayes_bands_bracket_mean_and_are_valid():
    matches = two_tier_country(seed=11, seasons=2)[0]
    t = datetime(2021, 10, 4, tzinfo=UTC)
    fixtures = matches.filter((pl.col("starts_at") >= t) & (pl.col("starts_at") < t + timedelta(days=7)))
    b = CFG.bayes.model_copy(update={"draws": 200, "tune": 300, "chains": 2})
    r = fit_and_predict(matches, fixtures, t, CFG.dixon_coles, b, max_predictive_draws=200)
    p = r.predictions
    assert p.height == fixtures.height * 5
    assert ((p["prob_lo"] <= p["prob"]) & (p["prob"] <= p["prob_hi"])).all()
    assert ((p["prob_lo"] >= 0) & (p["prob_hi"] <= 1)).all()
    one = p.filter(pl.col("market") == "1X2").group_by("event_id").agg(pl.col("prob").sum())
    assert (one["prob"] - 1).abs().max() < 1e-9
    # short chains here, so only check the health flag is consistent with its thresholds
    limit = BayesResult.MAX_DIVERGENCE_RATE * b.draws * b.chains
    assert r.healthy == (r.max_rhat <= BayesResult.MAX_RHAT and r.divergences <= limit)
