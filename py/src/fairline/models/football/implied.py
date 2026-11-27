"""Market-implied score matrix: price O/U 2.5 from Pinnacle's 1X2 + its main totals line.

Pinnacle quotes one main totals line per match (often 2.75, 3.0, 3.25...), so a quoted 2.5 is
usually missing. A 1-D Poisson fit on total goals would ignore two things that move the 2.5
boundary: home/away goal asymmetry (a heavy favourite skews the score distribution) and the
Dixon-Coles tau adjustment on 0-0, 1-0, 0-1 and 1-1. So we fit the same Dixon-Coles matrix the
model uses:

  outer: find total goals L = lambda_home + lambda_away such that the matrix's effective probability
         of Over on the quoted line (Asian settlement: pushes and half results for .0/.25/.75)
         equals Pinnacle's devigged Over;
  inner: for each L, split it so the matrix's P(home) / (P(home) + P(away)) equals Pinnacle's
         devigged ratio;
  rho:   the league's latest fitted Dixon-Coles rho.

Both solves are monotone 1-D root finds (brentq). The draw probability is not targeted, so the
gap between the matrix's draw and Pinnacle's devigged draw is kept as a fit diagnostic.
"""

from __future__ import annotations

from dataclasses import dataclass

from scipy.optimize import brentq

from fairline.models.football.markets import asian_total, one_x_two, over_under
from fairline.models.football.scores import score_matrix


@dataclass(frozen=True)
class ImpliedTotals:
    lambda_home: float
    lambda_away: float
    rho: float
    p_over: float  # at target_line
    p_under: float
    draw_fit_error: float  # matrix draw - market draw


class NoFit(ValueError):
    pass


def implied_totals(
    p1x2: dict[str, float],
    line: float,
    q_over: float,
    rho: float,
    target_line: float = 2.5,
    max_goals: int = 10,
) -> ImpliedTotals:
    """p1x2: devigged home/draw/away. q_over: devigged effective probability of Over `line`."""
    ph, pa = p1x2["home"], p1x2["away"]
    if not (0 < q_over < 1) or ph <= 0 or pa <= 0:
        raise NoFit("probabilities out of range")
    ratio = ph / (ph + pa)

    def matrix(total: float):
        def gap(s: float) -> float:
            p = one_x_two(score_matrix(s * total, (1 - s) * total, rho, max_goals))
            return p["home"] / (p["home"] + p["away"]) - ratio

        lo, hi = 0.02, 0.98
        if gap(lo) > 0 or gap(hi) < 0:
            raise NoFit(f"1X2 ratio {ratio:.3f} unreachable at total {total:.2f}")
        s = brentq(gap, lo, hi, xtol=1e-7)
        return score_matrix(s * total, (1 - s) * total, rho, max_goals), s

    def over_gap(total: float) -> float:
        return asian_total(matrix(total)[0], line, "over").effective_prob - q_over

    lo, hi = 0.6, 6.5
    try:
        if over_gap(lo) > 0 or over_gap(hi) < 0:
            raise NoFit(f"Over {line} at {q_over:.3f} unreachable")
        total = brentq(over_gap, lo, hi, xtol=1e-7)
    except ValueError as e:  # NoFit from the inner solve propagates as-is
        raise NoFit(str(e)) from None
    m, s = matrix(total)
    ou = over_under(m, target_line)
    return ImpliedTotals(s * total, (1 - s) * total, rho, ou["over"], ou["under"], one_x_two(m)["draw"] - p1x2["draw"])
