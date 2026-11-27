"""Fairline API: a thin read/compute layer over Postgres and the domain modules.

Run: uv run uvicorn fairline.api.app:app --port 8000
Decision support only. No endpoint places a bet; /api/bets records bets you placed yourself.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date as Date
from typing import Annotated

import psycopg
from fastapi import Depends, FastAPI, HTTPException, Query

from fairline.api import schemas as S
from fairline.api import services as svc
from fairline.core import db
from fairline.core.config import Config, get_config
from fairline.tracker import service as tracker

app = FastAPI(title="Fairline API", version="0.1.0")


def get_conn() -> Iterator[psycopg.Connection]:
    with db.connect() as conn:
        yield conn


Conn = Annotated[psycopg.Connection, Depends(get_conn)]
Cfg = Annotated[Config, Depends(get_config)]


def _pool(config: Config, pool: str) -> str:
    if pool not in config.settings.models.dixon_coles.pools:
        raise HTTPException(404, f"unknown pool {pool!r}")
    return pool


@app.get("/api/health")
def health(conn: Conn) -> dict:
    conn.execute("SELECT 1")
    return {"ok": True}


@app.get("/api/settings", response_model=S.SettingsOut)
def settings(config: Cfg):
    return svc.settings_out(config)


@app.get("/api/matchdays", response_model=list[S.Matchday])
def matchdays(conn: Conn, config: Cfg, pool: str = "ENG", season: str | None = None):
    return svc.matchdays(conn, config, _pool(config, pool), season)


@app.get("/api/board", response_model=S.BoardOut)
def board(
    conn: Conn,
    config: Cfg,
    date: Date,
    pool: str = "ENG",
    strategy: S.StrategyName = svc.DEFAULT_STRATEGY,
    offset_hours: Annotated[float, Query(ge=0, le=48)] = 2.0,
    bands: bool = False,
):
    return svc.board(conn, config, _pool(config, pool), date.isoformat(), strategy, offset_hours, bands)


@app.get("/api/events/{event_id}", response_model=S.EventOut)
def event(event_id: int, conn: Conn, config: Cfg):
    out = svc.event(conn, config, event_id)
    if out is None:
        raise HTTPException(404, "event not found")
    return out


@app.get("/api/events/{event_id}/odds", response_model=list[S.OddsSeries])
def odds(event_id: int, conn: Conn, config: Cfg, market: str = "1X2", line: float | None = None):
    if market == "OU" and line is None:
        line = 2.5
    return svc.odds_series(conn, config, event_id, market, line)


@app.post("/api/acca", response_model=S.AccaOut)
def acca(body: S.AccaIn, conn: Conn, config: Cfg):
    try:
        return svc.acca(conn, config, body)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e


@app.get("/api/bets", response_model=list[S.BetOut])
def list_bets(conn: Conn):
    return svc.bets(conn)


@app.post("/api/bets", response_model=S.BetOut, status_code=201)
def create_bet(body: S.BetIn, conn: Conn, config: Cfg):
    try:
        bet_id = tracker.log_bet(
            conn,
            config,
            [tracker.LegIn(lg.event_id, lg.market, lg.selection, lg.price, lg.line, lg.p_model) for lg in body.legs],
            body.bookmaker,
            body.stake,
            body.placed_at,
            body.strategy,
            body.source,
            body.suggested_stake_pct,
            body.notes,
        )
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    tracker.refresh(conn, config)
    return next(b for b in svc.bets(conn) if b.id == bet_id)


@app.post("/api/bets/refresh")
def refresh_bets(conn: Conn, config: Cfg) -> dict:
    return tracker.refresh(conn, config)


@app.post("/api/bets/{bet_id}/void", response_model=S.BetOut)
def void_bet(bet_id: int, conn: Conn):
    cur = conn.execute(
        "UPDATE tracked_bet SET status = 'void', pnl = 0, settled_at = now() WHERE id = %s AND status = 'open'",
        (bet_id,),
    )
    conn.commit()
    if cur.rowcount == 0:
        raise HTTPException(409, "bet not found or not open")
    return next(b for b in svc.bets(conn) if b.id == bet_id)


@app.get("/api/tracker/summary", response_model=S.TrackerSummary)
def tracker_summary(conn: Conn):
    return tracker.summary(conn)


@app.get("/api/backtests", response_model=list[S.BacktestListItem])
def backtests(conn: Conn):
    return svc.backtests(conn)


@app.get("/api/backtests/{run_id}", response_model=S.BacktestOut)
def backtest(run_id: int, conn: Conn):
    out = svc.backtest(conn, run_id)
    if out is None:
        raise HTTPException(404, "backtest not found")
    return out
