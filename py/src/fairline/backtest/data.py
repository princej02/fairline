"""Bulk loaders for backtests: one pool-season at a time."""

from __future__ import annotations

from collections import defaultdict

import psycopg

from fairline.core.config import EdgeSettings
from fairline.edge.board import EventInfo
from fairline.edge.devig import IncompleteMarket, Quote, Snapshot, devig


def season_events(conn: psycopg.Connection, comps: list[str], season: str, offset_hours: float):
    """Finished matches with results. Decision = kickoff - offset; slate = London matchday."""
    rows = conn.execute(
        """
        SELECT e.id, e.starts_at, e.starts_at - make_interval(secs => %s),
               to_char(e.starts_at AT TIME ZONE 'Europe/London', 'YYYY-MM-DD'), c.code,
               h.name || ' v ' || a.name, r.home_goals, r.away_goals
        FROM event e JOIN season s ON s.id = e.season_id JOIN competition c ON c.id = s.competition_id
        JOIN participant h ON h.id = e.home_id JOIN participant a ON a.id = e.away_id
        JOIN football_result r ON r.event_id = e.id
        WHERE c.code = ANY(%s) AND s.label = %s AND e.status = 'finished'
        ORDER BY e.starts_at, e.id
        """,
        (offset_hours * 3600, comps, season),
    ).fetchall()
    events = [EventInfo(r[0], r[1], r[2], slate=r[3], label=r[5]) for r in rows]
    meta = {r[0]: {"competition": r[4], "season": season, "home_goals": r[6], "away_goals": r[7]} for r in rows}
    return events, meta


def closing(conn: psycopg.Connection, event_ids: list[int], s: EdgeSettings):
    """(1) Devigged closing reference probability per (event, market, line, selection), first
    valid reference book in priority order; (2) closing price per bettable book."""
    rows = conn.execute(
        """
        SELECT o.event_id, m.code, m.line::float, b.code, o.selection, o.price::float, o.ts
        FROM odds_snapshot o JOIN market m ON m.id = o.market_id JOIN bookmaker b ON b.id = o.bookmaker_id
        WHERE o.is_closing AND o.event_id = ANY(%s) AND b.code = ANY(%s)
        """,
        (event_ids, sorted(set(s.reference_books) | set(s.bettable_books))),
    ).fetchall()
    grouped: dict = defaultdict(list)
    close_price: dict = {}
    for ev, market, line, book, sel, price, ts in rows:
        grouped[(ev, market, line, book)].append(Quote(sel, price, ts, book))
        close_price[(ev, market, line, sel, book)] = price
    p_close: dict = {}
    for (ev, market, line, book), qs in grouped.items():
        if book not in s.reference_books:
            continue
        try:
            fp = devig(Snapshot.validate(market, qs), s.devig_method)
        except IncompleteMarket:
            continue
        if fp.status != "ok":
            continue
        rank = s.reference_books.index(book)
        for sel, p in fp.probs.items():
            k = (ev, market, line, sel)
            if k not in p_close or rank < p_close[k][1]:
                p_close[k] = (p, rank, book)
    return {k: v[0] for k, v in p_close.items()}, close_price


def outcome(market: str, selection: str, line: float | None, hg: int, ag: int) -> bool:
    if market == "1X2":
        return {"home": hg > ag, "draw": hg == ag, "away": hg < ag}[selection]
    if market == "OU":
        return {"over": hg + ag > line, "under": hg + ag < line}[selection]
    raise ValueError(market)
