"""Entry points shared by the CLI and Dagster."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

import polars as pl
import psycopg

from fairline.core.config import Config
from fairline.models.football.data import load_matches
from fairline.models.football.walkforward import walk_forward


def run_walkforward(
    conn: psycopg.Connection, config: Config, pool: str, cold_start: str | None = None, persist: bool = False
) -> dict:
    s = config.settings.models.dixon_coles
    if cold_start:
        s = s.model_copy(update={"cold_start": cold_start})
    comps = s.pools[pool]
    matches = load_matches(conn, comps)
    t0 = time.perf_counter()
    res = walk_forward(matches, pool, comps, s, config.settings.models.walkforward)
    out = {
        "pool": pool,
        "cold_start": s.cold_start,
        "config_hash": res.config_hash,
        "fits": len(res.fits),
        "predicted_matches": res.predictions.height,
        "seconds": round(time.perf_counter() - t0, 1),
    }
    if persist:
        from fairline.models.football.store import persist as persist_result

        out |= persist_result(conn, res, s.model_dump())
    return out


def run_bayes(conn: psycopg.Connection, config: Config, pool: str, as_of: str) -> pl.DataFrame:
    from fairline.models.football.bayes import fit_and_predict

    s = config.settings.models.dixon_coles
    t = datetime.fromisoformat(as_of).replace(tzinfo=UTC)
    matches = load_matches(conn, s.pools[pool])
    fixtures = matches.filter((pl.col("starts_at") >= t) & (pl.col("starts_at") < t + timedelta(days=7)))
    t0 = time.perf_counter()
    r = fit_and_predict(matches, fixtures, t, s, config.settings.models.bayes)
    print(
        f"bayes {pool} as_of={as_of}: {r.n_matches} training matches, {fixtures.height} fixtures, "
        f"max r_hat={r.max_rhat:.4f}, divergences={r.divergences}, healthy={r.healthy}, "
        f"{time.perf_counter() - t0:.0f}s"
    )
    return r.predictions
