"""Pydantic response/request models. They mirror the domain dataclasses one-to-one."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

StrategyName = Literal["model_beats_ref", "reference", "model", "model_no_disagree"]


class StrategyInfo(BaseModel):
    name: StrategyName
    label: str
    description: str


class SettingsOut(BaseModel):
    pools: dict[str, list[str]]
    bettable_books: list[str]
    reference_books: list[str]
    kelly_fraction: float
    max_stake_pct: float
    max_match_exposure_pct: float
    max_slate_exposure_pct: float
    min_ev: float
    max_odds: float
    devig_method: str
    bankroll: float
    currency: str
    strategies: list[StrategyInfo]
    default_strategy: StrategyName


class Band(BaseModel):
    lo: float
    hi: float
    conservative: float
    interval: float


class BoardRow(BaseModel):
    event_id: int
    label: str
    competition: str | None
    starts_at: datetime
    decision_ts: datetime
    market: str
    line: float | None
    selection: str
    bookmaker: str
    odds_ts: datetime
    price: float
    effective_price: float
    p_model: float
    p_ref: float | None
    ref_book: str | None
    ref_note: str | None = None  # e.g. "derived from 3.0": the reference is computed, not quoted
    p_bet: float = Field(description="probability the chosen strategy bets on")
    ev_strategy: float
    ev_model: float
    ev_vs_ref: float | None
    ev_conservative: float | None
    band: Band | None
    home_effective_matches: float | None
    away_effective_matches: float | None
    cold_start: bool
    stake_pct: float
    stake_amount: float
    capped_by: list[str]
    flags: list[str]


class BoardOut(BaseModel):
    pool: str
    date: str
    strategy: StrategyName
    bands: bool
    rows: list[BoardRow]
    evaluated_prices: int
    skipped_snapshots: dict[str, int]
    notes: list[str]


class Matchday(BaseModel):
    date: str
    matches: int


class OddsPoint(BaseModel):
    ts: datetime
    price: float
    is_closing: bool
    ts_estimated: bool


class OddsSeries(BaseModel):
    bookmaker: str
    selection: str
    is_reference: bool
    points: list[OddsPoint]


class EventOut(BaseModel):
    event_id: int
    label: str
    competition: str
    season: str
    starts_at: datetime
    home: str
    away: str
    home_goals: int | None
    away_goals: int | None
    model: dict[str, float]  # "1X2:home" -> p, latest prediction
    model_as_of: datetime | None
    rates: dict[str, float] | None  # lambda_home, lambda_away, rho
    close_fair: dict[str, float]  # "1X2:home" -> devigged closing reference p


class AccaLegIn(BaseModel):
    event_id: int
    market: Literal["1X2", "OU", "BTTS"]
    selection: str
    price: float = Field(gt=1)
    line: float | None = None
    bookmaker: str = ""


class AccaIn(BaseModel):
    legs: list[AccaLegIn]
    builder_prices: dict[int, float] = Field(default_factory=dict)


class AccaLegOut(BaseModel):
    event_id: int
    market: str
    selection: str
    line: float | None
    price: float
    p_model: float
    ev: float
    drags: bool


class AccaOut(BaseModel):
    ok: bool
    legs: list[AccaLegOut]
    p_joint: float | None
    p_naive: float
    combined_price: float | None
    ev: float | None
    kelly_full: float
    suggested_stake_pct: float
    compounded_margin: float | None
    same_match_fair_odds: dict[int, float]
    warnings: list[str]
    blocked: list[str]


class BetLegIn(BaseModel):
    event_id: int
    market: Literal["1X2", "OU"]
    selection: str
    price: float = Field(gt=1)
    line: float | None = None
    p_model: float | None = None


class BetIn(BaseModel):
    legs: list[BetLegIn] = Field(min_length=1)
    bookmaker: str
    stake: float = Field(gt=0)
    placed_at: datetime
    strategy: str | None = None
    source: Literal["board", "manual"] = "manual"
    suggested_stake_pct: float | None = None
    notes: str | None = None


class BetLegOut(BaseModel):
    leg_no: int
    event_id: int
    label: str
    market: str
    line: float | None
    selection: str
    price: float
    effective_price: float | None
    ref_book: str | None
    ref_fair_prob: float | None
    execution_edge: float | None
    close_fair_prob: float | None
    clv: float | None
    result: str | None
    ref_source_line: float | None = None  # set when the placement reference was derived ("derived from 3.0")
    close_source_line: float | None = None
    clv_note: str | None = None  # why CLV is empty when no like-for-like close exists


class BetOut(BaseModel):
    id: int
    placed_at: datetime
    bookmaker: str | None
    stake: float
    price: float
    status: str
    pnl: float | None
    strategy: str | None
    source: str
    suggested_stake_pct: float | None
    notes: str | None
    legs: list[BetLegOut]


class CI(BaseModel):
    mean: float
    lo: float
    hi: float
    n: int
    positive_share: float


class TrackerSummary(BaseModel):
    bets: int
    open: int = 0
    settled: int = 0
    staked: float = 0
    pnl: float = 0
    roi: float | None = None
    clv: CI | None = None
    execution_edge: CI | None = None
    clv_by_book: dict[str, dict[str, float | int | None]] = Field(default_factory=dict)


class BacktestListItem(BaseModel):
    id: int
    started_at: datetime
    pools: list[str]
    first: str
    last: str


class BacktestOut(BaseModel):
    id: int
    started_at: datetime
    config: dict
    metrics: dict
