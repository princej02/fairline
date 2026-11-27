import numpy as np
import pytest

from fairline.core.config import get_config
from fairline.edge.acca import Leg, Rates, build
from fairline.models.football.markets import one_x_two, over_under
from fairline.models.football.scores import score_matrix

S = get_config().settings.edge
R = Rates(1.8, 0.9, -0.08)
M = score_matrix(R.lambda_home, R.lambda_away, R.rho, 10)
P_HOME, P_OVER = one_x_two(M)["home"], over_under(M, 2.5)["over"]


def test_same_match_joint_is_exact_not_product():
    legs = [
        Leg(1, "1X2", "home", 1.75, P_HOME, rates=R),
        Leg(1, "OU", "over", 1.95, P_OVER, line=2.5, rates=R),
    ]
    q = build(legs, S)
    i, j = np.indices(M.shape)
    exact = float(M[(i > j) & (i + j > 2.5)].sum())
    assert q.p_joint == pytest.approx(exact)
    assert q.p_joint > q.p_naive  # home win and goals are positively correlated here
    assert q.combined_price is None and q.ev is None  # needs a bet-builder price
    assert any("bet builder" in w for w in q.warnings)
    q2 = build(legs, S, builder_prices={1: 2.9})
    assert q2.ev == pytest.approx(exact * 2.9 - 1)


def test_cross_match_is_product_with_compounded_margin_and_drag():
    legs = [
        Leg(1, "1X2", "home", 1.80, 0.60, overround=1.05),
        Leg(2, "1X2", "away", 2.60, 0.36, overround=1.04),  # EV -6.4%: drags
        Leg(3, "OU", "over", 1.90, 0.56, line=2.5, overround=1.06),
    ]
    q = build(legs, S)
    assert q.p_joint == pytest.approx(0.60 * 0.36 * 0.56)
    assert q.combined_price == pytest.approx(1.80 * 2.60 * 1.90)
    assert q.compounded_margin == pytest.approx(1.05 * 1.04 * 1.06 - 1)
    assert [v.drags for v in q.legs] == [False, True, False]
    assert q.ev == pytest.approx(q.p_joint * q.combined_price - 1)


def test_blocks_contradictions_ah_legs_and_long_legs():
    assert not build([Leg(1, "1X2", "home", 1.8, 0.5, rates=R), Leg(1, "1X2", "away", 4.0, 0.2, rates=R)], S).ok
    assert not build([Leg(1, "AH", "home", 1.9, 0.5, line=-0.25), Leg(2, "1X2", "home", 1.8, 0.6)], S).ok
    assert not build([Leg(1, "1X2", "away", 7.5, 0.15), Leg(2, "1X2", "home", 1.8, 0.6)], S).ok
    assert not build([Leg(1, "1X2", "home", 1.8, 0.6)], S).ok  # one leg is not an acca


def test_max_odds_applies_to_legs_not_combined_price():
    legs = [Leg(k, "1X2", "home", 2.0, 0.52) for k in range(3)]  # 8.00 combined, legs fine
    q = build(legs, S)
    assert q.ok and q.combined_price == pytest.approx(8.0)
    assert not any("acca_max_combined_odds" in w for w in q.warnings)


def test_same_team_in_two_matches_warns():
    legs = [
        Leg(1, "1X2", "home", 1.8, 0.6, home_id=10, away_id=11),
        Leg(2, "1X2", "away", 2.2, 0.5, home_id=12, away_id=10),
    ]
    assert any("team appears" in w for w in build(legs, S).warnings)
