"""Derived O/U references end to end against TimescaleDB (skipped without FAIRLINE_TEST_DATABASE_URL):
derive on each poll, copy the derivation at close, and like-for-like CLV in the tracker."""

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
from fairline.ingest.the_odds_api.poll import finalize_closing, tick
from fairline.tracker import service as T

URL = os.environ.get("FAIRLINE_TEST_DATABASE_URL")
pytestmark = [pytest.mark.integration, pytest.mark.skipif(not URL, reason="FAIRLINE_TEST_DATABASE_URL not set")]
FIX = Path(__file__).parent / "fixtures"
CFG = get_config()
KO_A = datetime(2025, 1, 10, 15, 0, tzinfo=UTC)
KO_B = datetime(2025, 1, 10, 17, 30, tzinfo=UTC)


def iso(t):
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


class Api:
    def __init__(self):
        self.now = KO_A - timedelta(hours=30)
        # (id, home, away, kickoff, pinnacle main line as a function of time)
        self.fx = [
            ("evA", "Fulham", "Manchester United", KO_A, lambda t: 3.0),
            ("evB", "Everton", "Arsenal", KO_B, lambda t: 2.5 if t >= KO_B - timedelta(minutes=30) else 2.75),
        ]

    def handler(self, req):
        if req.url.path.split("/")[3] != "soccer_epl":
            return httpx.Response(200, json=[])
        live = [f for f in self.fx if f[3] > self.now]
        if req.url.path.endswith("/events"):
            return httpx.Response(
                200, json=[{"id": i, "commence_time": iso(k), "home_team": h, "away_team": a} for i, h, a, k, _ in live]
            )
        upd = iso(self.now - timedelta(minutes=1))
        body = []
        for i, h, a, k, line_at in live:
            ln = line_at(self.now)
            pin = {"key": "pinnacle", "last_update": upd, "markets": [
                {"key": "h2h", "last_update": upd, "outcomes": [{"name": h, "price": 2.6}, {"name": a, "price": 2.9}, {"name": "Draw", "price": 3.4}]},
                {"key": "totals", "last_update": upd, "outcomes": [{"name": "Over", "price": 1.97, "point": ln}, {"name": "Under", "price": 1.93, "point": ln}]},
            ]}  # fmt: skip
            wh = {"key": "williamhill", "last_update": upd, "markets": [
                {"key": "totals", "last_update": upd, "outcomes": [{"name": "Over", "price": 1.8, "point": 2.5}, {"name": "Under", "price": 1.95, "point": 2.5}]},
            ]}  # fmt: skip
            body.append({"id": i, "commence_time": iso(k), "home_team": h, "away_team": a, "bookmakers": [pin, wh]})
        return httpx.Response(200, json=body, headers={"x-requests-remaining": "400", "x-requests-last": "2"})


class Csv:
    def fetch(self, division, season, force=False):
        return Download((FIX / "E0_2425_sample.csv").read_bytes(), True, False, "fixture", None)


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
    ingest_season(conn, "ENG1", "2024-25", source=Csv())
    api = Api()
    client = OddsApiClient("https://api.example.test/v4", tmp_path_factory.mktemp("c"), key="k",
                           client=httpx.Client(transport=httpx.MockTransport(api.handler)), now=lambda: api.now)  # fmt: skip
    yield conn, api, client
    conn.close()


def eid(conn, ext):
    return conn.execute(
        "SELECT event_id FROM event_external_id WHERE provider='the_odds_api' AND external_id=%s", (ext,)
    ).fetchone()[0]


def test_derive_close_and_like_for_like_clv(env):
    conn, api, client = env
    for t in [KO_A - timedelta(hours=23), KO_A - timedelta(hours=3), KO_A - timedelta(minutes=18),
              KO_B - timedelta(minutes=18)]:  # fmt: skip
        api.now = t
        rep = tick(conn, CFG, client, now=t)
        assert all(p.status == "ok" for p in rep.polls)
    a, b = eid(conn, "evA"), eid(conn, "evB")

    # every pre-close poll derived both events (Pinnacle never quoted 2.5 then)
    lines = conn.execute(
        "SELECT event_id, is_closing, source_line::float FROM odds_derivation ORDER BY event_id, ts"
    ).fetchall()
    assert {(e, ln) for e, c, ln in lines if not c} == {(a, 3.0), (b, 2.75)}

    # bets: over 2.5 at William Hill, each placed after a derived snapshot existed
    bet_a = T.log_bet(conn, CFG, [T.LegIn(a, "OU", "over", 1.8, 2.5)], "williamhill", 10, KO_A - timedelta(hours=2))
    bet_b = T.log_bet(conn, CFG, [T.LegIn(b, "OU", "over", 1.8, 2.5)], "williamhill", 10, KO_B - timedelta(hours=2))
    legs = dict(conn.execute("SELECT bet_id, ref_book FROM tracked_bet_leg").fetchall())
    assert legs == {bet_a: "pinnacle_derived", bet_b: "pinnacle_derived"}

    # B's close poll saw Pinnacle quote 2.5 itself: nothing derived, quoted close instead
    out = finalize_closing(conn, CFG, KO_B + timedelta(minutes=5))
    assert out["closed"] == 1  # A was already closed by the 17:12 tick (after its kickoff)
    closes = conn.execute(
        "SELECT o.event_id, b.code FROM odds_snapshot o JOIN bookmaker b ON b.id = o.bookmaker_id "
        "JOIN market m ON m.id = o.market_id WHERE o.is_closing AND m.code = 'OU' AND b.code LIKE 'pinnacle%%' "
        "AND o.event_id = ANY(%s) GROUP BY 1, 2 ORDER BY 1",
        ([a, b],),
    ).fetchall()
    assert closes == [(a, "pinnacle_derived"), (b, "pinnacle")]
    assert conn.execute(
        "SELECT source_line::float FROM odds_derivation WHERE is_closing AND event_id = %s", (a,)
    ).fetchone() == (3.0,)

    T.refresh(conn, CFG)
    rows = {r[0]: r[1:] for r in conn.execute(
        "SELECT bet_id, ref_source_line::float, close_book, close_source_line::float, clv, clv_note FROM tracked_bet_leg"
    ).fetchall()}  # fmt: skip
    ref_a, cbook_a, csrc_a, clv_a, note_a = rows[bet_a]
    assert (ref_a, cbook_a, csrc_a, note_a) == (3.0, "pinnacle_derived", 3.0, None) and clv_a is not None
    ref_b, cbook_b, _, clv_b, note_b = rows[bet_b]
    assert ref_b == 2.75 and cbook_b is None and clv_b is None
    assert note_b == "no like-for-like close: placed against derived from 2.75, close is quoted"


def test_replay_from_cache_restores_derived_baselines(env):
    conn, api, client = env
    from fairline.ingest.the_odds_api.poll import rederive_from_cache

    n = conn.execute("SELECT count(*) FROM odds_derivation WHERE NOT is_closing").fetchone()[0]
    conn.execute("DELETE FROM odds_derivation WHERE NOT is_closing")
    conn.execute(
        "DELETE FROM odds_snapshot o USING bookmaker b WHERE b.id = o.bookmaker_id AND b.code = 'pinnacle_derived' AND NOT o.is_closing"
    )
    conn.commit()
    files = sorted(client.cache_dir.glob("*/*/*_odds.json"))
    out = rederive_from_cache(conn, CFG, files)
    assert out["derived"] == n and out["inserted"] == 2 * n  # over + under per derivation
    assert rederive_from_cache(conn, CFG, files)["inserted"] == 0  # idempotent


def test_close_copies_only_lines_the_close_poll_priced(env):
    """Pinnacle quotes 2.5 at T-3h, then moves its only line to 2.75 before the close. Its old 2.5
    rows are still the latest on file, but the close poll never priced 2.5: the close must be the
    derived 2.5 (from 2.75), not the stale quoted one stamped with the close poll's time."""
    conn, api, client = env
    from fairline.ingest.the_odds_api.poll import reclose

    ko = KO_B + timedelta(days=1)
    api.fx = [("evC", "Arsenal", "Fulham", ko, lambda t: 2.75 if t >= ko - timedelta(minutes=30) else 2.5)]
    for t in [ko - timedelta(hours=3), ko - timedelta(minutes=18)]:
        api.now = t
        assert all(p.status == "ok" for p in tick(conn, CFG, client, now=t).polls)
    c = eid(conn, "evC")
    bet = T.log_bet(conn, CFG, [T.LegIn(c, "OU", "under", 1.95, 2.5)], "williamhill", 10, ko - timedelta(hours=2))
    assert conn.execute("SELECT ref_book FROM tracked_bet_leg WHERE bet_id = %s", (bet,)).fetchone() == ("pinnacle",)

    def closes():
        return conn.execute(
            "SELECT b.code, m.code, m.line::float FROM odds_snapshot o JOIN bookmaker b ON b.id = o.bookmaker_id "
            "JOIN market m ON m.id = o.market_id WHERE o.is_closing AND o.event_id = %s GROUP BY 1, 2, 3 ORDER BY 1, 2",
            (c,),
        ).fetchall()

    finalize_closing(conn, CFG, ko + timedelta(minutes=5))
    expected = [("pinnacle", "1X2", None), ("pinnacle_derived", "OU", 2.5), ("williamhill", "OU", 2.5)]
    assert closes() == expected
    T.refresh(conn, CFG)
    clv, note = conn.execute("SELECT clv, clv_note FROM tracked_bet_leg WHERE bet_id = %s", (bet,)).fetchone()
    assert clv is None and note == "no like-for-like close: placed against quoted, close is derived from 2.75"

    # polls recorded before `quoted` existed are rebuilt from their cached response
    conn.execute("UPDATE ingestion_run SET stats = stats - 'quoted' WHERE source = 'the_odds_api' AND stats ? 'quoted'")
    conn.commit()
    out = reclose(conn, CFG, ko - timedelta(hours=1), ko + timedelta(minutes=10))
    assert out["events"] == 1 and out["closed"] == 1 and out["legs_reset"] == 1
    assert closes() == expected
