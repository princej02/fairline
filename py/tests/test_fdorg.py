"""football-data.org: parser and client (pure), then a TimescaleDB sync against a mocked API."""

import os
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import psycopg
import pytest

from fairline.core import db
from fairline.core.config import get_config
from fairline.ingest.football_data_co_uk.pipeline import ingest_season
from fairline.ingest.football_data_co_uk.source import Download
from fairline.ingest.football_data_org.client import FdoClient, FdoError
from fairline.ingest.football_data_org.parse import parse_matches
from fairline.ingest.football_data_org.sync import sync_season, sync_window

TOKEN = "secret-fdo-token"
CFG = get_config()
FIX = Path(__file__).parent / "fixtures"


def team(i, name, short):
    return {"id": i, "name": name, "shortName": short, "tla": short[:3].upper()}


MUN, FUL = team(66, "Manchester United FC", "Man United"), team(63, "Fulham FC", "Fulham")
ARS, EVE = team(57, "Arsenal FC", "Arsenal"), team(62, "Everton FC", "Everton")
WOL = team(76, "Wolverhampton Wanderers FC", "Wolverhampton")


def match(mid, utc, status, home, away, ft=None, ht=None, duration="REGULAR", regular=None, matchday=20):
    score = {
        "winner": None,
        "duration": duration,
        "fullTime": {"home": None, "away": None},
        "halfTime": {"home": None, "away": None},
    }
    if ft:
        score["fullTime"] = {"home": ft[0], "away": ft[1]}
    if ht:
        score["halfTime"] = {"home": ht[0], "away": ht[1]}
    if regular:
        score["regularTime"] = {"home": regular[0], "away": regular[1]}
    return {"id": mid, "utcDate": utc, "status": status, "matchday": matchday, "homeTeam": home, "awayTeam": away, "score": score}  # fmt: skip


# ------------------------------------------------------------------ parser
def test_parse_status_kickoff_and_90_minute_scores():
    data = {"matches": [
        match(1, "2025-01-26T14:00:00Z", "TIMED", FUL, MUN),
        match(2, "2025-02-01T00:00:00Z", "SCHEDULED", EVE, ARS),
        match(3, "2025-01-20T20:00:00Z", "FINISHED", ARS, EVE, ft=(3, 2), duration="EXTRA_TIME", regular=(2, 2)),
        match(4, "2025-01-21T20:00:00Z", "POSTPONED", ARS, FUL),
        match(5, "2025-01-22T20:00:00Z", "CANCELLED", EVE, FUL),
        match(6, "2025-01-23T20:00:00Z", "FINISHED", MUN, ARS, ft=(1, 0), ht=(0, 0)),
        match(7, "2025-05-01T20:00:00Z", "TIMED", {"id": None, "name": None}, ARS),  # cup tie, TBD
        match(8, "2025-01-24T20:00:00Z", "WEIRD", MUN, FUL),
    ]}  # fmt: skip
    ms, warnings = parse_matches(data)
    by = {m.external_id: m for m in ms}
    assert set(by) == {"1", "2", "3", "4", "5", "6"}
    assert by["1"].status == "scheduled" and by["1"].kickoff_known
    assert by["2"].status == "scheduled" and not by["2"].kickoff_known  # date fixed, time not
    assert by["3"].score == (2, 2)  # 90 minutes, not the extra-time 3-2
    assert by["4"].status == "postponed" and by["5"].status == "void"
    assert by["6"].score == (1, 0) and by["6"].half_time == (0, 0) and by["6"].home.short == "Man United"
    assert any("WEIRD" in w for w in warnings)


# ------------------------------------------------------------------ client
def test_client_sends_token_in_header_retries_429_and_caches(tmp_path):
    calls, slept = [], []

    def handler(req: httpx.Request):
        calls.append(req)
        if len(calls) == 1:
            return httpx.Response(429, headers={"X-RequestCounter-Reset": "7"}, json={"message": "slow down"})
        return httpx.Response(200, json={"matches": []}, headers={"X-Requests-Available-Minute": "9"})

    c = FdoClient("https://api.example.test/v4", tmp_path, delay=0, token_=TOKEN,
                  client=httpx.Client(transport=httpx.MockTransport(handler)), sleep=slept.append)  # fmt: skip
    r = c.window("PL", date(2025, 1, 25), date(2025, 1, 27))
    assert r.available_minute == 9 and 8.0 in slept
    assert calls[-1].headers["X-Auth-Token"] == TOKEN and TOKEN not in str(calls[-1].url)
    assert calls[-1].url.params["dateFrom"] == "2025-01-25" and calls[-1].url.path.endswith("/competitions/PL/matches")
    assert r.cache_path.exists() and all(TOKEN not in f.read_text() for f in tmp_path.rglob("*.json"))


def test_client_error_is_clean(tmp_path):
    c = FdoClient("https://api.example.test/v4", tmp_path, delay=0, token_=TOKEN,
                  client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(403, json={"message": "restricted"}))))  # fmt: skip
    with pytest.raises(FdoError) as e:
        c.season("SA", 2025)
    assert e.value.status == 403 and "restricted" in str(e.value) and TOKEN not in str(e.value)


# ------------------------------------------------------------------ DB
URL = os.environ.get("FAIRLINE_TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(not URL, reason="FAIRLINE_TEST_DATABASE_URL not set")


class CsvSource:
    def __init__(self, text):
        self.body = text.encode()

    def fetch(self, division, season, force=False):
        return Download(self.body, True, False, "fixture", None)


class FakeFdo:
    def __init__(self):
        self.season_matches = [
            match(1, "2024-08-16T19:00:00Z", "FINISHED", MUN, FUL, ft=(1, 0), ht=(0, 0), matchday=1),
            match(2, "2024-12-14T15:00:00Z", "FINISHED", ARS, EVE, ft=(2, 0), matchday=16),  # sample says 0-0
            match(3, "2025-01-26T14:00:00Z", "TIMED", FUL, MUN, matchday=23),
            match(4, "2025-02-01T00:00:00Z", "SCHEDULED", EVE, ARS, matchday=24),
            match(5, "2025-01-25T15:00:00Z", "POSTPONED", ARS, FUL, matchday=23),
            match(6, "2025-01-25T15:00:00Z", "TIMED", WOL, ARS, matchday=23),
        ]
        self.window_matches = []

    def handler(self, req: httpx.Request):
        assert req.headers["X-Auth-Token"] == TOKEN
        if not req.url.path.endswith("/competitions/PL/matches"):
            return httpx.Response(200, json={"matches": []})
        if "season" in req.url.params:
            assert req.url.params["season"] == "2024"
            return httpx.Response(200, json={"matches": self.season_matches})
        return httpx.Response(200, json={"matches": self.window_matches})


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
    api = FakeFdo()
    client = FdoClient("https://api.example.test/v4", tmp_path_factory.mktemp("fdo"), delay=0, token_=TOKEN,
                       client=httpx.Client(transport=httpx.MockTransport(api.handler)))  # fmt: skip
    yield conn, api, client
    conn.close()


def ev(conn, home, away):
    return conn.execute(
        "SELECT e.id, e.status, e.starts_at, e.kickoff_time_known, e.round FROM event e "
        "JOIN participant h ON h.id = e.home_id JOIN participant a ON a.id = e.away_id WHERE h.name = %s AND a.name = %s",
        (home, away),
    ).fetchall()


@pytest.mark.integration
@needs_db
def test_season_then_window_sync(env):
    conn, api, client = env
    now = datetime(2025, 1, 25, 10, tzinfo=UTC)
    (eng1,) = [r for r in sync_season(conn, CFG, client, now=now) if r.competition == "ENG1"]
    assert not eng1.error and eng1.matches == 6 and eng1.events == 5
    assert (eng1.created, eng1.linked) == (3, 2)  # 1 and 2 are the sample's played matches
    assert eng1.unresolved == ["Wolverhampton Wanderers FC"]
    assert eng1.auto_matched == {}  # short names are identical to ours, so nothing to review
    (d,) = eng1.disagreements
    assert d.endswith("football_data_co_uk 0-0 vs ours 2-0")
    assert eng1.results == 0  # never overwrites football-data.co.uk results

    (fm,) = ev(conn, "Fulham", "Man United")
    assert fm[1:] == ("scheduled", datetime(2025, 1, 26, 14, tzinfo=UTC), True, "23")
    assert ev(conn, "Everton", "Arsenal")[0][3] is False  # SCHEDULED: date only
    assert ev(conn, "Arsenal", "Fulham")[0][1] == "postponed"

    # window: 3 finishes, the postponed game gets a new date, 4 is cancelled
    api.window_matches = [
        match(3, "2025-01-26T14:00:00Z", "FINISHED", FUL, MUN, ft=(2, 1), ht=(1, 0), matchday=23),
        match(5, "2025-03-12T19:45:00Z", "TIMED", ARS, FUL, matchday=23),
        match(4, "2025-02-01T00:00:00Z", "CANCELLED", EVE, ARS, matchday=24),
    ]
    (w,) = [
        r
        for r in sync_window(conn, CFG, client, now=datetime(2025, 1, 26, 16, 5, tzinfo=UTC))
        if r.competition == "ENG1"
    ]
    assert w.results == 1 and w.status_changes == 3 and w.kickoff_changes == 1
    eid = ev(conn, "Fulham", "Man United")[0][0]
    assert conn.execute("SELECT home_goals, away_goals, ht_home, source FROM football_result WHERE event_id = %s", (eid,)).fetchone() == (
        2, 1, 1, "football_data_org")  # fmt: skip
    (af,) = ev(conn, "Arsenal", "Fulham")
    assert af[1:3] == ("scheduled", datetime(2025, 3, 12, 19, 45, tzinfo=UTC))
    assert ev(conn, "Everton", "Arsenal")[0][1] == "void"

    # football-data.co.uk later publishes the match: it links (no duplicate) and its row, with
    # match stats, supersedes ours
    text = (FIX / "E0_2425_sample.csv").read_text(encoding="utf-8-sig")
    text = text.replace("16/08/2024,20:00,Man United,Fulham,1", "26/01/2025,14:00,Fulham,Man United,2")
    r = ingest_season(conn, "ENG1", "2024-25", source=CsvSource(text))
    assert r.load.events_linked == 1 and len(ev(conn, "Fulham", "Man United")) == 1
    assert (
        conn.execute("SELECT source FROM football_result WHERE event_id = %s", (eid,)).fetchone()[0]
        == "football_data_co_uk"
    )
