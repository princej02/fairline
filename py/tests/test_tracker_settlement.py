"""Tracker settlement edge cases against TimescaleDB (skipped without FAIRLINE_TEST_DATABASE_URL):
accas fill per-leg CLV early but stay open (and count as exposure) until decided; postponed,
rearranged and cancelled fixtures void instead of leaving a bet open forever."""

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest

from fairline.core import db
from fairline.core.config import get_config
from fairline.edge.data import open_exposure_pct
from fairline.ingest.football_data_co_uk.pipeline import ingest_season
from fairline.ingest.football_data_co_uk.source import Download
from fairline.tracker import service as T

URL = os.environ.get("FAIRLINE_TEST_DATABASE_URL")
pytestmark = [pytest.mark.integration, pytest.mark.skipif(not URL, reason="FAIRLINE_TEST_DATABASE_URL not set")]
FIX = Path(__file__).parent / "fixtures"
CFG = get_config()
BANK = CFG.settings.tracker.bankroll
SAT = datetime(2025, 3, 1, 15, 0, tzinfo=UTC)
SUN = SAT + timedelta(days=1)
H = timedelta(hours=1)


class Csv:
    def fetch(self, division, season, force=False):
        return Download((FIX / "E0_2425_sample.csv").read_bytes(), True, False, "fixture", None)


@pytest.fixture(scope="module")
def conn():
    with psycopg.connect(URL, autocommit=True) as c:
        c.execute("DROP SCHEMA public CASCADE")
        c.execute("DROP EXTENSION IF EXISTS timescaledb CASCADE")
        c.execute("CREATE SCHEMA public")
    c = psycopg.connect(URL)
    db.migrate(c)
    db.sync_reference(c, CFG)
    c.commit()
    ingest_season(c, "ENG1", "2024-25", source=Csv())
    yield c
    c.close()


@pytest.fixture(autouse=True)
def clean(conn):
    conn.execute("DELETE FROM tracked_bet")
    conn.commit()


def team(conn, name):
    return conn.execute("SELECT id FROM participant WHERE name = %s", (name,)).fetchone()[0]


_pairs = iter([("Fulham", "Arsenal"), ("Everton", "Man United"), ("Arsenal", "Man United"), ("Fulham", "Everton"),
               ("Everton", "Fulham"), ("Man United", "Arsenal"), ("Arsenal", "Fulham"), ("Man United", "Everton"), ("Everton", "Arsenal"), ("Fulham", "Man United")])  # fmt: skip


def fixture(conn, ko):
    home, away = next(_pairs)
    season = conn.execute("SELECT season_id FROM event LIMIT 1").fetchone()[0]
    eid = conn.execute(
        "INSERT INTO event (season_id, starts_at, kickoff_time_known, home_id, away_id, status) "
        "VALUES (%s, %s, true, %s, %s, 'scheduled') RETURNING id",
        (season, ko, team(conn, home), team(conn, away)),
    ).fetchone()[0]
    conn.commit()
    return eid


def finish(conn, eid, hg, ag):
    conn.execute("UPDATE event SET status = 'finished' WHERE id = %s", (eid,))
    conn.execute(
        "INSERT INTO football_result (event_id, home_goals, away_goals, source) VALUES (%s, %s, %s, 'test')",
        (eid, hg, ag),
    )
    conn.commit()


def close_1x2(conn, eid, ts):
    for sel, price in (("home", 2.0), ("draw", 3.6), ("away", 4.0)):
        conn.execute(
            "INSERT INTO odds_snapshot (ts, ts_estimated, event_id, market_id, bookmaker_id, selection, price, "
            "is_closing, source) SELECT %s, false, %s, m.id, b.id, %s, %s, true, 'test' FROM market m, bookmaker b "
            "WHERE m.code = '1X2' AND b.code = 'pinnacle'",
            (ts, eid, sel, price),
        )
    conn.commit()


def bet(conn, legs, stake=10.0):
    return T.log_bet(conn, CFG, [T.LegIn(e, "1X2", "home", p) for e, p in legs], "williamhill", stake, SAT - 3 * H)


def state(conn, bet_id):
    st, pnl = conn.execute("SELECT status, pnl::float FROM tracked_bet WHERE id = %s", (bet_id,)).fetchone()
    legs = conn.execute(
        "SELECT result, clv IS NOT NULL FROM tracked_bet_leg WHERE bet_id = %s ORDER BY leg_no", (bet_id,)
    ).fetchall()
    return st, pnl, legs


def test_acca_fills_leg_clv_early_and_stays_open_until_the_last_leg(conn):
    sat, sun = fixture(conn, SAT), fixture(conn, SUN)
    b = bet(conn, [(sat, 2.1), (sun, 1.8)])
    close_1x2(conn, sat, SAT - 10 * timedelta(minutes=1))

    T.refresh(conn, CFG, now=SAT + 5 * timedelta(minutes=1))  # Saturday kicked off, close known
    assert state(conn, b) == ("open", None, [(None, True), (None, False)])

    finish(conn, sat, 2, 0)
    T.refresh(conn, CFG, now=SAT + 3 * H)
    assert state(conn, b) == ("open", None, [("won", True), (None, False)])
    assert open_exposure_pct(conn, BANK) == pytest.approx(10 / BANK)  # stake still committed

    finish(conn, sun, 1, 0)
    T.refresh(conn, CFG, now=SUN + 3 * H)
    st, pnl, _ = state(conn, b)
    assert st == "won" and pnl == pytest.approx(10 * (2.1 * 1.8 - 1))
    assert open_exposure_pct(conn, BANK) == 0


def test_acca_with_a_lost_leg_settles_lost_at_once(conn):
    # The bookmaker settles it too: no later leg can change a lost acca.
    sat, sun = fixture(conn, SAT), fixture(conn, SUN)
    b = bet(conn, [(sat, 2.1), (sun, 1.8)])
    finish(conn, sat, 0, 1)
    T.refresh(conn, CFG, now=SAT + 3 * H)
    assert state(conn, b)[:2] == ("lost", -10.0)
    assert open_exposure_pct(conn, BANK) == 0


def test_postponed_voids_after_the_window_and_releases_exposure(conn):
    ev = fixture(conn, SAT)
    b = bet(conn, [(ev, 2.0)])
    conn.execute("UPDATE event SET status = 'postponed' WHERE id = %s", (ev,))
    conn.commit()
    T.refresh(conn, CFG, now=SAT + 2 * H)  # may still be rearranged inside the window
    assert state(conn, b)[0] == "open"
    T.refresh(conn, CFG, now=SAT + 49 * H)
    assert state(conn, b) == ("void", 0.0, [("void", False)])
    assert open_exposure_pct(conn, BANK) == 0


def test_rearranged_outside_the_window_voids_at_once(conn):
    ev = fixture(conn, SAT)
    b = bet(conn, [(ev, 2.0)])
    conn.execute("UPDATE event SET starts_at = %s WHERE id = %s", (SAT + timedelta(days=5), ev))  # football-data.org
    conn.commit()
    T.refresh(conn, CFG, now=SAT + 2 * H)
    assert state(conn, b)[0] == "void"


def test_rearranged_inside_the_window_settles_normally(conn):
    ev = fixture(conn, SAT)
    b = bet(conn, [(ev, 2.0)])
    conn.execute("UPDATE event SET status = 'postponed' WHERE id = %s", (ev,))
    conn.commit()
    T.refresh(conn, CFG, now=SAT + 2 * H)
    conn.execute("UPDATE event SET status = 'scheduled', starts_at = %s WHERE id = %s", (SAT + 24 * H, ev))
    conn.commit()
    finish(conn, ev, 1, 0)
    T.refresh(conn, CFG, now=SAT + 50 * H)  # past the original window, but it was played inside it
    assert state(conn, b)[:2] == ("won", 10.0)


def test_cancelled_leg_voids_and_the_acca_runs_on_the_rest(conn):
    sat, sun = fixture(conn, SAT), fixture(conn, SUN)
    b = bet(conn, [(sat, 2.1), (sun, 1.8)])
    conn.execute("UPDATE event SET status = 'void' WHERE id = %s", (sat,))  # football-data.org CANCELLED
    conn.commit()
    T.refresh(conn, CFG, now=SAT + H)
    assert state(conn, b)[0] == "open"
    finish(conn, sun, 3, 1)
    T.refresh(conn, CFG, now=SUN + 3 * H)
    st, pnl, legs = state(conn, b)
    assert st == "won" and pnl == pytest.approx(10 * (1.8 - 1)) and [r for r, _ in legs] == ["void", "won"]
