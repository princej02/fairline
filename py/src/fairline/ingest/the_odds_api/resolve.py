"""The Odds API binding of the shared resolver (fairline.ingest.resolve)."""

from __future__ import annotations

from datetime import datetime

import psycopg

from fairline.core.config import Competition
from fairline.ingest import resolve as _r
from fairline.ingest.resolve import TeamResolution, match_names, normalise

PROVIDER = "the_odds_api"


def resolve_teams(
    conn: psycopg.Connection, comp: Competition, sport_id: int, names: list[str], now: datetime
) -> TeamResolution:
    return _r.resolve_teams(conn, PROVIDER, comp, sport_id, names, now)


def resolve_event(
    conn, comp: Competition, comp_id: int, external_id: str, home_id: int, away_id: int, commence: datetime
):
    return _r.resolve_event(conn, PROVIDER, comp, comp_id, external_id, home_id, away_id, commence)


__all__ = ["PROVIDER", "TeamResolution", "match_names", "normalise", "resolve_event", "resolve_teams"]
