"""Derive market probabilities from a score matrix.

Asian lines (handicaps and totals) are settled the way bookmakers settle them:
- half lines (-0.5, 2.5): win or lose
- whole lines (0, -1, 3): win, push (stake back) or lose
- quarter lines (-0.25, +0.75, 2.25): the stake is split in half across the two adjacent lines,
  e.g. -0.25 = half on 0 and half on -0.5. Outcomes can then be a half win or half loss.

So a quarter line has no single "win probability". Use `AsianOutcome.ev(price)` for expected
return, and `fair_odds` / `effective_prob` to compare with a devigged two-way price.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


def one_x_two(m: np.ndarray) -> dict[str, float]:
    return {
        "home": float(np.tril(m, -1).sum()),
        "draw": float(np.trace(m)),
        "away": float(np.triu(m, 1).sum()),
    }


def btts(m: np.ndarray) -> dict[str, float]:
    yes = float(m[1:, 1:].sum())
    return {"yes": yes, "no": 1 - yes}


def margin_distribution(m: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Values and probabilities of home goals minus away goals."""
    g = m.shape[0]
    i, j = np.indices(m.shape)
    diff = (i - j).ravel()
    values = np.arange(-(g - 1), g)
    return values, np.bincount(diff + (g - 1), m.ravel(), len(values))


def total_distribution(m: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    g = m.shape[0]
    i, j = np.indices(m.shape)
    values = np.arange(2 * g - 1)
    return values, np.bincount((i + j).ravel(), m.ravel(), len(values))


@dataclass(frozen=True)
class AsianOutcome:
    win: float
    half_win: float
    push: float
    half_loss: float
    loss: float

    def ev(self, price: float) -> float:
        """Expected profit per unit stake at decimal `price`."""
        b = price - 1
        return self.win * b + self.half_win * b / 2 - self.half_loss / 2 - self.loss

    @property
    def fair_odds(self) -> float:
        """Decimal price at which EV = 0."""
        up = self.win + self.half_win / 2
        down = self.loss + self.half_loss / 2
        return float("inf") if up == 0 else 1 + down / up

    @property
    def effective_prob(self) -> float:
        """1 / fair_odds: the probability a devigged two-way price for this line should match."""
        up = self.win + self.half_win / 2
        down = self.loss + self.half_loss / 2
        return up / (up + down) if up + down > 0 else 0.0


def _quarters(offset: float) -> int:
    q = offset * 4
    if abs(q - round(q)) > 1e-9:
        raise ValueError(f"line {offset} is not a multiple of 0.25")
    return round(q)


def _settle_single(values: np.ndarray, probs: np.ndarray, offset: float) -> np.ndarray:
    """Per-value result for one half/whole line: +1 win, 0 push, -1 loss."""
    r = values + offset
    return np.where(r > 1e-9, 1.0, np.where(r < -1e-9, -1.0, 0.0))


def asian(values: np.ndarray, probs: np.ndarray, offset: float) -> AsianOutcome:
    """Settle a bet whose result score is `values + offset` (> 0 wins)."""
    q = _quarters(offset)
    if q % 2:  # quarter line -> split stake over the two neighbouring lines
        score = (_settle_single(values, probs, (q - 1) / 4) + _settle_single(values, probs, (q + 1) / 4)) / 2
    else:
        score = _settle_single(values, probs, offset)

    def p(v: float) -> float:
        return float(probs[np.isclose(score, v)].sum())

    return AsianOutcome(win=p(1), half_win=p(0.5), push=p(0), half_loss=p(-0.5), loss=p(-1))


def asian_handicap(m: np.ndarray, line: float, side: str = "home") -> AsianOutcome:
    """`line` is from the backed side's perspective, e.g. home -0.75 or away +0.25."""
    values, probs = margin_distribution(m)
    if side == "home":
        return asian(values, probs, line)
    if side == "away":
        return asian(-values, probs, line)
    raise ValueError("side must be 'home' or 'away'")


def asian_total(m: np.ndarray, line: float, side: str = "over") -> AsianOutcome:
    values, probs = total_distribution(m)
    if side == "over":
        return asian(values, probs, -line)
    if side == "under":
        return asian(-values, probs, line)
    raise ValueError("side must be 'over' or 'under'")


def over_under(m: np.ndarray, line: float = 2.5) -> dict[str, float]:
    """Plain probabilities for a half line (no pushes possible)."""
    if _quarters(line) % 4 != 2:
        raise ValueError("over_under() is for half lines like 2.5; use asian_total() for others")
    o = asian_total(m, line, "over").win
    return {"over": o, "under": 1 - o}
