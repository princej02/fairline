import numpy as np
import pytest

from fairline.models.football.markets import (
    asian,
    asian_handicap,
    asian_total,
    btts,
    margin_distribution,
    one_x_two,
    over_under,
    total_distribution,
)
from fairline.models.football.scores import score_matrix

M = score_matrix(1.6, 1.1, -0.08, 10)
VALUES, PROBS = margin_distribution(M)


def p_margin(cond):
    return float(PROBS[cond(VALUES)].sum())


def test_basic_markets_sum_to_one():
    assert sum(one_x_two(M).values()) == pytest.approx(1)
    assert sum(over_under(M, 2.5).values()) == pytest.approx(1)
    assert sum(btts(M).values()) == pytest.approx(1)
    assert total_distribution(M)[1].sum() == pytest.approx(1)


def test_half_line_equals_plain_probability():
    o = asian_handicap(M, -0.5, "home")
    assert o.win == pytest.approx(one_x_two(M)["home"])
    assert o.push == o.half_win == o.half_loss == 0
    assert o.fair_odds == pytest.approx(1 / one_x_two(M)["home"])


def test_draw_no_bet_pushes_on_draw():
    o = asian_handicap(M, 0.0, "home")
    x = one_x_two(M)
    assert (o.win, o.push, o.loss) == pytest.approx((x["home"], x["draw"], x["away"]))
    assert o.effective_prob == pytest.approx(x["home"] / (x["home"] + x["away"]))


def test_minus_quarter_is_half_loss_on_draw():
    o = asian_handicap(M, -0.25, "home")
    assert o.win == pytest.approx(p_margin(lambda v: v > 0))
    assert o.half_loss == pytest.approx(p_margin(lambda v: v == 0))
    assert o.loss == pytest.approx(p_margin(lambda v: v < 0))
    assert o.half_win == 0 and o.push == 0


def test_minus_three_quarters_half_wins_by_one():
    o = asian_handicap(M, -0.75, "home")
    assert o.win == pytest.approx(p_margin(lambda v: v >= 2))
    assert o.half_win == pytest.approx(p_margin(lambda v: v == 1))
    assert o.loss == pytest.approx(p_margin(lambda v: v <= 0))


def test_plus_quarter_away_half_wins_on_draw():
    o = asian_handicap(M, 0.25, "away")
    assert o.win == pytest.approx(p_margin(lambda v: v < 0))
    assert o.half_win == pytest.approx(p_margin(lambda v: v == 0))
    assert o.loss == pytest.approx(p_margin(lambda v: v > 0))


def test_quarter_line_ev_is_average_of_split_stakes():
    price = 1.95
    q = asian_handicap(M, -0.25, "home").ev(price)
    a = asian_handicap(M, 0.0, "home").ev(price)
    b = asian_handicap(M, -0.5, "home").ev(price)
    assert q == pytest.approx((a + b) / 2)


def test_fair_odds_gives_zero_ev_on_every_line_type():
    for line in (-1.75, -1.0, -0.75, -0.25, 0.0, 0.25, 0.5, 1.25):
        for side in ("home", "away"):
            o = asian_handicap(M, line, side)
            assert o.ev(o.fair_odds) == pytest.approx(0, abs=1e-12)
    for line in (1.75, 2.0, 2.25, 2.5, 3.0, 3.25):
        for side in ("over", "under"):
            o = asian_total(M, line, side)
            assert o.ev(o.fair_odds) == pytest.approx(0, abs=1e-12)


def test_asian_total_quarter():
    v, p = total_distribution(M)
    o = asian_total(M, 2.25, "over")
    assert o.win == pytest.approx(p[v >= 3].sum())
    assert o.half_loss == pytest.approx(p[v == 2].sum())
    assert o.loss == pytest.approx(p[v <= 1].sum())
    u = asian_total(M, 2.25, "under")
    assert u.win == pytest.approx(p[v <= 1].sum()) and u.half_win == pytest.approx(p[v == 2].sum())


def test_opposite_sides_complement():
    for line in (-0.75, -0.25, 0.0, 0.5):
        h = asian_handicap(M, line, "home")
        a = asian_handicap(M, -line, "away")
        assert h.win == pytest.approx(a.loss) and h.half_win == pytest.approx(a.half_loss)
        assert h.push == pytest.approx(a.push)


def test_rejects_non_quarter_lines():
    with pytest.raises(ValueError):
        asian(VALUES, PROBS, 0.3)
    with pytest.raises(ValueError):
        over_under(M, 2.25)


def test_outcomes_partition_probability():
    for line in np.arange(-2, 2.25, 0.25):
        o = asian_handicap(M, float(line), "home")
        assert o.win + o.half_win + o.push + o.half_loss + o.loss == pytest.approx(1)
