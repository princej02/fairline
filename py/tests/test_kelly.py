import numpy as np
import pytest

from fairline.edge.kelly import binary_outcome, full_kelly, size
from fairline.models.football.markets import AsianOutcome, asian_handicap
from fairline.models.football.scores import score_matrix


def test_binary_matches_closed_form():
    for p, o in [(0.55, 2.0), (0.30, 4.0), (0.70, 1.6)]:
        assert full_kelly(binary_outcome(p), o) == pytest.approx((p * o - 1) / (o - 1), abs=1e-9)


def test_no_edge_no_stake():
    assert full_kelly(binary_outcome(0.5), 1.9) == 0
    assert full_kelly(binary_outcome(0.5), 1.0) == 0


def test_push_kelly_matches_analytic_draw_no_bet():
    pw, pp, pl, o = 0.45, 0.27, 0.28, 1.95
    b = o - 1
    expected = (pw * b - pl) / (b * (pw + pl))
    assert full_kelly(AsianOutcome(pw, 0, pp, 0, pl), o) == pytest.approx(expected, abs=1e-9)


def test_push_and_half_loss_stake_more_than_binary_with_same_win_prob():
    """Treating a push as a loss understates the edge, so binary Kelly under-stakes these lines."""
    m = score_matrix(1.7, 1.0, -0.08, 10)
    for line in (0.0, -0.25):
        o = asian_handicap(m, line, "home")
        price = o.fair_odds * 1.06
        assert full_kelly(o, price) > full_kelly(binary_outcome(o.win), price)


def test_quarter_line_kelly_is_growth_optimal():
    o = asian_handicap(score_matrix(1.6, 1.1, -0.08, 10), -0.75, "home")
    price = o.fair_odds * 1.08
    f = full_kelly(o, price)
    b = price - 1
    P = np.array([o.win, o.half_win, o.push, o.half_loss, o.loss])
    r = np.array([b, b / 2, 0, -0.5, -1])

    def g(x):
        return float((P * np.log1p(x * r)).sum())

    assert 0 < f < 1 and g(f) >= g(f * 0.98) and g(f) >= g(min(f * 1.02, 0.999))


def test_caps_bet_then_match_then_slate_proportionally():
    bets = [("a", 1, "sat", 0.20), ("b", 1, "sat", 0.04), ("c", 2, "sat", 0.06), ("d", 3, "sat", 0.06)]
    out = {s.key: s for s in size(bets, 0.25, 0.02, 0.02, 0.04)}
    # per bet: a 0.05 -> 0.02, b 0.01, c 0.015, d 0.015
    # match 1: 0.03 > 0.02 -> a, b scaled by 2/3 -> 0.01333, 0.00667
    assert out["a"].stake / out["b"].stake == pytest.approx(2.0)
    # slate: 0.02 + 0.015 + 0.015 = 0.05 > 0.04 -> everyone x0.8, nobody dropped
    assert sum(s.stake for s in out.values()) == pytest.approx(0.04)
    assert all(s.stake > 0 for s in out.values())
    assert out["c"].stake == pytest.approx(0.015 * 0.8)
    assert out["a"].capped_by == ("bet", "match", "slate")


def test_open_exposure_uses_up_slate_cap():
    out = size([("x", 1, "sat", 0.08)], 0.25, 0.02, 0.02, 0.05, open_exposure=0.04)
    assert out[0].stake == pytest.approx(0.01)
    out = size([("x", 1, "sat", 0.08)], 0.25, 0.02, 0.02, 0.05, open_exposure=0.06)
    assert out[0].stake == 0
