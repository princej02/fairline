"""Walk-forward Dixon-Coles: refit every `retrain_every_days`, predict only the following window.

Leakage rules, enforced here and tested:
- A fit at time t sees only results with kickoff + result_delay_hours <= t (dixon_coles.training_frame).
- It predicts only matches with t <= kickoff < t + step.
- Each prediction records as_of = t, and each fit records trained_through = t.

Cold start (settings.models.dixon_coles.cold_start):
- pooled: one fit per country over all its tiers; promoted clubs keep their second-tier ratings.
- prior: one fit per competition; teams with no history in it get the bottom-quantile prior.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta

import polars as pl

from fairline.core.config import DixonColesSettings, WalkForwardSettings
from fairline.models.football import dixon_coles as dc
from fairline.models.football.markets import btts, one_x_two, over_under
from fairline.models.football.scores import score_matrix

MODEL_NAME = "dixon_coles"


def config_hash(s: DixonColesSettings) -> str:
    payload = json.dumps(s.model_dump(), sort_keys=True, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()[:10]


@dataclass
class Fit:
    group: str  # e.g. 'ENG' (pooled) or 'ENG1' (prior mode)
    as_of: datetime
    params: dc.DixonColesParams


@dataclass
class WalkForwardResult:
    predictions: pl.DataFrame
    fits: list[Fit]
    config_hash: str


def fit_groups(pool_name: str, pool: list[str], s: DixonColesSettings) -> dict[str, list[str]]:
    """Which competitions are fitted together."""
    if s.cold_start == "pooled":
        return {pool_name: pool}
    return {c: [c] for c in pool}


def predict_matches(
    params: dc.DixonColesParams, fixtures: pl.DataFrame, s: DixonColesSettings, as_of: datetime
) -> list[dict]:
    rows = []
    for ev in fixtures.iter_rows(named=True):
        lam, mu, hc, ac = dc.expected_goals(params, ev["home_id"], ev["away_id"], ev["competition"], s)
        m = score_matrix(lam, mu, params.rho, s.max_goals)
        p1x2, ou, bt = one_x_two(m), over_under(m, 2.5), btts(m)
        rows.append(
            {
                "event_id": ev["event_id"],
                "competition": ev["competition"],
                "season": ev["season"],
                "starts_at": ev["starts_at"],
                "as_of": as_of,
                "home_id": ev["home_id"],
                "away_id": ev["away_id"],
                "home_goals": ev["home_goals"],
                "away_goals": ev["away_goals"],
                "lambda_home": lam,
                "lambda_away": mu,
                "rho": params.rho,
                "max_goals": s.max_goals,
                "p_home": p1x2["home"],
                "p_draw": p1x2["draw"],
                "p_away": p1x2["away"],
                "p_over25": ou["over"],
                "p_under25": ou["under"],
                "p_btts": bt["yes"],
                "home_cold_start": hc,
                "away_cold_start": ac,
            }
        )
    return rows


def walk_forward(
    matches: pl.DataFrame,
    pool_name: str,
    pool: list[str],
    s: DixonColesSettings,
    wf: WalkForwardSettings,
    start: datetime | None = None,
    end: datetime | None = None,
) -> WalkForwardResult:
    """`matches` must contain every competition in `pool` (see data.load_matches)."""
    if matches.is_empty():
        raise ValueError("no matches")
    step = timedelta(days=wf.retrain_every_days)
    first = matches["starts_at"].min()
    t = start or first + timedelta(days=wf.burn_in_days)
    t = t.replace(hour=0, minute=0, second=0, microsecond=0)
    end = end or matches["starts_at"].max() + timedelta(seconds=1)

    groups = fit_groups(pool_name, pool, s)
    data = {g: matches.filter(pl.col("competition").is_in(comps)) for g, comps in groups.items()}
    last: dict[str, dc.DixonColesParams] = {}
    fits: list[Fit] = []
    rows: list[dict] = []

    while t < end:
        window_end = t + step
        for g, frame in data.items():
            upcoming = frame.filter((pl.col("starts_at") >= t) & (pl.col("starts_at") < window_end))
            if upcoming.is_empty():
                continue
            params = dc.fit(frame, t, s, init=last.get(g))
            last[g] = params
            fits.append(Fit(g, t, params))
            rows.extend(predict_matches(params, upcoming, s, t))
        t = window_end

    preds = pl.DataFrame(rows) if rows else pl.DataFrame()
    return WalkForwardResult(predictions=preds, fits=fits, config_hash=config_hash(s))


def score_live(
    matches: pl.DataFrame,
    pool_name: str,
    pool: list[str],
    s: DixonColesSettings,
    now: datetime,
    horizon: timedelta,
    init: dict[str, dc.DixonColesParams] | None = None,
) -> WalkForwardResult:
    """One fit per group with the training cutoff at `now`, scoring every unplayed fixture kicking
    off in (now, now + horizon]. as_of = now, so these predictions are used for any decision made
    from now on, and tomorrow's run (with tonight's results) supersedes them."""
    groups = fit_groups(pool_name, pool, s)
    fits: list[Fit] = []
    rows: list[dict] = []
    for g, comps in groups.items():
        frame = matches.filter(pl.col("competition").is_in(comps))
        upcoming = frame.filter(
            (pl.col("starts_at") > now) & (pl.col("starts_at") <= now + horizon) & pl.col("home_goals").is_null()
        )
        if upcoming.is_empty():
            continue
        params = dc.fit(frame, now, s, init=(init or {}).get(g))
        fits.append(Fit(g, now, params))
        rows.extend(predict_matches(params, upcoming, s, now))
    return WalkForwardResult(pl.DataFrame(rows) if rows else pl.DataFrame(), fits, config_hash(s))
