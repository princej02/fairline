-- Derived reference prices (P1.1-7): O/U 2.5 implied from Pinnacle's 1X2 + main totals line when
-- Pinnacle does not quote 2.5 itself. They live under their own pseudo-bookmaker
-- (kind 'derived', e.g. pinnacle_derived) so no query can mix them with quoted prices by accident.
ALTER TABLE bookmaker DROP CONSTRAINT bookmaker_kind_check;
ALTER TABLE bookmaker ADD CONSTRAINT bookmaker_kind_check CHECK (kind IN ('book', 'exchange', 'aggregate', 'derived'));

-- How each derived snapshot was made. Same key as its odds_snapshot rows (per event/book/close/ts).
CREATE TABLE odds_derivation (
  event_id        BIGINT NOT NULL REFERENCES event(id) ON DELETE CASCADE,
  bookmaker_id    SMALLINT NOT NULL REFERENCES bookmaker(id),   -- the derived pseudo-book
  is_closing      BOOLEAN NOT NULL,
  ts              TIMESTAMPTZ NOT NULL,
  market_id       INT NOT NULL REFERENCES market(id),            -- what was derived (OU 2.5)
  source_book_id  SMALLINT NOT NULL REFERENCES bookmaker(id),    -- pinnacle
  source_line     NUMERIC(5, 2) NOT NULL,                        -- the line it was derived from
  source_over     NUMERIC(8, 3) NOT NULL,
  source_under    NUMERIC(8, 3) NOT NULL,
  lambda_home     DOUBLE PRECISION NOT NULL,
  lambda_away     DOUBLE PRECISION NOT NULL,
  rho             DOUBLE PRECISION NOT NULL,
  p_over          DOUBLE PRECISION NOT NULL,
  draw_fit_error  DOUBLE PRECISION NOT NULL,                     -- matrix draw - Pinnacle's devigged draw
  method          TEXT NOT NULL,
  PRIMARY KEY (event_id, bookmaker_id, market_id, is_closing, ts)
);

-- Like-for-like CLV for O/U: which line each reference was derived from (NULL = quoted).
ALTER TABLE tracked_bet_leg ADD COLUMN ref_source_line NUMERIC(5, 2);
ALTER TABLE tracked_bet_leg ADD COLUMN close_source_line NUMERIC(5, 2);
ALTER TABLE tracked_bet_leg ADD COLUMN clv_note TEXT;
