"""Persist evaluated board candidates to `edge` (idempotent per prediction, book and decision time)."""

from __future__ import annotations

import psycopg

from fairline.edge.board import Board


def persist(conn: psycopg.Connection, board: Board, devig_method: str) -> int:
    books = dict(conn.execute("SELECT code, id FROM bookmaker").fetchall())
    rows = [
        (
            c.prediction_id, books[c.bookmaker], c.price, c.p_ref, devig_method, c.ev_model, c.stake_pct,
            c.decision_ts, c.odds_ts, c.effective_price, c.ref_book, c.ev_vs_ref, c.ev_vs_ref_conservative,
            c.ev_conservative, c.kelly_full, c.stake_pct, list(c.capped_by), c.flags, c.on_board,
        )
        for c in board.candidates
        if c.prediction_id is not None
    ]  # fmt: skip
    with conn.transaction(), conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO edge (prediction_id, bookmaker_id, price, fair_prob, devig_method, ev, kelly_frac,
              decision_ts, odds_ts, effective_price, ref_book, ev_vs_ref, ev_vs_ref_conservative,
              ev_conservative, kelly_full, stake_pct, capped_by, flags, on_board)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (prediction_id, bookmaker_id, decision_ts) DO UPDATE SET
              price = EXCLUDED.price, fair_prob = EXCLUDED.fair_prob, devig_method = EXCLUDED.devig_method,
              ev = EXCLUDED.ev, kelly_frac = EXCLUDED.kelly_frac, odds_ts = EXCLUDED.odds_ts,
              effective_price = EXCLUDED.effective_price, ref_book = EXCLUDED.ref_book,
              ev_vs_ref = EXCLUDED.ev_vs_ref, ev_vs_ref_conservative = EXCLUDED.ev_vs_ref_conservative,
              ev_conservative = EXCLUDED.ev_conservative, kelly_full = EXCLUDED.kelly_full,
              stake_pct = EXCLUDED.stake_pct, capped_by = EXCLUDED.capped_by, flags = EXCLUDED.flags,
              on_board = EXCLUDED.on_board, computed_at = now()
            """,
            rows,
        )
    return len(rows)
