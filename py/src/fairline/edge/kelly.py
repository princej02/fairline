"""Kelly staking that handles pushes and half results, plus exposure caps.

The binary formula f* = (pO - 1) / (O - 1) assumes a non-win loses the whole stake. Asian
lines can push (stake back) or half-win / half-lose, so we maximise expected log-wealth over
all five settlement states instead:

    g(f) = sum_s P_s * log(1 + f * r_s),  r = {win: b, half_win: b/2, push: 0, half_loss: -1/2, loss: -1}

with b = O - 1. g is concave, so g'(f) = 0 has one root in [0, 1); for a binary bet it equals
the closed form exactly.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, replace

import numpy as np
from scipy.optimize import brentq

from fairline.models.football.markets import AsianOutcome


def binary_outcome(p: float) -> AsianOutcome:
    return AsianOutcome(win=p, half_win=0.0, push=0.0, half_loss=0.0, loss=1 - p)


def full_kelly(outcome: AsianOutcome, price: float) -> float:
    """Growth-optimal fraction of bankroll. 0 when EV <= 0."""
    b = price - 1
    if b <= 0:
        return 0.0
    P = np.array([outcome.win, outcome.half_win, outcome.push, outcome.half_loss, outcome.loss])
    r = np.array([b, b / 2, 0.0, -0.5, -1.0])
    keep = P > 0
    P, r = P[keep], r[keep]
    if (P * r).sum() <= 0:  # g'(0) = EV
        return 0.0
    if (r >= 0).all():  # cannot lose: degenerate, cap handles it
        return 1.0

    def dg(f: float) -> float:
        return float((P * r / (1 + f * r)).sum())

    f_max = min(1.0, 1 / -r.min()) * (1 - 1e-12)  # stay where 1 + f r > 0 for every state
    if dg(f_max) > 0:
        return f_max
    return brentq(dg, 0.0, f_max, xtol=1e-12)


@dataclass(frozen=True)
class Stake:
    key: str  # unique bet id (e.g. "event:market:selection:book")
    event_id: int
    slate: str  # bets open at the same time share a slate
    kelly: float  # full-Kelly fraction
    stake: float  # after fraction and caps
    capped_by: tuple[str, ...] = ()


def size(
    bets: list[tuple[str, int, str, float]],
    kelly_fraction: float,
    max_stake: float,
    max_match: float,
    max_slate: float,
    open_exposure: float = 0.0,
) -> list[Stake]:
    """Apply fractional Kelly, then per-bet, per-match and per-slate caps, in that order.

    bets: (key, event_id, slate, full_kelly_fraction).
    open_exposure: stakes already placed and unsettled, which use up the slate cap first.
    A binding match/slate cap scales every stake in its group by the same factor; no bet is
    dropped in favour of another because of list order.
    """
    out = []
    for key, ev, slate, k in bets:
        s = k * kelly_fraction
        caps = ()
        if s > max_stake:
            s, caps = max_stake, ("bet",)
        out.append(Stake(key, ev, slate, k, s, caps))

    def scale(key_of, limit: float, name: str) -> None:
        groups: dict = defaultdict(list)
        for i, st in enumerate(out):
            groups[key_of(st)].append(i)
        limit = max(limit, 0.0)
        for members in groups.values():
            total = sum(out[i].stake for i in members)
            if total > limit and total > 0:
                f = limit / total
                for i in members:
                    out[i] = replace(out[i], stake=out[i].stake * f, capped_by=out[i].capped_by + (name,))

    scale(lambda st: st.event_id, max_match, "match")
    scale(lambda st: st.slate, max_slate - open_exposure, "slate")
    return out
