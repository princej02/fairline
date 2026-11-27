"""Bet tracker: the live test of whether historical soft-book CLV survives execution.

Each leg records two checks:
- execution_edge = effective_price * ref_fair_prob - 1, using the devigged sharp reference at
  the moment the bet was placed. Negative means the "edge" had already gone, or the soft
  price was stale and the sharp line had moved.
- clv = effective_price * close_fair_prob - 1, using the devigged sharp *closing* price. This
  is the real test: sustained positive CLV is the best available evidence of an edge.
Fairline never places bets; these are bets you placed yourself and logged.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime

import numpy as np
import psycopg

from fairline.backtest import metrics as M
from fairline.backtest.data import outcome
from fairline.core.config import Config
from fairline.edge.devig import IncompleteMarket, Quote, Snapshot, devig, exchange_effective_odds


@dataclass
class LegIn:
    event_id: int
    market: str
    selection: str
    price: float
    line: float | None = None
    p_model: float | None = None


def _market_id(conn, market: str, line: float | None) -> int:
    row = conn.execute(
        "SELECT m.id FROM market m JOIN sport s ON s.id = m.sport_id "
        "WHERE s.code = 'football' AND m.code = %s AND m.line IS NOT DISTINCT FROM %s",
        (market, line),
    ).fetchone()
    if row is None:
        raise ValueError(f"unknown market {market} {line}")
    return row[0]


def reference_at(conn, config: Config, event_id: int, market: str, line: float | None, at: datetime, closing: bool):
    """Devigged reference probabilities from the first reference book with a valid snapshot:
    the latest one at or before `at`, or the closing one. Returns (book, probs, ts) or None."""
    s = config.settings.edge
    rows = conn.execute(
        f"""
        SELECT b.code, o.selection, o.price::float, o.ts FROM odds_snapshot o
        JOIN bookmaker b ON b.id = o.bookmaker_id JOIN market m ON m.id = o.market_id
        WHERE o.event_id = %s AND m.code = %s AND m.line IS NOT DISTINCT FROM %s AND b.code = ANY(%s)
          AND {"o.is_closing" if closing else "o.ts <= %s"}
        """,
        (event_id, market, line, s.reference_books) + (() if closing else (at,)),
    ).fetchall()
    by: dict = defaultdict(list)
    for book, sel, price, ts in rows:
        by[(book, ts)].append(Quote(sel, price, ts, book))
    for book in s.reference_books:
        for ts in sorted((t for (b, t) in by if b == book), reverse=True):
            try:
                fp = devig(Snapshot.validate(market, by[(book, ts)]), s.devig_method)
            except IncompleteMarket:
                continue
            if fp.status == "ok":
                return book, fp.probs, ts
    return None


def log_bet(
    conn: psycopg.Connection,
    config: Config,
    legs: list[LegIn],
    bookmaker: str,
    stake: float,
    placed_at: datetime,
    strategy: str | None = None,
    source: str = "manual",
    suggested_stake_pct: float | None = None,
    notes: str | None = None,
) -> int:
    if not legs:
        raise ValueError("a bet needs at least one leg")
    if bookmaker not in config.bookmakers:
        raise ValueError(f"unknown bookmaker {bookmaker}")
    exchange = config.bookmakers[bookmaker].kind == "exchange"
    commission = config.settings.edge.betfair_commission
    with conn.transaction():
        book_id = conn.execute("SELECT id FROM bookmaker WHERE code = %s", (bookmaker,)).fetchone()[0]
        for lg in legs:
            ko = conn.execute("SELECT starts_at FROM event WHERE id = %s", (lg.event_id,)).fetchone()
            if ko is None:
                raise ValueError(f"unknown event {lg.event_id}")
            if placed_at >= ko[0]:
                raise ValueError(f"event {lg.event_id} has already kicked off; only pre-match bets are tracked")
        price = math.prod(lg.price for lg in legs)
        bet_id = conn.execute(
            """
            INSERT INTO tracked_bet (placed_at, bookmaker_id, stake, price, status, notes, strategy, source,
              suggested_stake_pct, bankroll)
            VALUES (%s, %s, %s, %s, 'open', %s, %s, %s, %s, %s) RETURNING id
            """,
            (
                placed_at,
                book_id,
                stake,
                price,
                notes,
                strategy,
                source,
                suggested_stake_pct,
                config.settings.tracker.bankroll,
            ),
        ).fetchone()[0]
        for n, lg in enumerate(legs, 1):
            eff = exchange_effective_odds(lg.price, commission) if exchange else lg.price
            ref = reference_at(conn, config, lg.event_id, lg.market, lg.line, placed_at, closing=False)
            ref_book, ref_p, ref_ts = (ref[0], ref[1].get(lg.selection), ref[2]) if ref else (None, None, None)
            conn.execute(
                """
                INSERT INTO tracked_bet_leg (bet_id, leg_no, event_id, market_id, selection, price, bookmaker_id,
                  effective_price, p_model, ref_book, ref_odds_ts, ref_fair_prob, execution_edge)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    bet_id,
                    n,
                    lg.event_id,
                    _market_id(conn, lg.market, lg.line),
                    lg.selection,
                    lg.price,
                    book_id,
                    eff,
                    lg.p_model,
                    ref_book,
                    ref_ts,
                    ref_p,
                    None if ref_p is None else eff * ref_p - 1,
                ),
            )
    return bet_id


def refresh(conn: psycopg.Connection, config: Config) -> dict[str, int]:
    """Fill closing reference / CLV where the close is known, and settle finished open bets."""
    legs = conn.execute(
        """
        SELECT l.bet_id, l.leg_no, l.event_id, m.code, m.line::float, l.selection, l.effective_price::float,
               l.close_fair_prob, e.status, r.home_goals, r.away_goals
        FROM tracked_bet_leg l JOIN tracked_bet b ON b.id = l.bet_id JOIN market m ON m.id = l.market_id
        JOIN event e ON e.id = l.event_id LEFT JOIN football_result r ON r.event_id = e.id
        WHERE b.status = 'open' OR l.close_fair_prob IS NULL
        """
    ).fetchall()
    n_clv = n_settled = 0
    by_bet: dict = defaultdict(list)
    with conn.transaction():
        for bet_id, leg_no, ev, market, line, sel, eff, close_p, status, hg, ag in legs:
            result = None
            if status == "void":
                result = "void"
            elif hg is not None:
                result = "won" if outcome(market, sel, line, hg, ag) else "lost"
            if close_p is None:
                ref = reference_at(conn, config, ev, market, line, datetime.max, closing=True)
                if ref and sel in ref[1]:
                    close_p = ref[1][sel]
                    conn.execute(
                        "UPDATE tracked_bet_leg SET close_book = %s, close_fair_prob = %s, clv = %s "
                        "WHERE bet_id = %s AND leg_no = %s",
                        (ref[0], close_p, eff * close_p - 1, bet_id, leg_no),
                    )
                    n_clv += 1
            if result:
                conn.execute(
                    "UPDATE tracked_bet_leg SET result = %s WHERE bet_id = %s AND leg_no = %s", (result, bet_id, leg_no)
                )
            by_bet[bet_id].append((result, eff))
        for bet_id, rs in by_bet.items():
            st, stake = conn.execute("SELECT status, stake::float FROM tracked_bet WHERE id = %s", (bet_id,)).fetchone()
            if st != "open":
                continue
            results = [r for r, _ in rs]
            if any(r == "lost" for r in results):
                status, pnl = "lost", -stake
            elif all(r in ("won", "void") for r in results) and results:
                live = [e for r, e in rs if r == "won"]
                status = "void" if not live else "won"
                pnl = 0.0 if not live else stake * (math.prod(live) - 1)
            else:
                continue
            conn.execute(
                "UPDATE tracked_bet SET status = %s, pnl = %s, settled_at = now() WHERE id = %s", (status, pnl, bet_id)
            )
            n_settled += 1
    return {"clv_filled": n_clv, "settled": n_settled}


def summary(conn: psycopg.Connection, n_boot: int = 2000) -> dict:
    rows = conn.execute(
        """
        SELECT b.id, b.placed_at, b.stake::float, b.status, b.pnl::float, bk.code,
               exp(sum(ln(l.effective_price::float))) AS eff_price,
               CASE WHEN bool_and(l.close_fair_prob IS NOT NULL)
                    THEN exp(sum(ln(l.effective_price::float)) + sum(ln(l.close_fair_prob))) - 1 END AS clv,
               CASE WHEN bool_and(l.ref_fair_prob IS NOT NULL)
                    THEN exp(sum(ln(l.effective_price::float)) + sum(ln(l.ref_fair_prob))) - 1 END AS exec_edge
        FROM tracked_bet b JOIN tracked_bet_leg l ON l.bet_id = b.id LEFT JOIN bookmaker bk ON bk.id = b.bookmaker_id
        GROUP BY b.id, bk.code ORDER BY b.placed_at
        """
    ).fetchall()
    if not rows:
        return {"bets": 0}
    days = np.array([r[1].date().isoformat() for r in rows])
    clv = np.array([np.nan if r[7] is None else r[7] for r in rows])
    ex = np.array([np.nan if r[8] is None else r[8] for r in rows])
    settled = [r for r in rows if r[3] in ("won", "lost")]
    staked = sum(r[2] for r in settled)
    pnl = sum(r[4] or 0 for r in settled)

    def ci(v):
        m = ~np.isnan(v)
        if m.sum() == 0:
            return None
        est, lo, hi = M.cluster_bootstrap(v[m], days[m], n_boot=n_boot)
        return {"mean": est, "lo": lo, "hi": hi, "n": int(m.sum()), "positive_share": float((v[m] > 0).mean())}

    by_book: dict = defaultdict(list)
    for r, c in zip(rows, clv, strict=True):
        by_book[r[5]].append(c)
    return {
        "bets": len(rows),
        "open": sum(r[3] == "open" for r in rows),
        "settled": len(settled),
        "staked": staked,
        "pnl": pnl,
        "roi": pnl / staked if staked else None,
        "clv": ci(clv),
        "execution_edge": ci(ex),
        "clv_by_book": {
            b: {
                "n": int((~np.isnan(np.array(v))).sum()),
                "mean": float(np.nanmean(v)) if not np.isnan(v).all() else None,
            }
            for b, v in by_book.items()
        },
    }
