-- P1.5 tracker: verify live CLV. Two checks per leg:
--   execution_edge = effective_price * ref_fair_prob - 1   (vs devigged reference at placement:
--                    did we really get a price better than the sharp line, or a stale-line mirage?)
--   clv            = effective_price * close_fair_prob - 1 (vs devigged reference close: true CLV)
ALTER TABLE tracked_bet
  ADD COLUMN strategy            TEXT,
  ADD COLUMN source              TEXT NOT NULL DEFAULT 'manual' CHECK (source IN ('board', 'manual')),
  ADD COLUMN suggested_stake_pct DOUBLE PRECISION,
  ADD COLUMN bankroll            NUMERIC(12, 2),
  ADD COLUMN settled_at          TIMESTAMPTZ,
  ADD COLUMN created_at          TIMESTAMPTZ NOT NULL DEFAULT now();

ALTER TABLE tracked_bet_leg
  ADD COLUMN bookmaker_id     SMALLINT REFERENCES bookmaker(id),
  ADD COLUMN effective_price  NUMERIC(8, 3),
  ADD COLUMN p_model          DOUBLE PRECISION,
  ADD COLUMN ref_book         TEXT,
  ADD COLUMN ref_odds_ts      TIMESTAMPTZ,
  ADD COLUMN ref_fair_prob    DOUBLE PRECISION,
  ADD COLUMN execution_edge   DOUBLE PRECISION,
  ADD COLUMN close_book       TEXT,
  ADD COLUMN close_fair_prob  DOUBLE PRECISION,
  ADD COLUMN clv              DOUBLE PRECISION,
  ADD COLUMN result           TEXT CHECK (result IN ('won', 'lost', 'void'));

CREATE INDEX tracked_bet_status_idx ON tracked_bet (status);
