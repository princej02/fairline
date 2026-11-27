"""Live-odds poller against a real TimescaleDB (skipped unless FAIRLINE_TEST_DATABASE_URL is set).

Fake API via httpx.MockTransport. Seeds the 2024-25 EPL sample (Man United, Fulham, Arsenal,
Everton) so provider names have candidates, then: resolve names and fixtures, store snapshots,
mark a closing price only when a poll landed near kickoff, respect the budget, keep the key out
of the DB, and let a later football-data.co.uk load link to the poller's event, not duplicate it.
"""

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import psycopg
import pytest

from fairline.core import db
from fairline.core.config import get_config
from fairline.ingest.football_data_co_uk.pipeline import ingest_season
from fairline.ingest.football_data_co_uk.source import Download
from fairline.ingest.the_odds_api.client import OddsApiClient
from fairline.ingest.the_odds_api.poll import finalize_closing, ledger, tick

URL = os.environ.get("FAIRLINE_TEST_DATABASE_URL")
pytestmark = [pytest.mark.integration, pytest.mark.skipif(not URL, reason="FAIRLINE_TEST_DATABASE_URL not set")]
FIX = Path(__file__).parent / "fixtures"
KEY = "secret-integration-key"
CFG = get_config()
KO_A = datetime(2025, 1, 10, 15, 0, tzinfo=UTC)  # Fulham v Man United (reverse of the sample fixture)
KO_B = datetime(2025, 1, 11, 15, 0, tzinfo=UTC)  # Everton v Arsenal


class CsvSource:
    def __init__(self, text: str):
        self.body = text.encode()

    def fetch(self, division, season, force=False):
        return Download(self.body, True, False, "fixture", None)


def _iso(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def _book(key, upd, home, away, h2h, ou):
    return {"key": key, "title": key, "last_update": _iso(upd), "markets": [
        {"key": "h2h", "last_update": _iso(upd),
         "outcomes": [{"name": home, "price": h2h[0]}, {"name": away, "price": h2h[1]}, {"name": "Draw", "price": h2h[2]}]},
        {"key": "totals", "last_update": _iso(upd),
         "outcomes": [{"name": "Over", "price": ou[0], "point": 2.5}, {"name": "Under", "price": ou[1], "point": 2.5}]},
    ]}  # fmt: skip


class FakeApi:
    """Serves /events and /odds for the EPL only; other leagues have no fixtures."""

    def __init__(self):
        self.now = KO_A - timedelta(hours=30)
        self.odds_calls = 0
        self.remaining = 400

    def fixtures(self):
        return [("evA", "Fulham", "Manchester United", KO_A), ("evB", "Everton", "Arsenal", KO_B),
                ("evC", "Wolverhampton Wanderers", "Arsenal", KO_B + timedelta(days=7))]  # fmt: skip

    def handler(self, req: httpx.Request):
        assert req.url.params["apiKey"] == KEY
        sport = req.url.path.split("/")[3]
        if sport != "soccer_epl":
            return httpx.Response(200, json=[])
        live = [f for f in self.fixtures() if f[3] > self.now]
        if req.url.path.endswith("/events"):
            body = [
                {"id": i, "sport_key": sport, "commence_time": _iso(t), "home_team": h, "away_team": a}
                for i, h, a, t in live
            ]
            return httpx.Response(200, json=body)
        self.odds_calls += 1
        self.remaining -= 2
        drift = 0.01 * self.odds_calls  # prices move between polls, so each poll is a new snapshot
        upd = self.now - timedelta(minutes=2)
        body = [{"id": i, "sport_key": sport, "commence_time": _iso(t), "home_team": h, "away_team": a, "bookmakers": [
            _book("pinnacle", upd, h, a, (2.9 + drift, 2.5, 3.4), (1.9, 1.95)),
            _book("betfair_ex_uk", upd, h, a, (3.0 + drift, 2.56, 3.5), (1.95, 2.0)),
            _book("williamhill", upd, h, a, (2.8, 2.4, 3.3), (1.85, 1.9)),
        ]} for i, h, a, t in live]  # fmt: skip
        return httpx.Response(200, json=body, headers={"x-requests-remaining": str(self.remaining),
                                                       "x-requests-used": str(500 - self.remaining), "x-requests-last": "2"})  # fmt: skip


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    with psycopg.connect(URL, autocommit=True) as c:
        c.execute("DROP SCHEMA public CASCADE")
        c.execute("DROP EXTENSION IF EXISTS timescaledb CASCADE")
        c.execute("CREATE SCHEMA public")
    conn = psycopg.connect(URL)
    db.migrate(conn)
    db.sync_reference(conn, CFG)
    conn.commit()
    ingest_season(
        conn, "ENG1", "2024-25", source=CsvSource((FIX / "E0_2425_sample.csv").read_text(encoding="utf-8-sig"))
    )
    api = FakeApi()
    client = OddsApiClient("https://api.example.test/v4", tmp_path_factory.mktemp("cache"), key=KEY,
                           client=httpx.Client(transport=httpx.MockTransport(api.handler)), now=lambda: api.now)  # fmt: skip
    yield conn, api, client
    conn.close()


def _at(api, conn, client, t):
    api.now = t
    return tick(conn, CFG, client, now=t)


def test_full_cycle(env):
    conn, api, client = env

    # T-30h: nothing in a window yet -> free fixture calls only, but the fresh fixture list is
    # synced for free: names resolve, events are created, Wolves lands in review before any spend
    rep = _at(api, conn, client, KO_A - timedelta(hours=30))
    assert rep.polls == [] and api.odds_calls == 0
    (sync,) = [x for x in rep.syncs if x.fixtures]  # other leagues have no fixtures in the fake API
    assert (sync.competition, sync.fixtures, sync.events, sync.created) == ("ENG1", 3, 2, 2)
    assert sync.auto_matched == {"Manchester United": "Man United"}  # the other three are exact
    assert sync.unresolved == ["Wolverhampton Wanderers"]
    review = conn.execute("SELECT alias, competition FROM participant_alias_review").fetchall()
    assert review == [("Wolverhampton Wanderers", "ENG1")]

    # T-23h: A's T-24h window -> one EPL paid poll onto the events the sync already made
    rep = _at(api, conn, client, KO_A - timedelta(hours=23))
    (p,) = rep.polls
    assert (p.competition, p.kind, p.status, p.credits) == ("ENG1", "window", "ok", 2)
    assert p.unresolved == ["Wolverhampton Wanderers"] and p.events == 2 and p.created == 0
    a_id = conn.execute(
        "SELECT event_id FROM event_external_id WHERE provider = 'the_odds_api' AND external_id = 'evA'"
    ).fetchone()[0]
    season, ts_est = conn.execute(
        "SELECT s.label, bool_or(o.ts_estimated) FROM event e JOIN season s ON s.id = e.season_id "
        "JOIN odds_snapshot o ON o.event_id = e.id WHERE e.id = %s GROUP BY 1", (a_id,)
    ).fetchone()  # fmt: skip
    assert season == "2024-25" and ts_est is False

    # T-3h and the close window for A
    _at(api, conn, client, KO_A - timedelta(hours=3))
    rep = _at(api, conn, client, KO_A - timedelta(minutes=18))
    assert [(p.kind, p.status) for p in rep.polls] == [("close", "ok")]
    assert api.odds_calls == 3

    # after kickoff: A gets closing prices stamped with the close poll's time
    rep = _at(api, conn, client, KO_A + timedelta(minutes=2))
    assert rep.closing["closed"] == 1
    rows = conn.execute(
        "SELECT b.code, o.selection, o.price::float, o.ts FROM odds_snapshot o JOIN bookmaker b ON b.id = o.bookmaker_id "
        "JOIN market m ON m.id = o.market_id WHERE o.event_id = %s AND o.is_closing AND m.code = '1X2' ORDER BY 1, 2",
        (a_id,),
    ).fetchall()
    assert {r[0] for r in rows} == {"pinnacle", "betfair_ex_uk", "williamhill"}
    assert all(r[3] == KO_A - timedelta(minutes=18) for r in rows)
    assert dict(((b, s), p) for b, s, p, _ in rows)[("pinnacle", "home")] == pytest.approx(2.93)  # 3rd poll's price

    # credits ledger matches the API's own count; the key is nowhere in the DB
    led = ledger(conn, KO_A + timedelta(minutes=2))
    assert led.spent_month == 2 * api.odds_calls and led.remaining == api.remaining
    assert led.close_7d == 2  # one closing poll
    dump = json.dumps(conn.execute("SELECT stats, error FROM ingestion_run").fetchall(), default=str)
    assert KEY not in dump


def test_no_closing_price_without_a_poll_near_kickoff(env):
    conn, api, client = env
    # B: polls happened at its T-24h/T-3h only if windows fired; force the issue by jumping past kickoff
    api.now = KO_B + timedelta(minutes=5)
    out = finalize_closing(conn, CFG, api.now)
    b_id = conn.execute(
        "SELECT event_id FROM event_external_id WHERE provider = 'the_odds_api' AND external_id = 'evB'"
    ).fetchone()[0]
    n = conn.execute("SELECT count(*) FROM odds_snapshot WHERE event_id = %s AND is_closing", (b_id,)).fetchone()[0]
    assert n == 0 and out["no_close_poll"] >= 1


def test_budget_blocks_polls(env):
    conn, api, client = env
    t = datetime(2025, 1, 20, 12, tzinfo=UTC)
    conn.execute(
        "INSERT INTO ingestion_run (source, scope, started_at, status, stats) VALUES ('the_odds_api', 'odds:manual', %s, 'ok', %s)",
        (t - timedelta(hours=1), json.dumps({"credits": 489, "remaining": 11})),
    )
    conn.commit()
    api.now = t
    rep = tick(conn, CFG, client, now=t, force=["ENG1"])
    assert [(p.status, "reserve" in p.reason) for p in rep.polls] == [("skipped", True)]


def test_fdcuk_links_to_the_pollers_event_instead_of_duplicating(env):
    conn, api, client = env
    a_id = conn.execute(
        "SELECT event_id FROM event_external_id WHERE provider = 'the_odds_api' AND external_id = 'evA'"
    ).fetchone()[0]
    text = (FIX / "E0_2425_sample.csv").read_text(encoding="utf-8-sig")
    text = text.replace("16/08/2024,20:00,Man United,Fulham", "10/01/2025,15:00,Fulham,Man United")
    r = ingest_season(conn, "ENG1", "2024-25", source=CsvSource(text))
    assert r.load.events_linked == 1
    n = conn.execute(
        "SELECT count(*) FROM event e JOIN participant h ON h.id = e.home_id JOIN participant a ON a.id = e.away_id "
        "WHERE h.name = 'Fulham' AND a.name = 'Man United'"
    ).fetchone()[0]
    assert n == 1
    status, providers = conn.execute(
        "SELECT e.status, array_agg(x.provider ORDER BY x.provider) FROM event e JOIN event_external_id x ON x.event_id = e.id "
        "WHERE e.id = %s GROUP BY 1", (a_id,)
    ).fetchone()  # fmt: skip
    assert status == "finished" and providers == ["football_data_co_uk", "the_odds_api"]
