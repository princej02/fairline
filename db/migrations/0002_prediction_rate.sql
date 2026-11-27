-- Per-match model state for each fit, so any derived market (BTTS, Asian handicap, other
-- totals lines) can be recomputed for a historical fold without re-running the optimiser:
--   score_matrix(lambda_home, lambda_away, rho, max_goals) -> markets.*
CREATE TABLE prediction_rate (
  model_version_id  INT NOT NULL REFERENCES model_version(id) ON DELETE CASCADE,
  event_id          BIGINT NOT NULL REFERENCES event(id) ON DELETE CASCADE,
  as_of             TIMESTAMPTZ NOT NULL,
  lambda_home       DOUBLE PRECISION NOT NULL CHECK (lambda_home > 0),
  lambda_away       DOUBLE PRECISION NOT NULL CHECK (lambda_away > 0),
  rho               DOUBLE PRECISION NOT NULL,
  max_goals         SMALLINT NOT NULL CHECK (max_goals >= 10),
  home_cold_start   BOOLEAN NOT NULL,
  away_cold_start   BOOLEAN NOT NULL,
  PRIMARY KEY (model_version_id, event_id, as_of)
);
CREATE INDEX prediction_rate_event_idx ON prediction_rate (event_id, as_of);
