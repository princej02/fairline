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
from datetime import UTC, datetime, timedelta

import numpy as np
import psycopg

from fairline.backtest import metrics as M
from fairline.backtest.data import outcome
from fairline.core.config import Config
from fairline.edge.devig import IncompleteMarket, Quote, Snapshot, exchange_effective_odds, reference_fair


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


@dataclass(frozen=True)
class Ref:
    book: str
    probs: dict[str, float]
    ts: datetime
    source_line: float | None = None  # set when the book is a derived one (e.g. "derived from 3.0")

    @property
    def derived(self) -> bool:
        return self.source_line is not None


def _source_line(conn, event_id: int, book: str, ts: datetime, closing: bool) -> float | None:
    row = conn.execute(
        "SELECT d.source_line::float FROM odds_derivation d JOIN bookmaker b ON b.id = d.bookmaker_id "
        "WHERE d.event_id = %s AND b.code = %s AND d.is_closing = %s AND d.ts = %s",
        (event_id, book, closing, ts),
    ).fetchone()
    return row[0] if row else None


def reference_at(
    conn,
    config: Config,
    event_id: int,
    market: str,
    line: float | None,
    at: datetime,
    closing: bool,
    like: Ref | None = None,
) -> Ref | None:
    """Devigged reference probabilities: the latest snapshot at or before `at`, or the closing one.

    A reference book and its derived twin (pinnacle / pinnacle_derived) count as one source; the
    fresher snapshot wins, the quoted one on a tie. For closing lookups `like` (the placement
    reference) enforces like-for-like CLV: a quoted placement reference only compares with a
    quoted close, and a derived one only with a close derived from the same source line.
    Otherwise a 0.5-1% line-translation residual could pass for CLV."""
    s = config.settings.edge
    derived = s.derived_references
    books = list(s.reference_books) + [derived[b] for b in s.reference_books if b in derived]
    rows = conn.execute(
        f"""
        SELECT b.code, o.selection, o.price::float, o.ts FROM odds_snapshot o
        JOIN bookmaker b ON b.id = o.bookmaker_id JOIN market m ON m.id = o.market_id
        WHERE o.event_id = %s AND m.code = %s AND m.line IS NOT DISTINCT FROM %s AND b.code = ANY(%s)
          AND {"o.is_closing" if closing else "o.ts <= %s"}
        """,
        (event_id, market, line, books) + (() if closing else (at,)),
    ).fetchall()
    by: dict = defaultdict(list)
    for book, sel, price, ts in rows:
        by[(book, ts)].append(Quote(sel, price, ts, book))

    def best(book: str) -> Ref | None:
        is_derived = book in derived.values()
        for ts in sorted((t for (b, t) in by if b == book), reverse=True):
            try:
                fp = reference_fair(Snapshot.validate(market, by[(book, ts)]), s.devig_method, derived=is_derived)
            except IncompleteMarket:
                continue
            if fp.status == "ok":
                src = _source_line(conn, event_id, book, ts, closing) if is_derived else None
                if is_derived and src is None:
                    continue  # a derived price without its provenance is unusable
                return Ref(book, fp.probs, ts, src)
        return None

    for parent in s.reference_books:
        quoted = best(parent)
        twin = best(derived[parent]) if parent in derived else None
        if closing and like is not None:
            if like.derived:
                if twin is not None and twin.source_line == like.source_line:
                    return twin
                continue
            twin = None  # quoted placement reference: quoted closes only
        cands = [r for r in (quoted, twin) if r is not None]
        if cands:
            return max(cands, key=lambda r: (r.ts, not r.derived))
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
    lg_kickoff: dict[int, datetime] = {}
    with conn.transaction():
        book_id = conn.execute("SELECT id FROM bookmaker WHERE code = %s", (bookmaker,)).fetchone()[0]
        for lg in legs:
            ko = conn.execute("SELECT starts_at FROM event WHERE id = %s", (lg.event_id,)).fetchone()
            if ko is None:
                raise ValueError(f"unknown event {lg.event_id}")
            if placed_at >= ko[0]:
                raise ValueError(f"event {lg.event_id} has already kicked off; only pre-match bets are tracked")
            lg_kickoff[lg.event_id] = ko[0]
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
            ref_book, ref_p, ref_ts = (ref.book, ref.probs.get(lg.selection), ref.ts) if ref else (None, None, None)
            conn.execute(
                """
                INSERT INTO tracked_bet_leg (bet_id, leg_no, event_id, market_id, selection, price, bookmaker_id,
                  effective_price, p_model, ref_book, ref_odds_ts, ref_fair_prob, execution_edge, ref_source_line,
                  kickoff_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
                    ref.source_line if ref else None,
                    lg_kickoff[lg.event_id],
                ),
            )
    return bet_id


def leg_result(
    market: str, selection: str, line: float | None, status: str, starts_at: datetime,
    kickoff_at: datetime | None, home_goals: int | None, away_goals: int | None, now: datetime, void_after: timedelta,
) -> str | None:  # fmt: skip
    """won | lost | void, or None while undecided. Void: cancelled; rearranged to a date outside the
    window after the kickoff the bet was placed against; or postponed and the window has passed."""
    placed_ko = kickoff_at or starts_at
    if status == "void":
        return "void"
    if starts_at > placed_ko + void_after:
        return "void"
    if status == "postponed":
        return "void" if now > placed_ko + void_after else None
    if home_goals is not None:
        return "won" if outcome(market, selection, line, home_goals, away_goals) else "lost"
    return None


REFRESH_LOCK = 0x7472_6B72  # pg advisory lock: one refresh at a time (scheduled jobs and the UI button)


def refresh(conn: psycopg.Connection, config: Config, now: datetime | None = None) -> dict[str, int]:
    """Fill closing reference / CLV where the close is known, and settle open bets whose legs are
    all decided. Per-leg CLV fills as each match closes; a multi-leg bet stays open (and its stake
    counts as open exposure) until a leg loses or every leg is won or void. Idempotent and free."""
    now = now or datetime.now(UTC)
    void_after = timedelta(hours=config.settings.tracker.postponed_void_hours)
    n_clv = n_settled = 0
    by_bet: dict = defaultdict(list)
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (REFRESH_LOCK,))
        legs = conn.execute(
            """
            SELECT l.bet_id, l.leg_no, l.event_id, m.code, m.line::float, l.selection, l.effective_price::float,
                   l.close_fair_prob, e.status, r.home_goals, r.away_goals, l.ref_book, l.ref_odds_ts,
                   l.ref_source_line::float, e.starts_at, l.kickoff_at
            FROM tracked_bet_leg l JOIN tracked_bet b ON b.id = l.bet_id JOIN market m ON m.id = l.market_id
            JOIN event e ON e.id = l.event_id LEFT JOIN football_result r ON r.event_id = e.id
            WHERE b.status = 'open' OR l.close_fair_prob IS NULL
            """
        ).fetchall()
        for bet_id, leg_no, ev, market, line, sel, eff, close_p, status, hg, ag, rbook, rts, rsrc, ko, ko0 in legs:
            result = leg_result(market, sel, line, status, ko, ko0, hg, ag, now, void_after)
            if close_p is None:
                like = Ref(rbook, {}, rts, rsrc) if rbook else None
                ref = reference_at(conn, config, ev, market, line, datetime.max, closing=True, like=like)
                if ref and sel in ref.probs:
                    close_p = ref.probs[sel]
                    conn.execute(
                        "UPDATE tracked_bet_leg SET close_book = %s, close_fair_prob = %s, clv = %s, "
                        "close_source_line = %s, clv_note = NULL WHERE bet_id = %s AND leg_no = %s",
                        (ref.book, close_p, eff * close_p - 1, ref.source_line, bet_id, leg_no),
                    )
                    n_clv += 1
                elif like is not None and (other := reference_at(conn, config, ev, market, line, datetime.max, True)):
                    have = f"derived from {other.source_line:g}" if other.derived else "quoted"
                    want = f"derived from {like.source_line:g}" if like.derived else "quoted"
                    conn.execute(
                        "UPDATE tracked_bet_leg SET clv_note = %s WHERE bet_id = %s AND leg_no = %s",
                        (f"no like-for-like close: placed against {want}, close is {have}", bet_id, leg_no),
                    )
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
