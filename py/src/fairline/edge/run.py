"""Build a value board for one historical matchday (each match decided `offset_hours` before kickoff)."""

from __future__ import annotations

import psycopg

from fairline.core.config import Config
from fairline.edge.board import Board, build_board
from fairline.edge.data import load_inputs, matchday_events
from fairline.models.football.walkforward import MODEL_NAME, config_hash


def run_board(conn: psycopg.Connection, config: Config, pool: str, day: str, offset_hours: float = 2.0) -> Board:
    st = config.settings
    s = st.edge
    comps = st.models.dixon_coles.pools[pool]
    events = matchday_events(conn, comps, day, offset_hours)
    books = sorted(set(s.reference_books) | set(s.bettable_books))
    probs, quotes = load_inputs(conn, events, books, MODEL_NAME, config_hash(st.models.dixon_coles))
    exchanges = {b.code for b in config.bookmakers.values() if b.kind == "exchange"}
    return build_board(events, probs, quotes, s, exchanges)
