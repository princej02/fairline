"""Simulate leagues from known Dixon-Coles parameters, for recovery and leakage tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import polars as pl

from fairline.models.football.scores import score_matrix


def simulate_season(
    rng,
    teams: list[int],
    att: dict,
    dfn: dict,
    comp: str,
    start: datetime,
    mu=0.25,
    home=0.25,
    rho=-0.08,
    season="sim",
    event_offset=0,
):
    rows = []
    rounds = [(h, a) for h in teams for a in teams if h != a]
    rng.shuffle(rounds)
    per_week = len(teams) // 2
    for k, (h, a) in enumerate(rounds):
        lam = np.exp(mu + home + att[h] - dfn[a])
        m = np.exp(mu + att[a] - dfn[h])
        mat = score_matrix(lam, m, rho, 10).ravel()
        idx = rng.choice(mat.size, p=mat)
        rows.append(
            {
                "event_id": event_offset + k,
                "starts_at": start + timedelta(days=7 * (k // per_week), hours=15),
                "competition": comp,
                "season": season,
                "home_id": h,
                "away_id": a,
                "home_goals": idx // 11,
                "away_goals": idx % 11,
            }
        )
    return rows


def frame(rows) -> pl.DataFrame:
    return pl.DataFrame(rows).with_columns(
        pl.col("starts_at").dt.replace_time_zone("UTC") if pl.DataFrame(rows)["starts_at"].dtype.time_zone is None
        else pl.col("starts_at"),
        pl.col("home_goals").cast(pl.Int16),
        pl.col("away_goals").cast(pl.Int16),
    )  # fmt: skip


def two_tier_country(seed=1, seasons=3):
    """20-team top tier + 20-team second tier, 3 up / 3 down each summer. Second-tier teams are weaker."""
    rng = np.random.default_rng(seed)
    top = list(range(1, 21))
    second = list(range(21, 41))
    att = {t: rng.normal(0.15, 0.2) for t in top} | {t: rng.normal(-0.25, 0.2) for t in second}
    dfn = {t: rng.normal(0.15, 0.2) for t in top} | {t: rng.normal(-0.25, 0.2) for t in second}
    rows = []
    start = datetime(2020, 8, 1, tzinfo=UTC)
    off = 0
    for s in range(seasons):
        label = f"{2020 + s}-{(21 + s) % 100:02d}"
        r1 = simulate_season(rng, top, att, dfn, "T1", start, season=label, event_offset=off)
        off += len(r1)
        r2 = simulate_season(rng, second, att, dfn, "T2", start + timedelta(hours=1), season=label, event_offset=off)
        off += len(r2)
        rows += r1 + r2
        # promotion / relegation: weakest 3 top go down, strongest 3 second go up
        strength = {t: att[t] + dfn[t] for t in top + second}
        down = sorted(top, key=strength.get)[:3]
        up = sorted(second, key=strength.get)[-3:]
        top = [t for t in top if t not in down] + up
        second = [t for t in second if t not in up] + down
        start += timedelta(days=365)
    return frame(rows), att, dfn, top
