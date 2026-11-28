"""Accumulator builder: joint probability, compounded margin and per-leg EV.

- Legs from different matches are treated as independent (joint = product). A team appearing
  in two legs (e.g. a cup tie and a league game the same week) gets a warning.
- Legs from the same match are correlated (Home + Over 2.5, Home + BTTS...). Their joint
  probability is computed exactly from that match's Dixon-Coles score matrix, never by
  multiplying marginals. Bookmakers don't price same-match legs as a normal acca (that's a
  "bet builder" with its own price), so such a group needs a bet-builder price from you
  before any EV is shown.
- Only binary markets (1X2, half-line O/U, BTTS) can be legs. Asian lines with pushes and
  half results are refused until AH accas get proper settlement handling.
- `max_odds` applies to each leg; the combined price has its own cap (acca_max_combined_odds).
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field

import numpy as np

from fairline.core.config import EdgeSettings
from fairline.edge.kelly import binary_outcome, full_kelly
from fairline.models.football.scores import score_matrix


@dataclass(frozen=True)
class Rates:
    lambda_home: float
    lambda_away: float
    rho: float
    max_goals: int = 10


@dataclass(frozen=True)
class Leg:
    event_id: int
    market: str  # '1X2' | 'OU' | 'BTTS'
    selection: str
    price: float
    p_model: float
    line: float | None = None
    bookmaker: str = ""
    overround: float | None = None  # sum(1/O) of this leg's full market at its bookmaker
    home_id: int | None = None
    away_id: int | None = None
    rates: Rates | None = None  # needed when another leg shares this match


def leg_mask(leg: Leg, g: int) -> np.ndarray:
    """Boolean grid of scorelines (home=i, away=j) on which this leg wins."""
    i, j = np.indices((g, g))
    m, s = leg.market, leg.selection
    if m == "1X2":
        return {"home": i > j, "draw": i == j, "away": i < j}[s]
    if m == "OU":
        if leg.line is None or (leg.line * 2) % 2 != 1:
            raise ValueError("O/U acca legs must be half lines like 2.5")
        return {"over": i + j > leg.line, "under": i + j < leg.line}[s]
    if m == "BTTS":
        both = (i >= 1) & (j >= 1)
        return {"yes": both, "no": ~both}[s]
    raise ValueError(f"market {m!r} cannot be an acca leg (binary markets only)")


@dataclass
class LegView:
    leg: Leg
    ev: float
    drags: bool  # this leg's own EV is negative


@dataclass
class AccaQuote:
    legs: list[LegView]
    p_joint: float | None  # exact; None if a same-match group lacks rates
    p_naive: float  # product of marginals (shown for comparison only)
    combined_price: float | None  # None if a same-match group has no bet-builder price
    ev: float | None
    kelly: float  # full Kelly on the combined ticket (binary)
    compounded_margin: float | None
    same_match_groups: dict[int, float] = field(default_factory=dict)  # event_id -> exact fair odds
    warnings: list[str] = field(default_factory=list)
    blocked: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.blocked


def build(legs: list[Leg], s: EdgeSettings, builder_prices: dict[int, float] | None = None) -> AccaQuote:
    """builder_prices: event_id -> the bookmaker's bet-builder price for that match's legs."""
    builder_prices = builder_prices or {}
    warnings: list[str] = []
    blocked: list[str] = []

    if not 2 <= len(legs) <= s.acca_max_legs:
        blocked.append(f"an acca needs 2 to {s.acca_max_legs} legs (got {len(legs)})")
    for lg in legs:
        try:
            leg_mask(lg, 11)
        except (ValueError, KeyError) as e:
            blocked.append(f"leg {lg.market} {lg.selection}: {e}")
        if lg.price > s.max_odds:
            blocked.append(f"leg {lg.market} {lg.selection} @ {lg.price} is above max_odds {s.max_odds}")
    if blocked:
        return AccaQuote([], None, 0.0, None, None, 0.0, None, warnings=warnings, blocked=blocked)

    views = [LegView(lg, lg.p_model * lg.price - 1, lg.p_model * lg.price - 1 < 0) for lg in legs]
    for v in views:
        if v.drags:
            warnings.append(
                f"leg {v.leg.market} {v.leg.selection} @ {v.leg.price} has EV {v.ev:+.1%} and drags the ticket"
            )

    by_event: dict[int, list[Leg]] = defaultdict(list)
    for lg in legs:
        by_event[lg.event_id].append(lg)

    team_events: dict[int, set[int]] = defaultdict(set)
    for lg in legs:
        for t in (lg.home_id, lg.away_id):
            if t is not None:
                team_events[t].add(lg.event_id)
    if any(len(evs) > 1 for evs in team_events.values()):
        warnings.append("a team appears in two different matches; those legs may be correlated")

    p_joint: float | None = 1.0
    price: float | None = 1.0
    same: dict[int, float] = {}
    for ev_id, group in by_event.items():
        if len(group) == 1:
            p_joint = None if p_joint is None else p_joint * group[0].p_model
            price = None if price is None else price * group[0].price
            continue
        sels = [(g.market, g.selection, g.line) for g in group]
        if len(set(sels)) != len(sels):
            blocked.append(f"match {ev_id}: duplicate leg")
        r = next((g.rates for g in group if g.rates is not None), None)
        if r is None:
            warnings.append(f"match {ev_id}: same-match legs but no model rates; joint probability unknown")
            p_joint = None
        else:
            m = score_matrix(r.lambda_home, r.lambda_away, r.rho, r.max_goals)
            mask = np.ones(m.shape, dtype=bool)
            for g in group:
                mask &= leg_mask(g, m.shape[0])
            pj = float(m[mask].sum())
            if pj <= 0:
                blocked.append(f"match {ev_id}: legs are mutually exclusive")
            else:
                same[ev_id] = 1 / pj
            p_joint = None if p_joint is None else p_joint * pj
        warnings.append(
            f"match {ev_id}: same-match legs are correlated; multiplying marginals would be wrong. "
            "Bookmakers price this as a bet builder"
            + ("" if ev_id in builder_prices else ", so enter its bet-builder price to see EV")
        )
        price = None if (price is None or ev_id not in builder_prices) else price * builder_prices[ev_id]

    p_naive = math.prod(lg.p_model for lg in legs)
    if price is not None and price > s.acca_max_combined_odds:
        warnings.append(f"combined price {price:.2f} is above acca_max_combined_odds {s.acca_max_combined_odds}")
    margins = [lg.overround for lg in legs]
    compounded = None if any(m is None for m in margins) else math.prod(margins) - 1
    ev = None if (p_joint is None or price is None) else p_joint * price - 1
    kelly = 0.0 if ev is None or blocked else full_kelly(binary_outcome(p_joint), price)
    return AccaQuote(views, p_joint, p_naive, price, ev, kelly, compounded, same, warnings, blocked)
