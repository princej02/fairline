"""When to spend a credit. Pure functions, so the schedule can be tested and simulated offline.

One /odds call returns every upcoming fixture of a league, so the unit of spending is a league
poll. A league needs a poll when any of its fixtures has entered a window (T-24h, T-3h, ...) or
the closing window (T-20m) since that league was last polled. A poll for one kickoff slot
therefore also covers later fixtures whose windows have already opened.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from fairline.core.config import TheOddsApiSettings


@dataclass(frozen=True)
class Fixture:
    external_id: str
    commence: datetime


@dataclass
class PollNeed:
    sport: str
    kind: str  # "close" | "window"
    first_kickoff: datetime
    reasons: list[str] = field(default_factory=list)


def plan(
    fixtures: dict[str, list[Fixture]],
    last_poll: dict[str, datetime | None],
    now: datetime,
    s: TheOddsApiSettings,
) -> list[PollNeed]:
    """Leagues to poll now. Two rules keep one poll doing several jobs:
    - tolerance: a window opening at `start` is already covered by any poll since
      start - w * window_tolerance_share (T-24h by a poll from T-32h, T-3h by one from T-4h);
    - deferral: a window-only poll waits for a closing poll of that league when the closing poll
      lands no later than the window's opening + tolerance (measured from when the window
      opened, not from now: a window that opened yesterday is overdue and polls now).
    Closing windows get neither: a close must be a fresh price."""
    close_w = timedelta(minutes=s.close_window_minutes)
    needs: dict[str, PollNeed] = {}
    for sport, fx in fixtures.items():
        last = last_poll.get(sport)
        upcoming = [f for f in fx if f.commence > now]
        next_close = min((f.commence - close_w for f in upcoming if f.commence - close_w > now), default=None)
        closes, windows, deadline = [], [], None
        for f in upcoming:
            start = f.commence - close_w
            if now >= start and (last is None or last < start):
                closes.append(f"{f.external_id[:8]} close T-{s.close_window_minutes:g}m")
            for w in s.poll_windows_hours:
                start = f.commence - timedelta(hours=w)
                t = timedelta(hours=w * s.window_tolerance_share)
                if now >= start and (last is None or last < start - t):
                    windows.append(f"{f.external_id[:8]} T-{w:g}h")
                    # a window poll may wait for a close poll, but never past start + tolerance
                    deadline = start + t if deadline is None else min(deadline, start + t)
        if not closes and windows and next_close is not None and next_close <= deadline:
            continue  # the closing poll due shortly will cover these windows
        if closes or windows:
            first = min(f.commence for f in upcoming)
            needs[sport] = PollNeed(sport, "close" if closes else "window", first, closes + windows)
    return sorted(needs.values(), key=lambda n: (n.kind != "close", n.first_kickoff))


def simulate(
    fixtures: dict[str, list[Fixture]],
    start: datetime,
    end: datetime,
    s: TheOddsApiSettings,
    last_poll: dict[str, datetime | None] | None = None,
) -> list[tuple[datetime, str, str]]:
    """Every (time, sport, kind) poll the scheduler would make between start and end, ignoring
    the budget. Used to estimate monthly credit need from the free fixture list."""
    last = dict(last_poll or {})
    out = []
    t, step = start, timedelta(minutes=s.tick_minutes)
    while t <= end:
        for need in plan(fixtures, last, t, s):
            out.append((t, need.sport, need.kind))
            last[need.sport] = t
        t += step
    return out
