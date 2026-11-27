-- P1.1-7: live odds from The Odds API.

-- Provider team names we could not resolve with confidence. Their fixtures are skipped (no odds
-- stored) until someone maps the name: `fairline odds alias add "<provider name>" "<our name>"`.
CREATE TABLE participant_alias_review (
  provider        TEXT NOT NULL,
  sport_id        SMALLINT NOT NULL REFERENCES sport(id),
  alias           TEXT NOT NULL,
  competition     TEXT NOT NULL,
  candidates      TEXT[] NOT NULL DEFAULT '{}',   -- our names it plausibly matches (ambiguous) or none
  first_seen      TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_seen       TIMESTAMPTZ NOT NULL DEFAULT now(),
  times_seen      INT NOT NULL DEFAULT 1,
  PRIMARY KEY (provider, sport_id, alias)
);

-- Poll history doubles as the credit ledger and as proof of when a closing snapshot was taken.
CREATE INDEX ingestion_run_source_scope_idx ON ingestion_run (source, scope, started_at DESC);
