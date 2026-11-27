"""API tests against a real TimescaleDB (skipped unless FAIRLINE_TEST_DATABASE_URL is set).

Seeds the sample Premier League CSV (2 matches with pre-match and closing odds), then
exercises the tracker end to end: log a bet, check execution edge against the sharp price at
placement, refresh to fill closing CLV and settle, and summarise.
"""

import os
from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient

from fairline.api.app import app, get_conn
from fairline.core import db
from fairline.core.config import get_config
from fairline.ingest.football_data_co_uk.pipeline import ingest_season
from fairline.ingest.football_data_co_uk.source import Download

URL = os.environ.get("FAIRLINE_TEST_DATABASE_URL")
pytestmark = [pytest.mark.integration, pytest.mark.skipif(not URL, reason="FAIRLINE_TEST_DATABASE_URL not set")]
FIX = Path(__file__).parent / "fixtures"


class FixtureSource:
    def fetch(self, division, season, force=False):
        return Download((FIX / "E0_2425_sample.csv").read_bytes(), True, False, "fixture", None)


@pytest.fixture(scope="module")
def client():
    with psycopg.connect(URL, autocommit=True) as c:
        c.execute("DROP SCHEMA public CASCADE")
        c.execute("DROP EXTENSION IF EXISTS timescaledb CASCADE")
        c.execute("CREATE SCHEMA public")
    with psycopg.connect(URL) as c:
        db.migrate(c)
        db.sync_reference(c, get_config())
        c.commit()
        ingest_season(c, "ENG1", "2024-25", source=FixtureSource())

    def conn():
        with psycopg.connect(URL) as c:
            yield c

    app.dependency_overrides[get_conn] = conn
    yield TestClient(app)
    app.dependency_overrides.clear()


def event_id(name: str) -> int:
    with psycopg.connect(URL) as c:
        return c.execute(
            "SELECT e.id FROM event e JOIN participant h ON h.id = e.home_id WHERE h.name = %s", (name,)
        ).fetchone()[0]


def test_settings_and_health(client):
    assert client.get("/api/health").json() == {"ok": True}
    s = client.get("/api/settings").json()
    assert s["default_strategy"] == "model_beats_ref" and "pinnacle" not in s["bettable_books"]


def test_board_without_model_predictions_is_empty_not_an_error(client):
    r = client.get("/api/board", params={"pool": "ENG", "date": "2024-08-16"})
    assert r.status_code == 200 and r.json()["rows"] == []
    assert client.get("/api/board", params={"pool": "XXX", "date": "2024-08-16"}).status_code == 404


def test_odds_series_marks_estimated_timestamps(client):
    series = client.get(f"/api/events/{event_id('Man United')}/odds", params={"market": "1X2"}).json()
    pin = [s for s in series if s["bookmaker"] == "pinnacle"]
    assert pin and all(s["is_reference"] for s in pin)
    assert all(p["ts_estimated"] for s in series for p in s["points"])


def test_tracker_execution_edge_then_clv_and_settlement(client):
    eid = event_id("Man United")  # kickoff 2024-08-16 19:00 UTC; result 1-0
    body = {
        "legs": [{"event_id": eid, "market": "1X2", "selection": "home", "price": 1.66}],
        "bookmaker": "bet365",
        "stake": 20,
        "placed_at": "2024-08-16T18:00:00Z",
        "strategy": "reference",
        "source": "board",
        "suggested_stake_pct": 0.01,
    }
    r = client.post("/api/bets", json=body)
    assert r.status_code == 201, r.text
    leg = r.json()["legs"][0]
    # Pinnacle pre-match 1.63 / 4.36 / 5.62, devigged with power -> the fair home probability
    assert leg["ref_book"] == "pinnacle" and 0.58 < leg["ref_fair_prob"] < 0.62
    assert leg["execution_edge"] == pytest.approx(1.66 * leg["ref_fair_prob"] - 1)
    # Created after the match, so refresh has already settled it and filled closing CLV.
    bet = r.json()
    assert bet["status"] == "won" and bet["pnl"] == pytest.approx(20 * 0.66)
    assert leg["close_fair_prob"] is not None and leg["clv"] == pytest.approx(1.66 * leg["close_fair_prob"] - 1)

    s = client.get("/api/tracker/summary").json()
    assert s["bets"] == 1 and s["settled"] == 1 and s["clv"]["n"] == 1


def test_tracker_rejects_post_kickoff_and_unknown_book(client):
    eid = event_id("Arsenal")
    base = {"legs": [{"event_id": eid, "market": "1X2", "selection": "home", "price": 1.3}], "stake": 5}
    late = client.post("/api/bets", json=base | {"bookmaker": "bet365", "placed_at": "2024-12-14T15:30:00Z"})
    assert late.status_code == 422 and "kicked off" in late.text
    bad = client.post("/api/bets", json=base | {"bookmaker": "nope", "placed_at": "2024-12-14T10:00:00Z"})
    assert bad.status_code == 422


def test_betfair_commission_applied_to_logged_exchange_bet(client):
    eid = event_id("Arsenal")
    r = client.post(
        "/api/bets",
        json={
            "legs": [{"event_id": eid, "market": "1X2", "selection": "draw", "price": 7.0}],
            "bookmaker": "betfair_ex_uk",
            "stake": 5,
            "placed_at": "2024-12-14T13:30:00Z",
        },
    )
    leg = r.json()["legs"][0]
    c = get_config().settings.edge.betfair_commission
    assert leg["effective_price"] == pytest.approx(1 + 6 * (1 - c))
    assert r.json()["status"] == "won"  # 0-0: the draw wins
    assert r.json()["pnl"] == pytest.approx(5 * 6 * (1 - c))  # paid after commission
