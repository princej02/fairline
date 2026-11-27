"""Runs the full pipeline against a real TimescaleDB. Skipped unless FAIRLINE_TEST_DATABASE_URL is set.

docker compose -f infra/docker-compose.yml --env-file .env up -d db
FAIRLINE_TEST_DATABASE_URL=postgresql://fairline:...@127.0.0.1:5433/fairline_test uv run pytest -m integration
"""

import os
from pathlib import Path

import psycopg
import pytest

from fairline.core import db
from fairline.core.config import get_config
from fairline.ingest.football_data_co_uk.pipeline import ingest_season
from fairline.ingest.football_data_co_uk.source import Download

URL = os.environ.get("FAIRLINE_TEST_DATABASE_URL")
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not URL, reason="FAIRLINE_TEST_DATABASE_URL not set"),
]
FIX = Path(__file__).parent / "fixtures"


class FixtureSource:
    """Stands in for the HTTP source; serves the sample CSVs."""

    files = {("E0", "2024-25"): "E0_2425_sample.csv", ("E0", "2015-16"): "E0_1516_sample.csv"}

    def fetch(self, division, season, force=False):
        return Download((FIX / self.files[(division, season)]).read_bytes(), True, False, "fixture", None)


@pytest.fixture
def conn():
    # Reset on its own session: TimescaleDB can't be re-created in a session that already loaded it.
    with psycopg.connect(URL, autocommit=True) as c:
        c.execute("DROP SCHEMA public CASCADE")
        c.execute("DROP EXTENSION IF EXISTS timescaledb CASCADE")
        c.execute("CREATE SCHEMA public")
    with psycopg.connect(URL) as c:
        db.migrate(c)
        db.sync_reference(c, get_config())
        c.commit()
        yield c


def counts(c):
    return {
        t: c.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
        for t in ("participant", "event", "football_result", "odds_snapshot")
    }


def test_ingest_is_idempotent(conn):
    src = FixtureSource()
    r1 = ingest_season(conn, "ENG1", "2024-25", source=src)
    assert r1.load.events_created == 2 and r1.load.teams_created == 4
    first = counts(conn)
    assert first == {"participant": 4, "event": 2, "football_result": 2, "odds_snapshot": r1.odds_rows_parsed}

    r2 = ingest_season(conn, "ENG1", "2024-25", source=src)
    assert r2.load.events_created == 0 and r2.load.odds_inserted == 0 and r2.load.events_updated == 0
    assert counts(conn) == first


def test_teams_shared_across_seasons(conn):
    src = FixtureSource()
    ingest_season(conn, "ENG1", "2024-25", source=src)
    ingest_season(conn, "ENG1", "2015-16", source=src)
    # 4 + 4 distinct names, none overlapping in these fixtures
    assert counts(conn)["participant"] == 8
    seasons = conn.execute("SELECT label FROM season ORDER BY label").fetchall()
    assert [s[0] for s in seasons] == ["2015-16", "2024-25"]


def test_closing_pinnacle_queryable(conn):
    ingest_season(conn, "ENG1", "2024-25", source=FixtureSource())
    rows = conn.execute(
        """
        SELECT o.selection, o.price::float FROM odds_snapshot o
        JOIN bookmaker b ON b.id = o.bookmaker_id JOIN market m ON m.id = o.market_id
        JOIN event e ON e.id = o.event_id JOIN participant h ON h.id = e.home_id
        WHERE b.code = 'pinnacle' AND m.code = '1X2' AND o.is_closing AND h.name = 'Man United'
        ORDER BY o.selection
        """
    ).fetchall()
    assert rows == [("away", 6.4), ("draw", 4.7), ("home", 1.55)]


def test_audit_row_written(conn):
    ingest_season(conn, "ENG1", "2024-25", source=FixtureSource())
    status, scope = conn.execute("SELECT status, scope FROM ingestion_run").fetchone()
    assert (status, scope) == ("ok", "ENG1:2024-25")


def test_walkforward_persist_is_idempotent(conn):
    """Insert a simulated two-tier league as real events, run walk-forward, persist twice."""
    from datetime import UTC, datetime

    from fairline.models.football.data import load_matches
    from fairline.models.football.store import persist
    from fairline.models.football.walkforward import walk_forward
    from tests.sim import two_tier_country

    sim = two_tier_country(seed=2, seasons=2)[0]
    sport = conn.execute("SELECT id FROM sport WHERE code='football'").fetchone()[0]
    ids, seasons = {}, {}
    for t in sorted(set(sim["home_id"]) | set(sim["away_id"])):
        ids[t] = conn.execute(
            "INSERT INTO participant (sport_id, kind, name) VALUES (%s,'team',%s) RETURNING id", (sport, f"Sim {t}")
        ).fetchone()[0]
    comp_map = {"T1": "ENG1", "T2": "ENG2"}
    for r in sim.iter_rows(named=True):
        key = (comp_map[r["competition"]], r["season"])
        if key not in seasons:
            seasons[key] = conn.execute(
                "INSERT INTO season (competition_id, label) SELECT id, %s FROM competition WHERE code=%s RETURNING id",
                (r["season"], key[0]),
            ).fetchone()[0]
        eid = conn.execute(
            "INSERT INTO event (season_id, starts_at, home_id, away_id, status) "
            "VALUES (%s,%s,%s,%s,'finished') RETURNING id",
            (seasons[key], r["starts_at"], ids[r["home_id"]], ids[r["away_id"]]),
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO football_result (event_id, home_goals, away_goals, source) VALUES (%s,%s,%s,'sim')",
            (eid, r["home_goals"], r["away_goals"]),
        )
    conn.commit()

    cfg = get_config().settings.models
    matches = load_matches(conn, ["ENG1", "ENG2"])
    res = walk_forward(
        matches, "ENG", ["ENG1", "ENG2"], cfg.dixon_coles, cfg.walkforward,
        start=datetime(2021, 8, 1, tzinfo=UTC), end=datetime(2021, 9, 1, tzinfo=UTC),
    )  # fmt: skip
    first = persist(conn, res, cfg.dixon_coles.model_dump())
    persist(conn, res, cfg.dixon_coles.model_dump())
    n_pred = conn.execute("SELECT count(*) FROM prediction").fetchone()[0]
    n_ver = conn.execute("SELECT count(*) FROM model_version").fetchone()[0]
    assert n_pred == first["predictions"] == res.predictions.height * 5
    assert n_ver == len(res.fits)
    leaked = conn.execute(
        "SELECT count(*) FROM prediction p JOIN event e ON e.id = p.event_id WHERE p.as_of >= e.starts_at"
    ).fetchone()[0]
    assert leaked == 0
    # Stored rates reproduce the in-memory markets exactly, so BTTS/AH can be rebuilt later.
    from fairline.models.football.recompute import derive, load_rates

    assert first["rates"] == res.predictions.height
    sample = res.predictions.head(5)
    rates = load_rates(conn, sample["event_id"].to_list(), res.config_hash)
    assert rates.height == 5
    for want, got in zip(sample.sort("event_id").iter_rows(named=True), rates.iter_rows(named=True), strict=True):
        d = derive(got["lambda_home"], got["lambda_away"], got["rho"], got["max_goals"], ah_line=-0.25)
        assert d["btts_yes"] == pytest.approx(want["p_btts"], abs=1e-12)
        assert d["1x2_home"] == pytest.approx(want["p_home"], abs=1e-12)
        assert d["ah_home_fair_odds"] > 1
