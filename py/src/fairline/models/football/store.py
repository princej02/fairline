"""Persist walk-forward fits (model_version) and predictions (prediction). Idempotent."""

from __future__ import annotations

import json

import psycopg

from fairline.models.football.walkforward import MODEL_NAME, WalkForwardResult

# (column in predictions frame, market code, line, selection). Only enabled markets get probability
# rows; prediction_rate keeps lambda_home / lambda_away / rho per match so BTTS, AH and other lines
# can be recomputed for any historical fold without refitting (see recompute.py).
STORED = [
    ("p_home", "1X2", None, "home"),
    ("p_draw", "1X2", None, "draw"),
    ("p_away", "1X2", None, "away"),
    ("p_over25", "OU", 2.5, "over"),
    ("p_under25", "OU", 2.5, "under"),
]


def version_label(cfg_hash: str, group: str, as_of) -> str:
    return f"{cfg_hash}/{group}/{as_of:%Y-%m-%d}"


def persist(conn: psycopg.Connection, result: WalkForwardResult, settings_json: dict) -> dict[str, int]:
    markets = {
        (code, None if line is None else float(line)): mid
        for mid, code, line in conn.execute(
            "SELECT m.id, m.code, m.line FROM market m JOIN sport s ON s.id = m.sport_id WHERE s.code = 'football'"
        )
    }
    versions = 0
    pred_rows = []
    rate_rows = []
    with conn.transaction():
        version_ids: dict[tuple[str, object], int] = {}
        for f in result.fits:
            params = {"config": settings_json, "group": f.group, "fit": f.params.to_json()}
            vid = conn.execute(
                """
                INSERT INTO model_version (name, version, params, trained_through)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (name, version) DO UPDATE SET params = EXCLUDED.params
                RETURNING id
                """,
                (MODEL_NAME, version_label(result.config_hash, f.group, f.as_of), json.dumps(params), f.as_of),
            ).fetchone()[0]
            version_ids[(f.group, f.as_of)] = vid
            versions += 1

        if not result.predictions.is_empty():
            group_of = _group_lookup(result)
            for r in result.predictions.iter_rows(named=True):
                vid = version_ids[(group_of[(r["competition"], r["as_of"])], r["as_of"])]
                for col, code, line, sel in STORED:
                    pred_rows.append((vid, r["event_id"], markets[(code, line)], sel, r[col], r["as_of"]))
                rate_rows.append(
                    (
                        vid, r["event_id"], r["as_of"], r["lambda_home"], r["lambda_away"], r["rho"],
                        r["max_goals"], r["home_cold_start"], r["away_cold_start"],
                    )
                )  # fmt: skip
            with conn.cursor() as cur:
                cur.executemany(
                    """
                    INSERT INTO prediction (model_version_id, event_id, market_id, selection, prob, as_of)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (model_version_id, event_id, market_id, selection, as_of)
                    DO UPDATE SET prob = EXCLUDED.prob
                    """,
                    pred_rows,
                )
                cur.executemany(
                    """
                    INSERT INTO prediction_rate (model_version_id, event_id, as_of, lambda_home, lambda_away,
                      rho, max_goals, home_cold_start, away_cold_start)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (model_version_id, event_id, as_of) DO UPDATE SET
                      lambda_home = EXCLUDED.lambda_home, lambda_away = EXCLUDED.lambda_away, rho = EXCLUDED.rho,
                      max_goals = EXCLUDED.max_goals, home_cold_start = EXCLUDED.home_cold_start,
                      away_cold_start = EXCLUDED.away_cold_start
                    """,
                    rate_rows,
                )
    return {"model_versions": versions, "predictions": len(pred_rows), "rates": len(rate_rows)}


def _group_lookup(result: WalkForwardResult) -> dict:
    out = {}
    for f in result.fits:
        for comp in f.params.divisions:
            out[(comp, f.as_of)] = f.group
    return out
