"""Credit budget for The Odds API free tier.

Checked before every paid call, in this order:
1. Hard floor: the API's own `x-requests-remaining` minus this call must stay >= reserve_credits.
2. Monthly cap: credits spent this calendar month (our ledger) + this call <= budget - reserve.
   Applies to every poll, closing polls included.
3. Window polls only (T-24h, T-3h); closing polls are what CLV needs, so they skip these two:
   a. Burst-aware pace: spend may run up to `burst_days` of allowance ahead of an even monthly
      line. Fixtures cluster on weekends, and a month that opens on a full Saturday slate must
      not be throttled on day one. Under strictly linear pacing it would be.
   b. Close reserve: after this poll there must still be room for the closing polls left this
      month, projected from the last 7 days' close spend per day x days remaining.
"""

from __future__ import annotations

import calendar
import math
from dataclasses import dataclass
from datetime import UTC, datetime

from fairline.core.config import TheOddsApiSettings


@dataclass(frozen=True)
class Ledger:
    spent_month: int  # credits we spent this calendar month
    remaining: int | None  # the API's last reported balance
    close_7d: int = 0  # credits spent on closing polls in the last 7 days


def call_cost(markets: str, n_books: int) -> int:
    """markets x regions, where each group of up to 10 bookmakers bills as one region."""
    n_markets = len([m for m in markets.split(",") if m.strip()])
    return n_markets * max(1, math.ceil(n_books / 10))


def month_start(now: datetime) -> datetime:
    return now.astimezone(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def days_in_month(now: datetime) -> int:
    n = now.astimezone(UTC)
    return calendar.monthrange(n.year, n.month)[1]


def month_elapsed(now: datetime) -> float:
    n = now.astimezone(UTC)
    return min(1.0, (n - month_start(n)).total_seconds() / (days_in_month(n) * 86400))


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str
    warn: bool = False  # past warn_at of the monthly budget


def decide(kind: str, cost: int, ledger: Ledger, now: datetime, s: TheOddsApiSettings) -> Decision:
    budget, spent = s.monthly_credit_budget, ledger.spent_month
    after = spent + cost
    warn = after >= s.warn_at * budget
    if ledger.remaining is not None and ledger.remaining - cost < s.reserve_credits:
        return Decision(
            False, f"API reports {ledger.remaining} credits left; keeping {s.reserve_credits} in reserve", warn
        )
    if after > budget - s.reserve_credits:
        return Decision(False, f"monthly budget: {spent}+{cost} > {budget}-{s.reserve_credits}", warn)
    if kind == "close":
        return Decision(True, "ok", warn)
    days = days_in_month(now)
    pace = budget * min(1.0, month_elapsed(now) + s.burst_days / days)
    if after > pace:
        return Decision(
            False, f"ahead of pace ({after} > {pace:.0f} incl. {s.burst_days:g}-day burst); close polls only", warn
        )
    days_left = days * (1 - month_elapsed(now))
    close_need = ledger.close_7d / 7 * days_left
    if after + close_need > budget - s.reserve_credits:
        return Decision(
            False, f"keeping ~{close_need:.0f} credits for this month's remaining closing polls; close polls only", warn
        )
    return Decision(True, "ok", warn)
