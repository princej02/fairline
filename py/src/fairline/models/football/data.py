"""Load matches for model fitting from Postgres."""

from __future__ import annotations

import polars as pl
import psycopg

_SQL = """
SELECT e.id AS event_id, e.starts_at, c.code AS competition, s.label AS season,
       e.home_id, e.away_id, r.home_goals, r.away_goals
FROM event e
JOIN season s ON s.id = e.season_id
JOIN competition c ON c.id = s.competition_id
LEFT JOIN football_result r ON r.event_id = e.id
WHERE c.code = ANY(%s) AND c.enabled AND e.status IN ('scheduled', 'finished')
ORDER BY e.starts_at, e.id
"""

SCHEMA = {
    "event_id": pl.Int64,
    "starts_at": pl.Datetime("us", "UTC"),
    "competition": pl.String,
    "season": pl.String,
    "home_id": pl.Int64,
    "away_id": pl.Int64,
    "home_goals": pl.Int16,
    "away_goals": pl.Int16,
}


def load_matches(conn: psycopg.Connection, competitions: list[str]) -> pl.DataFrame:
    rows = conn.execute(_SQL, (competitions,)).fetchall()
    return pl.DataFrame(rows, schema=SCHEMA, orient="row")
