-- P1.3: store every evaluated price (not only the ones shown), so P1.4 can measure CLV and
-- check whether filters such as edge_not_robust actually help.
ALTER TABLE edge
  ADD COLUMN decision_ts             TIMESTAMPTZ,
  ADD COLUMN odds_ts                 TIMESTAMPTZ,
  ADD COLUMN effective_price         NUMERIC(8, 3),
  ADD COLUMN ref_book                TEXT,
  ADD COLUMN ev_vs_ref               DOUBLE PRECISION,
  ADD COLUMN ev_vs_ref_conservative  DOUBLE PRECISION,
  ADD COLUMN ev_conservative         DOUBLE PRECISION,
  ADD COLUMN kelly_full              DOUBLE PRECISION,
  ADD COLUMN stake_pct               DOUBLE PRECISION,
  ADD COLUMN capped_by               TEXT[] NOT NULL DEFAULT '{}',
  ADD COLUMN flags                   TEXT[] NOT NULL DEFAULT '{}',
  ADD COLUMN on_board                BOOLEAN NOT NULL DEFAULT FALSE,
  ALTER COLUMN fair_prob DROP NOT NULL;   -- NULL when no reference market was available

COMMENT ON COLUMN edge.ev IS 'model EV: p_model * effective_price - 1';
COMMENT ON COLUMN edge.fair_prob IS 'reference (sharp) fair probability, devig_method';
CREATE UNIQUE INDEX edge_unique_eval ON edge (prediction_id, bookmaker_id, decision_ts);
