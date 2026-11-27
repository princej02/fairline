"""Load board inputs from Postgres: model probabilities, odds and events, all cut at each
event's own decision time."""

from __future__ import annotations

import psycopg

from fairline.edge.board import EventInfo, ModelProb
from fairline.edge.devig import Quote

_DECISIONS = "WITH d AS (SELECT * FROM unnest(%s::bigint[], %s::timestamptz[]) AS d(event_id, decision_ts))"

_PROBS = (
    _DECISIONS
    + """
, latest AS (
  SELECT DISTINCT ON (p.event_id) p.event_id, p.as_of, p.model_version_id
  FROM prediction p JOIN d ON d.event_id = p.event_id
  JOIN model_version mv ON mv.id = p.model_version_id
  WHERE p.as_of <= d.decision_ts AND mv.name = %s AND mv.version LIKE %s || '/%%'
  ORDER BY p.event_id, p.as_of DESC
)
SELECT p.id, p.event_id, m.code, m.line::float, p.selection, p.prob, p.prob_lo
FROM prediction p
JOIN latest l ON l.event_id = p.event_id AND l.as_of = p.as_of AND l.model_version_id = p.model_version_id
JOIN market m ON m.id = p.market_id
"""
)

_QUOTES = (
    _DECISIONS
    + """
SELECT o.event_id, m.code, m.line::float, b.code, o.selection, o.price::float, o.ts
FROM odds_snapshot o JOIN d ON d.event_id = o.event_id
JOIN market m ON m.id = o.market_id JOIN bookmaker b ON b.id = o.bookmaker_id
WHERE o.ts <= d.decision_ts AND b.code = ANY(%s)
"""
)


def load_inputs(
    conn: psycopg.Connection,
    events: list[EventInfo],
    books: list[str],
    model_name: str,
    config_hash: str,
):
    ids = [e.event_id for e in events]
    ts = [e.decision_ts for e in events]
    probs = [
        ModelProb(
            event_id=r[1], market=r[2], line=r[3], selection=r[4], p=r[5], p_conservative=None, prediction_id=r[0]
        )  # fmt: skip
        for r in conn.execute(_PROBS, (ids, ts, model_name, config_hash))
    ]
    quotes: dict = {}
    for ev, market, line, book, sel, price, t in conn.execute(_QUOTES, (ids, ts, books)):
        quotes.setdefault((ev, market, line), []).append(Quote(sel, price, t, book))
    return probs, quotes


def matchday_events(
    conn: psycopg.Connection, competitions: list[str], day: str, offset_hours: float
) -> list[EventInfo]:
    """Events kicking off on a London calendar day; each decided `offset_hours` before kickoff."""
    rows = conn.execute(
        """
        SELECT e.id, e.starts_at, e.starts_at - make_interval(secs => %s), h.name || ' v ' || a.name
        FROM event e JOIN season s ON s.id = e.season_id JOIN competition c ON c.id = s.competition_id
        JOIN participant h ON h.id = e.home_id JOIN participant a ON a.id = e.away_id
        WHERE c.code = ANY(%s) AND (e.starts_at AT TIME ZONE 'Europe/London')::date = %s::date
        ORDER BY e.starts_at, e.id
        """,
        (offset_hours * 3600, competitions, day),
    ).fetchall()
    return [EventInfo(r[0], r[1], r[2], slate=day, label=r[3]) for r in rows]


def open_exposure_pct(conn: psycopg.Connection, bankroll: float) -> float:
    """Stake already placed and unsettled, as a fraction of bankroll."""
    total = conn.execute("SELECT coalesce(sum(stake), 0) FROM tracked_bet WHERE status = 'open'").fetchone()[0]
    return float(total) / bankroll if bankroll > 0 else 0.0
