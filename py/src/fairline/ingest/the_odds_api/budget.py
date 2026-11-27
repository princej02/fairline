"""Credit budget for The Odds API free tier.

Three rules, checked before every paid call (cheapest-to-lose first):
1. Hard floor: the API's own `x-requests-remaining` minus this call must stay >= reserve_credits.
2. Monthly cap: credits we spent this calendar month (our ledger) + this call <= budget - reserve.
3. Pacing: a non-closing poll is refused when it would put us more than `pace_slack` ahead of an
   even spend across the month. Closing polls (the ones CLV depends on) are exempt from pacing.
"""

from __future__ import annotations

import calendar
import math
from dataclasses import dataclass
from datetime import UTC, datetime

from fairline.core.config import TheOddsApiSettings


def call_cost(markets: str, n_books: int) -> int:
    """markets x regions, where each group of up to 10 bookmakers bills as one region."""
    n_markets = len([m for m in markets.split(",") if m.strip()])
    return n_markets * max(1, math.ceil(n_books / 10))


def month_start(now: datetime) -> datetime:
    return now.astimezone(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def month_elapsed(now: datetime) -> float:
    n = now.astimezone(UTC)
    days = calendar.monthrange(n.year, n.month)[1]
    return min(1.0, (n - month_start(n)).total_seconds() / (days * 86400))


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str
    warn: bool = False  # past warn_at of the monthly budget


def decide(kind: str, cost: int, spent: int, remaining: int | None, now: datetime, s: TheOddsApiSettings) -> Decision:
    after = spent + cost
    warn = after >= s.warn_at * s.monthly_credit_budget
    if remaining is not None and remaining - cost < s.reserve_credits:
        return Decision(False, f"API reports {remaining} credits left; keeping {s.reserve_credits} in reserve", warn)
    if after > s.monthly_credit_budget - s.reserve_credits:
        return Decision(False, f"monthly budget: {spent}+{cost} > {s.monthly_credit_budget}-{s.reserve_credits}", warn)
    pace = s.monthly_credit_budget * (month_elapsed(now) + s.pace_slack)
    if kind != "close" and after > pace:
        return Decision(
            False, f"ahead of pace ({after} > {pace:.0f} by this point in the month); close polls only", warn
        )
    return Decision(True, "ok", warn)
