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
