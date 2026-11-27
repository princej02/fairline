"""Glue between the API and the domain modules. No pricing or staking logic lives here: it all
comes from edge.board (evaluate / select), edge.kelly (size), edge.acca and tracker.service."""

from __future__ import annotations

import threading
from collections import defaultdict
from datetime import UTC, datetime

import polars as pl
import psycopg

from fairline.api import schemas as S
from fairline.backtest.data import closing
from fairline.backtest.engine import STRATEGIES
from fairline.core.config import Config
from fairline.edge import acca as A
from fairline.edge.board import evaluate, select
from fairline.edge.data import load_inputs, matchday_events
from fairline.edge.kelly import binary_outcome, full_kelly, size
from fairline.models.football.walkforward import MODEL_NAME, config_hash

STRATEGY_INFO = [
    S.StrategyInfo(
        name="model_beats_ref",
        label="Must beat Pinnacle",
        description="Model edge, only where the price also beats the devigged sharp price.",
    ),
    S.StrategyInfo(
        name="reference",
        label="Reference only",
        description="Soft-book prices that beat devigged Pinnacle. Ignores the model.",
    ),
    S.StrategyInfo(
        name="model",
        label="Raw model EV",
        description="Dixon-Coles EV alone. Lost 8.3% flat (CLV -5.0%) in the P1.4 backtest.",
    ),
    S.StrategyInfo(
        name="model_no_disagree",
        label="Model, no disagreements",
        description="Raw model minus picks where model and market disagree by > disagreement_warn.",
    ),
]
DEFAULT_STRATEGY = "model_beats_ref"
_STRATS = {s.name: s for s in STRATEGIES}

_band_cache: dict[tuple[str, str], dict] = {}
# One PyMC run at a time across the process: it already uses every core (4 chains), and a second
# request for the same slate must wait for the first rather than start a duplicate sampler.
# The /api/board route is a sync `def`, so FastAPI runs it in the threadpool and the wait never
# blocks the event loop.
_sampler_lock = threading.Lock()


def settings_out(config: Config) -> S.SettingsOut:
    e, st = config.settings.edge, config.settings
    return S.SettingsOut(
        pools=st.models.dixon_coles.pools,
        bettable_books=e.bettable_books,
        reference_books=e.reference_books,
        kelly_fraction=e.kelly_fraction,
        max_stake_pct=e.max_stake_pct,
        max_match_exposure_pct=e.max_match_exposure_pct,
        max_slate_exposure_pct=e.max_slate_exposure_pct,
        min_ev=e.min_ev,
        max_odds=e.max_odds,
        devig_method=e.devig_method,
        bankroll=st.tracker.bankroll,
        currency=st.tracker.currency,
        strategies=STRATEGY_INFO,
        default_strategy=DEFAULT_STRATEGY,
    )


def matchdays(conn: psycopg.Connection, config: Config, pool: str, season: str | None) -> list[S.Matchday]:
    comps = config.settings.models.dixon_coles.pools[pool]
    rows = conn.execute(
        """
        SELECT to_char(e.starts_at AT TIME ZONE 'Europe/London', 'YYYY-MM-DD') d, count(*)
        FROM event e JOIN season s ON s.id = e.season_id JOIN competition c ON c.id = s.competition_id
        WHERE c.code = ANY(%s) AND (%s::text IS NULL OR s.label = %s)
        GROUP BY 1 ORDER BY 1 DESC
        """,
        (comps, season, season),
    ).fetchall()
    return [S.Matchday(date=d, matches=n) for d, n in rows]


def _bands(conn, config: Config, pool: str, day: str, event_ids: list[int]) -> dict:
    """PyMC bands for one slate (~5-10 s), cached in-process. as_of = the matchday's midnight UTC,
    which is before every decision time on that day."""
    key = (pool, day)
    if (hit := _band_cache.get(key)) is not None:
        return hit
    with _sampler_lock:
        if (hit := _band_cache.get(key)) is not None:  # filled while we waited
            return hit
        out = _sample_bands(conn, config, pool, day, event_ids)
        _band_cache[key] = out
        return out


def _sample_bands(conn, config: Config, pool: str, day: str, event_ids: list[int]) -> dict:
    from fairline.models.football.bayes import fit_and_predict
    from fairline.models.football.data import load_matches

    st = config.settings
    matches = load_matches(conn, st.models.dixon_coles.pools[pool])
    fixtures = matches.filter(pl.col("event_id").is_in(event_ids))
    t = datetime.fromisoformat(day).replace(tzinfo=UTC)
    r = fit_and_predict(
        matches,
        fixtures,
        t,
        st.models.dixon_coles,
        st.models.bayes,
        conservative_quantile=st.edge.conservative_quantile,
    )
    out = {
        "healthy": r.healthy,
        "rows": {
            (x["event_id"], x["market"], x["line"], x["selection"]): x for x in r.predictions.iter_rows(named=True)
        },
    }
    return out


def _derived_notes(conn, cands, derived_books: set[str]) -> dict:
    """(event_id, decision_ts) -> 'derived from 3.0' for the derivation a board row used: the
    latest pre-decision derivation for that event, which is the snapshot the board picked."""
    keys = {(c.event_id, c.decision_ts) for c in cands if c.ref_book in derived_books}
    out = {}
    for eid, dts in keys:
        row = conn.execute(
            "SELECT source_line::float, draw_fit_error FROM odds_derivation "
            "WHERE event_id = %s AND NOT is_closing AND ts <= %s ORDER BY ts DESC LIMIT 1",
            (eid, dts),
        ).fetchone()
        if row:
            out[(eid, dts)] = f"derived from {row[0]:g} (draw fit {row[1]:+.1%})"
    return out


def board(
    conn: psycopg.Connection, config: Config, pool: str, day: str, strategy: str, offset_hours: float, bands: bool
) -> S.BoardOut:
    st = config.settings
    s = st.edge
    comps = st.models.dixon_coles.pools[pool]
    events = matchday_events(conn, comps, day, offset_hours)
    notes: list[str] = []
    if not events:
        return S.BoardOut(
            pool=pool,
            date=day,
            strategy=strategy,
            bands=bands,
            rows=[],
            evaluated_prices=0,
            skipped_snapshots={},
            notes=["no matches on this day"],
        )
    ids = [e.event_id for e in events]
    books = s.live_quote_books()
    probs, quotes = load_inputs(conn, events, books, MODEL_NAME, config_hash(st.models.dixon_coles))
    band_rows: dict = {}
    if bands:
        b = _bands(conn, config, pool, day, ids)
        band_rows = b["rows"]
        if not b["healthy"]:
            notes.append("PyMC sample for this slate failed its health check; treat bands as unreliable")
        notes.append("P1.4: bands are too wide (the 90% band covered the Pinnacle close 98.7% of the time)")
        for p in probs:
            br = band_rows.get((p.event_id, p.market, p.line, p.selection))
            if br:
                p.p_conservative = br["prob_conservative"]
    exchanges = {b.code for b in config.bookmakers.values() if b.kind == "exchange"}
    cands, skipped = evaluate(events, probs, quotes, s, exchanges)

    strat = _STRATS[strategy]
    picks = select(cands, s, prob_of=lambda c: strat.prob_of(c, float("nan")), keep=strat.keep)
    stakes = size(
        [
            (
                str(i),
                pk.candidate.event_id,
                pk.candidate.slate,
                full_kelly(binary_outcome(pk.p), pk.candidate.effective_price),
            )
            for i, pk in enumerate(picks)
        ],
        s.kelly_fraction,
        s.max_stake_pct,
        s.max_match_exposure_pct,
        s.max_slate_exposure_pct,
    )

    meta = {}
    pred_ids = [pk.candidate.prediction_id for pk in picks if pk.candidate.prediction_id]
    if pred_ids:
        for pid, he, ae, hc, ac in conn.execute(
            """
            SELECT p.id,
              (mv.params #>> ARRAY['fit', 'teams', e.home_id::text, 'effective_matches'])::float,
              (mv.params #>> ARRAY['fit', 'teams', e.away_id::text, 'effective_matches'])::float,
              pr.home_cold_start, pr.away_cold_start
            FROM prediction p JOIN model_version mv ON mv.id = p.model_version_id JOIN event e ON e.id = p.event_id
            LEFT JOIN prediction_rate pr ON pr.model_version_id = p.model_version_id
              AND pr.event_id = p.event_id AND pr.as_of = p.as_of
            WHERE p.id = ANY(%s)
            """,
            (pred_ids,),
        ):
            meta[pid] = (he, ae, bool(hc) or bool(ac))
    comp_of = dict(
        conn.execute(
            "SELECT e.id, c.code FROM event e JOIN season s ON s.id = e.season_id "
            "JOIN competition c ON c.id = s.competition_id WHERE e.id = ANY(%s)",
            (ids,),
        ).fetchall()
    )

    ref_notes = _derived_notes(conn, [pk.candidate for pk in picks], set(s.derived_references.values()))
    rows = []
    for pk, stake in zip(picks, stakes, strict=True):
        c = pk.candidate
        br = band_rows.get((c.event_id, c.market, c.line, c.selection))
        he, ae, cold = meta.get(c.prediction_id, (None, None, False))
        rows.append(
            S.BoardRow(
                event_id=c.event_id,
                label=c.label,
                competition=comp_of.get(c.event_id),
                starts_at=c.starts_at,
                decision_ts=c.decision_ts,
                market=c.market,
                line=c.line,
                selection=c.selection,
                bookmaker=c.bookmaker,
                odds_ts=c.odds_ts,
                price=c.price,
                effective_price=c.effective_price,
                p_model=c.p_model,
                p_ref=c.p_ref,
                ref_book=c.ref_book,
                ref_note=ref_notes.get((c.event_id, c.decision_ts))
                if c.ref_book in s.derived_references.values()
                else None,
                p_bet=pk.p,
                ev_strategy=pk.ev,
                ev_model=c.ev_model,
                ev_vs_ref=c.ev_vs_ref,
                ev_conservative=c.ev_conservative,
                band=S.Band(
                    lo=br["prob_lo"],
                    hi=br["prob_hi"],
                    conservative=br["prob_conservative"],
                    interval=st.models.bayes.interval,
                )
                if br
                else None,
                home_effective_matches=he,
                away_effective_matches=ae,
                cold_start=cold,
                stake_pct=stake.stake,
                stake_amount=round(stake.stake * st.tracker.bankroll, 2),
                capped_by=list(stake.capped_by),
                flags=c.flags,
            )
        )
    rows.sort(key=lambda r: (r.starts_at, -r.ev_strategy))
    return S.BoardOut(
        pool=pool,
        date=day,
        strategy=strategy,
        bands=bands,
        rows=rows,
        evaluated_prices=len(cands),
        skipped_snapshots=skipped,
        notes=notes,
    )


def _latest_prediction(conn, config: Config, event_id: int):
    """Latest walk-forward prediction made before kickoff, plus its rates."""
    h = config_hash(config.settings.models.dixon_coles)
    rows = conn.execute(
        """
        WITH latest AS (
          SELECT p.as_of, p.model_version_id FROM prediction p JOIN model_version mv ON mv.id = p.model_version_id
          JOIN event e ON e.id = p.event_id
          WHERE p.event_id = %s AND p.as_of < e.starts_at AND mv.name = %s AND mv.version LIKE %s || '/%%'
          ORDER BY p.as_of DESC LIMIT 1
        )
        SELECT m.code, m.line::float, p.selection, p.prob, l.as_of, pr.lambda_home, pr.lambda_away, pr.rho, pr.max_goals
        FROM latest l JOIN prediction p ON p.event_id = %s AND p.as_of = l.as_of AND p.model_version_id = l.model_version_id
        JOIN market m ON m.id = p.market_id
        LEFT JOIN prediction_rate pr ON pr.model_version_id = l.model_version_id AND pr.event_id = %s AND pr.as_of = l.as_of
        """,
        (event_id, MODEL_NAME, h, event_id, event_id),
    ).fetchall()
    return rows


def _key(market: str, line: float | None, sel: str) -> str:
    return f"{market}{'' if line is None else f' {line:g}'}:{sel}"


def event(conn: psycopg.Connection, config: Config, event_id: int) -> S.EventOut | None:
    row = conn.execute(
        """
        SELECT e.id, h.name || ' v ' || a.name, c.code, s.label, e.starts_at, h.name, a.name, r.home_goals, r.away_goals
        FROM event e JOIN season s ON s.id = e.season_id JOIN competition c ON c.id = s.competition_id
        JOIN participant h ON h.id = e.home_id JOIN participant a ON a.id = e.away_id
        LEFT JOIN football_result r ON r.event_id = e.id WHERE e.id = %s
        """,
        (event_id,),
    ).fetchone()
    if row is None:
        return None
    preds = _latest_prediction(conn, config, event_id)
    model = {_key(m, ln, sel): p for m, ln, sel, p, *_ in preds}
    rates = None
    if preds and preds[0][5] is not None:
        rates = {"lambda_home": preds[0][5], "lambda_away": preds[0][6], "rho": preds[0][7]}
    p_close, _ = closing(conn, [event_id], config.settings.edge)
    return S.EventOut(
        event_id=row[0],
        label=row[1],
        competition=row[2],
        season=row[3],
        starts_at=row[4],
        home=row[5],
        away=row[6],
        home_goals=row[7],
        away_goals=row[8],
        model=model,
        model_as_of=preds[0][4] if preds else None,
        rates=rates,
        close_fair={_key(m, ln, sel): p for (_, m, ln, sel), p in p_close.items()},
    )


def odds_series(conn: psycopg.Connection, config: Config, event_id: int, market: str, line: float | None):
    s = config.settings.edge
    books = s.live_quote_books()  # includes derived references (e.g. pinnacle_derived)
    rows = conn.execute(
        """
        SELECT b.code, o.selection, o.ts, o.price::float, o.is_closing, o.ts_estimated FROM odds_snapshot o
        JOIN bookmaker b ON b.id = o.bookmaker_id JOIN market m ON m.id = o.market_id
        WHERE o.event_id = %s AND m.code = %s AND m.line IS NOT DISTINCT FROM %s AND b.code = ANY(%s)
        ORDER BY o.ts
        """,
        (event_id, market, line, books),
    ).fetchall()
    grouped: dict = defaultdict(list)
    for book, sel, ts, price, closing_, est in rows:
        grouped[(book, sel)].append(S.OddsPoint(ts=ts, price=price, is_closing=closing_, ts_estimated=est))
    return [
        S.OddsSeries(
            bookmaker=b,
            selection=sel,
            is_reference=b in s.reference_books or b in s.derived_references.values(),
            points=pts,
        )
        for (b, sel), pts in sorted(grouped.items())
    ]


def acca(conn: psycopg.Connection, config: Config, body: S.AccaIn) -> S.AccaOut:
    s = config.settings.edge
    legs = []
    for lg in body.legs:
        preds = _latest_prediction(conn, config, lg.event_id)
        model = {_key(m, ln, sel): p for m, ln, sel, p, *_ in preds}
        rates = None
        if preds and preds[0][5] is not None:
            rates = A.Rates(preds[0][5], preds[0][6], preds[0][7], preds[0][8])
        if lg.market == "BTTS":
            if rates is None:
                raise ValueError(f"event {lg.event_id}: no model rates for BTTS")
            from fairline.models.football.markets import btts
            from fairline.models.football.scores import score_matrix

            p_model = btts(score_matrix(rates.lambda_home, rates.lambda_away, rates.rho, rates.max_goals))[lg.selection]
        else:
            p_model = model.get(_key(lg.market, lg.line, lg.selection))
            if p_model is None:
                raise ValueError(f"event {lg.event_id}: no model probability for {lg.market} {lg.selection}")
        overround = None
        if lg.bookmaker:
            r = conn.execute(
                """
                SELECT sum(1 / o.price::float) FROM odds_snapshot o JOIN bookmaker b ON b.id = o.bookmaker_id
                JOIN market m ON m.id = o.market_id
                WHERE o.event_id = %s AND b.code = %s AND m.code = %s AND m.line IS NOT DISTINCT FROM %s AND NOT o.is_closing
                GROUP BY o.ts ORDER BY o.ts DESC LIMIT 1
                """,
                (lg.event_id, lg.bookmaker, lg.market, lg.line),
            ).fetchone()
            overround = r[0] if r else None
        teams = conn.execute("SELECT home_id, away_id FROM event WHERE id = %s", (lg.event_id,)).fetchone()
        legs.append(
            A.Leg(
                lg.event_id,
                lg.market,
                lg.selection,
                lg.price,
                p_model,
                line=lg.line,
                bookmaker=lg.bookmaker,
                overround=overround,
                home_id=teams[0],
                away_id=teams[1],
                rates=rates,
            )
        )
    q = A.build(legs, s, builder_prices=body.builder_prices)
    stake = min(q.kelly * s.kelly_fraction, s.max_stake_pct)
    return S.AccaOut(
        ok=q.ok,
        legs=[
            S.AccaLegOut(
                event_id=v.leg.event_id,
                market=v.leg.market,
                selection=v.leg.selection,
                line=v.leg.line,
                price=v.leg.price,
                p_model=v.leg.p_model,
                ev=v.ev,
                drags=v.drags,
            )
            for v in q.legs
        ],
        p_joint=q.p_joint,
        p_naive=q.p_naive,
        combined_price=q.combined_price,
        ev=q.ev,
        kelly_full=q.kelly,
        suggested_stake_pct=stake,
        compounded_margin=q.compounded_margin,
        same_match_fair_odds=q.same_match_groups,
        warnings=q.warnings,
        blocked=q.blocked,
    )


def bets(conn: psycopg.Connection) -> list[S.BetOut]:
    rows = conn.execute(
        """
        SELECT b.id, b.placed_at, bk.code, b.stake::float, b.price::float, b.status, b.pnl::float, b.strategy, b.source,
               b.suggested_stake_pct, b.notes,
               l.leg_no, l.event_id, h.name || ' v ' || a.name, m.code, m.line::float, l.selection, l.price::float,
               l.effective_price::float, l.ref_book, l.ref_fair_prob, l.execution_edge, l.close_fair_prob, l.clv, l.result,
               l.ref_source_line::float, l.close_source_line::float, l.clv_note
        FROM tracked_bet b LEFT JOIN bookmaker bk ON bk.id = b.bookmaker_id
        JOIN tracked_bet_leg l ON l.bet_id = b.id JOIN market m ON m.id = l.market_id JOIN event e ON e.id = l.event_id
        JOIN participant h ON h.id = e.home_id JOIN participant a ON a.id = e.away_id
        ORDER BY b.placed_at DESC, b.id DESC, l.leg_no
        """
    ).fetchall()
    out: dict[int, S.BetOut] = {}
    for r in rows:
        if r[0] not in out:
            out[r[0]] = S.BetOut(
                id=r[0],
                placed_at=r[1],
                bookmaker=r[2],
                stake=r[3],
                price=r[4],
                status=r[5],
                pnl=r[6],
                strategy=r[7],
                source=r[8],
                suggested_stake_pct=r[9],
                notes=r[10],
                legs=[],
            )
        out[r[0]].legs.append(
            S.BetLegOut(
                leg_no=r[11],
                event_id=r[12],
                label=r[13],
                market=r[14],
                line=r[15],
                selection=r[16],
                price=r[17],
                effective_price=r[18],
                ref_book=r[19],
                ref_fair_prob=r[20],
                execution_edge=r[21],
                close_fair_prob=r[22],
                clv=r[23],
                result=r[24],
                ref_source_line=r[25],
                close_source_line=r[26],
                clv_note=r[27],
            )
        )
    return list(out.values())


def backtests(conn: psycopg.Connection) -> list[S.BacktestListItem]:
    return [
        S.BacktestListItem(
            id=i, started_at=t, pools=c.get("pools", []), first=c.get("first", ""), last=c.get("last", "")
        )
        for i, t, c in conn.execute("SELECT id, started_at, config FROM backtest_run ORDER BY id DESC")
    ]


def backtest(conn: psycopg.Connection, run_id: int) -> S.BacktestOut | None:
    r = conn.execute("SELECT id, started_at, config, metrics FROM backtest_run WHERE id = %s", (run_id,)).fetchone()
    return None if r is None else S.BacktestOut(id=r[0], started_at=r[1], config=r[2], metrics=r[3] or {})
