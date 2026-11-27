-- Fairline core schema. Applied by `fairline db migrate` (tracked in schema_migrations).
CREATE EXTENSION IF NOT EXISTS timescaledb;

-- ===================== Reference =====================
CREATE TABLE sport (
  id    SMALLSERIAL PRIMARY KEY,
  code  TEXT UNIQUE NOT NULL
);
INSERT INTO sport (code) VALUES ('football'), ('nba'), ('nfl'), ('ufc'), ('boxing'), ('f1'), ('racing');

-- Rows are synced from config/competitions.yaml; `enabled` mirrors the allowlist.
CREATE TABLE competition (
  id        SERIAL PRIMARY KEY,
  sport_id  SMALLINT NOT NULL REFERENCES sport(id),
  code      TEXT UNIQUE NOT NULL,
  name      TEXT NOT NULL,
  country   TEXT,
  kind      TEXT NOT NULL CHECK (kind IN ('league', 'cup', 'continental', 'international')),
  providers JSONB NOT NULL DEFAULT '{}',
  enabled   BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE TABLE season (
  id              SERIAL PRIMARY KEY,
  competition_id  INT NOT NULL REFERENCES competition(id),
  label           TEXT NOT NULL,            -- '2025-26'
  starts_on       DATE,
  ends_on         DATE,
  UNIQUE (competition_id, label)
);

-- Teams, fighters, drivers, horses.
CREATE TABLE participant (
  id          SERIAL PRIMARY KEY,
  sport_id    SMALLINT NOT NULL REFERENCES sport(id),
  kind        TEXT NOT NULL CHECK (kind IN ('team', 'person', 'horse')),
  name        TEXT NOT NULL,
  country     TEXT,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Entity resolution: every provider's spelling maps to one participant.
CREATE TABLE participant_alias (
  provider        TEXT NOT NULL,
  sport_id        SMALLINT NOT NULL REFERENCES sport(id),
  alias           TEXT NOT NULL,
  external_id     TEXT,
  participant_id  INT NOT NULL REFERENCES participant(id),
  reviewed        BOOLEAN NOT NULL DEFAULT FALSE,   -- false = auto-created, needs a human look
  PRIMARY KEY (provider, sport_id, alias)
);

CREATE TABLE bookmaker (
  id        SMALLSERIAL PRIMARY KEY,
  code      TEXT UNIQUE NOT NULL,
  name      TEXT NOT NULL,
  kind      TEXT NOT NULL CHECK (kind IN ('book', 'exchange', 'aggregate')),
  is_sharp  BOOLEAN NOT NULL DEFAULT FALSE,
  providers JSONB NOT NULL DEFAULT '{}'
);

CREATE TABLE market (
  id        SERIAL PRIMARY KEY,
  sport_id  SMALLINT NOT NULL REFERENCES sport(id),
  code      TEXT NOT NULL,                  -- '1X2', 'OU', 'AH', 'BTTS'
  line      NUMERIC(5, 2),                  -- NULL for 1X2 / BTTS
  UNIQUE NULLS NOT DISTINCT (sport_id, code, line)
);
INSERT INTO market (sport_id, code, line)
SELECT id, m.code, m.line FROM sport, (VALUES ('1X2', NULL::numeric), ('OU', 2.5)) AS m(code, line)
WHERE sport.code = 'football';

-- ===================== Events =====================
CREATE TABLE event (
  id                  BIGSERIAL PRIMARY KEY,
  season_id           INT NOT NULL REFERENCES season(id),
  starts_at           TIMESTAMPTZ NOT NULL,
  kickoff_time_known  BOOLEAN NOT NULL DEFAULT TRUE,  -- false: date known, time defaulted
  home_id             INT REFERENCES participant(id), -- NULL for multi-runner events
  away_id             INT REFERENCES participant(id),
  venue               TEXT,
  neutral             BOOLEAN NOT NULL DEFAULT FALSE,
  status              TEXT NOT NULL DEFAULT 'scheduled'
                      CHECK (status IN ('scheduled', 'live', 'finished', 'postponed', 'void')),
  round               TEXT,
  updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX event_starts_at_idx ON event (starts_at);
CREATE INDEX event_season_idx ON event (season_id);

CREATE TABLE event_external_id (
  provider     TEXT NOT NULL,
  external_id  TEXT NOT NULL,
  event_id     BIGINT NOT NULL REFERENCES event(id) ON DELETE CASCADE,
  PRIMARY KEY (provider, external_id)
);

CREATE TABLE football_result (
  event_id     BIGINT PRIMARY KEY REFERENCES event(id) ON DELETE CASCADE,
  home_goals   SMALLINT NOT NULL,
  away_goals   SMALLINT NOT NULL,
  ht_home      SMALLINT,
  ht_away      SMALLINT,
  stats        JSONB NOT NULL DEFAULT '{}',  -- shots, corners, cards, referee...
  source       TEXT NOT NULL,
  recorded_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ===================== Odds (time series) =====================
-- Append-only observations. A price is never overwritten by a later price;
-- re-ingesting the same observation is idempotent.
CREATE TABLE odds_snapshot (
  ts            TIMESTAMPTZ NOT NULL,     -- when the price was observable
  ts_estimated  BOOLEAN NOT NULL DEFAULT FALSE,
                -- true: ts is a conservative upper bound (the price was observed no later than ts)
  event_id      BIGINT NOT NULL REFERENCES event(id) ON DELETE CASCADE,
  market_id     INT NOT NULL REFERENCES market(id),
  bookmaker_id  SMALLINT NOT NULL REFERENCES bookmaker(id),
  selection     TEXT NOT NULL,            -- home|draw|away|over|under
  price         NUMERIC(8, 3) NOT NULL CHECK (price > 1),
  is_closing    BOOLEAN NOT NULL DEFAULT FALSE,
  source        TEXT NOT NULL,
  PRIMARY KEY (event_id, market_id, bookmaker_id, selection, is_closing, ts)
);
SELECT create_hypertable('odds_snapshot', by_range('ts', INTERVAL '30 days'));
ALTER TABLE odds_snapshot SET (
  timescaledb.compress,
  timescaledb.compress_segmentby = 'event_id, market_id, bookmaker_id',
  timescaledb.compress_orderby = 'ts DESC'
);
SELECT add_compression_policy('odds_snapshot', INTERVAL '60 days');

-- ===================== Feature store =====================
-- Long format so new feature families (xG later, lineups, rest days...) need no migration.
-- as_of is when the value became knowable: the backtester filters on it to prevent leakage.
CREATE TABLE participant_feature (
  event_id        BIGINT NOT NULL REFERENCES event(id) ON DELETE CASCADE,
  participant_id  INT NOT NULL REFERENCES participant(id),
  feature         TEXT NOT NULL,            -- 'goals_for', 'xg_for', 'rest_days', ...
  value           DOUBLE PRECISION,
  as_of           TIMESTAMPTZ NOT NULL,
  source          TEXT NOT NULL,
  PRIMARY KEY (event_id, participant_id, feature, source)
);
CREATE INDEX participant_feature_lookup ON participant_feature (participant_id, feature, as_of);

-- ===================== Models, predictions, edges =====================
CREATE TABLE model_version (
  id               SERIAL PRIMARY KEY,
  name             TEXT NOT NULL,
  version          TEXT NOT NULL,
  params           JSONB NOT NULL,
  trained_through  TIMESTAMPTZ NOT NULL,   -- training data cutoff: anti-leakage guard
  created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (name, version)
);

CREATE TABLE prediction (
  id                BIGSERIAL PRIMARY KEY,
  model_version_id  INT NOT NULL REFERENCES model_version(id),
  event_id          BIGINT NOT NULL REFERENCES event(id) ON DELETE CASCADE,
  market_id         INT NOT NULL REFERENCES market(id),
  selection         TEXT NOT NULL,
  prob              DOUBLE PRECISION NOT NULL CHECK (prob BETWEEN 0 AND 1),
  prob_lo           DOUBLE PRECISION,
  prob_hi           DOUBLE PRECISION,
  as_of             TIMESTAMPTZ NOT NULL,   -- information cutoff for this prediction
  UNIQUE (model_version_id, event_id, market_id, selection, as_of)
);

CREATE TABLE edge (
  id             BIGSERIAL PRIMARY KEY,
  prediction_id  BIGINT NOT NULL REFERENCES prediction(id) ON DELETE CASCADE,
  bookmaker_id   SMALLINT NOT NULL REFERENCES bookmaker(id),
  price          NUMERIC(8, 3) NOT NULL,
  fair_prob      DOUBLE PRECISION NOT NULL,
  devig_method   TEXT NOT NULL,
  ev             DOUBLE PRECISION NOT NULL,
  kelly_frac     DOUBLE PRECISION NOT NULL,
  computed_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ===================== Manual bet tracking (Fairline never places bets) =====================
CREATE TABLE tracked_bet (
  id            BIGSERIAL PRIMARY KEY,
  placed_at     TIMESTAMPTZ NOT NULL,
  bookmaker_id  SMALLINT REFERENCES bookmaker(id),
  stake         NUMERIC(10, 2) NOT NULL CHECK (stake > 0),
  price         NUMERIC(10, 3) NOT NULL,   -- combined price for accas
  status        TEXT NOT NULL DEFAULT 'open'
                CHECK (status IN ('open', 'won', 'lost', 'void', 'half_won', 'half_lost')),
  pnl           NUMERIC(10, 2),
  notes         TEXT
);

CREATE TABLE tracked_bet_leg (
  bet_id         BIGINT NOT NULL REFERENCES tracked_bet(id) ON DELETE CASCADE,
  leg_no         SMALLINT NOT NULL,
  event_id       BIGINT NOT NULL REFERENCES event(id),
  market_id      INT NOT NULL REFERENCES market(id),
  selection      TEXT NOT NULL,
  price          NUMERIC(8, 3) NOT NULL,
  edge_id        BIGINT REFERENCES edge(id),
  closing_price  NUMERIC(8, 3),            -- filled after kickoff, used for CLV
  PRIMARY KEY (bet_id, leg_no)
);

-- ===================== Backtests =====================
CREATE TABLE backtest_run (
  id                BIGSERIAL PRIMARY KEY,
  model_version_id  INT REFERENCES model_version(id),
  config            JSONB NOT NULL,
  metrics           JSONB,
  started_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  finished_at       TIMESTAMPTZ
);

-- ===================== News (Phase 2; reserved) =====================
CREATE TABLE news_item (
  id            BIGSERIAL PRIMARY KEY,
  source        TEXT NOT NULL,
  url           TEXT UNIQUE,
  published_at  TIMESTAMPTZ,
  title         TEXT,
  body          TEXT,
  fetched_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE news_signal (
  id              BIGSERIAL PRIMARY KEY,
  news_item_id    BIGINT REFERENCES news_item(id) ON DELETE CASCADE,
  event_id        BIGINT REFERENCES event(id),
  participant_id  INT REFERENCES participant(id),
  kind            TEXT NOT NULL,           -- injury|suspension|lineup|weather|other
  payload         JSONB NOT NULL,
  confidence      REAL,
  extracted_by    TEXT
);

-- ===================== Ops =====================
CREATE TABLE ingestion_run (
  id             BIGSERIAL PRIMARY KEY,
  source         TEXT NOT NULL,
  scope          TEXT NOT NULL,            -- e.g. 'ENG1:2024-25'
  started_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  finished_at    TIMESTAMPTZ,
  status         TEXT NOT NULL DEFAULT 'running' CHECK (status IN ('running', 'ok', 'skipped', 'failed')),
  stats          JSONB NOT NULL DEFAULT '{}',
  error          TEXT
);
