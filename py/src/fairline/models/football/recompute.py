"""Rebuild any market for a stored historical prediction from prediction_rate, without refitting."""

from __future__ import annotations

import polars as pl
import psycopg

from fairline.models.football.markets import asian_handicap, asian_total, btts, one_x_two
from fairline.models.football.scores import score_matrix

_SQL = """
SELECT r.event_id, r.as_of, r.lambda_home, r.lambda_away, r.rho, r.max_goals, mv.version
FROM prediction_rate r JOIN model_version mv ON mv.id = r.model_version_id
WHERE r.event_id = ANY(%s) AND (%s::text IS NULL OR mv.version LIKE %s || '/%%')
ORDER BY r.event_id, r.as_of
"""


def load_rates(conn: psycopg.Connection, event_ids: list[int], config_hash: str | None = None) -> pl.DataFrame:
    rows = conn.execute(_SQL, (event_ids, config_hash, config_hash)).fetchall()
    cols = ["event_id", "as_of", "lambda_home", "lambda_away", "rho", "max_goals", "version"]
    return pl.DataFrame(rows, schema=cols, orient="row")


def derive(
    lambda_home: float, lambda_away: float, rho: float, max_goals: int, ah_line: float | None = None,
    total_line: float | None = None,
) -> dict[str, float]:  # fmt: skip
    m = score_matrix(lambda_home, lambda_away, rho, max_goals)
    out = {f"1x2_{k}": v for k, v in one_x_two(m).items()} | {f"btts_{k}": v for k, v in btts(m).items()}
    if ah_line is not None:
        h = asian_handicap(m, ah_line, "home")
        out |= {"ah_home_fair_odds": h.fair_odds, "ah_home_effective_prob": h.effective_prob}
    if total_line is not None:
        o = asian_total(m, total_line, "over")
        out |= {"total_over_fair_odds": o.fair_odds, "total_over_effective_prob": o.effective_prob}
    return out
