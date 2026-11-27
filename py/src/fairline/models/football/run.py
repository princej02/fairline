"""Entry points shared by the CLI and Dagster."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

import polars as pl
import psycopg

from fairline.core.config import Config
from fairline.models.football.data import load_matches
from fairline.models.football.walkforward import score_live, walk_forward


def run_walkforward(
    conn: psycopg.Connection,
    config: Config,
    pool: str,
    cold_start: str | None = None,
    persist: bool = False,
    until: datetime | None = None,
) -> dict:
    """Walk forward up to `until` (default: now). The last window starts at or before `until` and
    scores every fixture in it, including scheduled ones; nothing is fitted "as of" a future date,
    even though the fixture list now runs to the end of the season."""
    s = config.settings.models.dixon_coles
    if cold_start:
        s = s.model_copy(update={"cold_start": cold_start})
    comps = s.pools[pool]
    matches = load_matches(conn, comps)
    t0 = time.perf_counter()
    now = until or datetime.now(UTC)
    wf = config.settings.models.walkforward
    res = walk_forward(matches, pool, comps, s, wf, end=now)
    out = {
        "pool": pool,
        "cold_start": s.cold_start,
        "config_hash": res.config_hash,
        "fits": len(res.fits),
        "predicted_matches": res.predictions.height,
        "seconds": round(time.perf_counter() - t0, 1),
    }
    live = None
    if wf.live_horizon_hours > 0:
        last = {f.group: f.params for f in res.fits}  # warm start from the latest weekly fit
        live = score_live(matches, pool, comps, s, now, timedelta(hours=wf.live_horizon_hours), init=last)
        out["live_predicted"] = live.predictions.height
    if persist:
        from fairline.models.football.store import persist as persist_result

        out |= persist_result(conn, res, s.model_dump())
        if live is not None and live.fits:
            out["live"] = persist_result(conn, live, s.model_dump())
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
