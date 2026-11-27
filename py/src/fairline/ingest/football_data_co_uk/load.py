"""Idempotent upsert of a parsed football-data.co.uk season into Postgres.

Re-running the same file changes nothing. Re-running an updated current-season file adds new
matches, fills in results, and adds odds rows that were not there before.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import polars as pl
import psycopg

from fairline.core.config import Competition
from fairline.ingest.football_data_co_uk.parse import ParsedSeason

PROVIDER = "football_data_co_uk"


@dataclass
class LoadStats:
    teams_created: int = 0
    events_created: int = 0
    events_updated: int = 0
    results_upserted: int = 0
    odds_inserted: int = 0


def load_season(conn: psycopg.Connection, comp: Competition, season: str, parsed: ParsedSeason) -> LoadStats:
    stats = LoadStats()
    with conn.transaction():
        sport_id = conn.execute("SELECT id FROM sport WHERE code = %s", (comp.sport,)).fetchone()[0]
        comp_row = conn.execute("SELECT id FROM competition WHERE code = %s AND enabled", (comp.code,)).fetchone()
        if comp_row is None:
            raise RuntimeError(f"competition {comp.code} not in DB; run `fairline db migrate` to sync config")
        season_id = _upsert_season(conn, comp_row[0], season, parsed.events)

        teams = sorted(set(parsed.events["home"]) | set(parsed.events["away"]))
        team_ids, stats.teams_created = _resolve_teams(conn, sport_id, teams)

        event_ids = _upsert_events(conn, season_id, parsed.events, team_ids, stats)
        stats.results_upserted = _upsert_results(conn, parsed.events, event_ids)
        stats.odds_inserted = _insert_odds(conn, sport_id, parsed.odds, event_ids)
    return stats


def _upsert_season(conn, competition_id: int, season: str, events: pl.DataFrame) -> int:
    first, last = events["match_date"].min(), events["match_date"].max()
    return conn.execute(
        """
        INSERT INTO season (competition_id, label, starts_on, ends_on) VALUES (%s, %s, %s, %s)
        ON CONFLICT (competition_id, label) DO UPDATE
          SET starts_on = LEAST(season.starts_on, EXCLUDED.starts_on),
              ends_on = GREATEST(season.ends_on, EXCLUDED.ends_on)
        RETURNING id
        """,
        (competition_id, season, first, last),
    ).fetchone()[0]


def _resolve_teams(conn, sport_id: int, names: list[str]) -> tuple[dict[str, int], int]:
    """Map provider team names to participant ids, creating unreviewed participants for new names.

    football-data.co.uk is the first source, so its spellings seed the canonical names.
    Later sources resolve onto these via the alias table (and a review queue for fuzzy matches).
    """
    rows = conn.execute(
        "SELECT alias, participant_id FROM participant_alias WHERE provider = %s AND sport_id = %s AND alias = ANY(%s)",
        (PROVIDER, sport_id, names),
    ).fetchall()
    ids = dict(rows)
    created = 0
    for name in names:
        if name in ids:
            continue
        pid = conn.execute(
            "INSERT INTO participant (sport_id, kind, name) VALUES (%s, 'team', %s) RETURNING id",
            (sport_id, name),
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO participant_alias (provider, sport_id, alias, participant_id) VALUES (%s, %s, %s, %s)",
            (PROVIDER, sport_id, name, pid),
        )
        ids[name] = pid
        created += 1
    return ids, created


def _upsert_events(conn, season_id: int, events: pl.DataFrame, team_ids: dict[str, int], stats: LoadStats):
    existing = dict(
        conn.execute(
            "SELECT external_id, event_id FROM event_external_id WHERE provider = %s AND external_id = ANY(%s)",
            (PROVIDER, events["external_id"].to_list()),
        ).fetchall()
    )
    out: dict[str, int] = {}
    for ev in events.iter_rows(named=True):
        ext = ev["external_id"]
        args = (
            ev["kickoff_utc"],
            ev["kickoff_time_known"],
            team_ids[ev["home"]],
            team_ids[ev["away"]],
            ev["status"],
        )
        if ext in existing:
            cur = conn.execute(
                """
                UPDATE event SET starts_at = %s, kickoff_time_known = %s, home_id = %s, away_id = %s,
                  status = %s, updated_at = now()
                WHERE id = %s AND (starts_at, kickoff_time_known, home_id, away_id, status)
                  IS DISTINCT FROM (%s, %s, %s, %s, %s)
                """,
                (*args, existing[ext], *args),
            )
            stats.events_updated += cur.rowcount
            out[ext] = existing[ext]
        else:
            eid = conn.execute(
                "INSERT INTO event (season_id, starts_at, kickoff_time_known, home_id, away_id, status) "
                "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
                (season_id, *args),
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO event_external_id (provider, external_id, event_id) VALUES (%s, %s, %s)",
                (PROVIDER, ext, eid),
            )
            stats.events_created += 1
            out[ext] = eid
    return out


def _upsert_results(conn, events: pl.DataFrame, event_ids: dict[str, int]) -> int:
    finished = events.filter(pl.col("status") == "finished")
    rows = [
        (
            event_ids[r["external_id"]],
            r["home_goals"],
            r["away_goals"],
            r["ht_home"],
            r["ht_away"],
            json.dumps({k: v for k, v in r["stats"].items() if v is not None}),
            PROVIDER,
        )
        for r in finished.iter_rows(named=True)
    ]
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO football_result (event_id, home_goals, away_goals, ht_home, ht_away, stats, source)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (event_id) DO UPDATE SET home_goals = EXCLUDED.home_goals,
              away_goals = EXCLUDED.away_goals, ht_home = EXCLUDED.ht_home, ht_away = EXCLUDED.ht_away,
              stats = EXCLUDED.stats, source = EXCLUDED.source, recorded_at = now()
            WHERE (football_result.home_goals, football_result.away_goals, football_result.stats)
              IS DISTINCT FROM (EXCLUDED.home_goals, EXCLUDED.away_goals, EXCLUDED.stats)
            """,
            rows,
        )
    return len(rows)


def _insert_odds(conn, sport_id: int, odds: pl.DataFrame, event_ids: dict[str, int]) -> int:
    if odds.is_empty():
        return 0
    conn.execute(
        """
        CREATE TEMP TABLE _odds_in (
          ts TIMESTAMPTZ, ts_estimated BOOLEAN, event_id BIGINT, bookmaker TEXT, market TEXT,
          line NUMERIC, selection TEXT, price NUMERIC, is_closing BOOLEAN
        ) ON COMMIT DROP
        """
    )
    frame = odds.with_columns(pl.col("external_id").replace_strict(event_ids, return_dtype=pl.Int64).alias("event_id"))
    cols = [
        "ts",
        "ts_estimated",
        "event_id",
        "bookmaker",
        "market",
        "line",
        "selection",
        "price",
        "is_closing",
    ]
    with conn.cursor() as cur, cur.copy(f"COPY _odds_in ({', '.join(cols)}) FROM STDIN") as copy:
        for row in frame.select(cols).iter_rows():
            copy.write_row(row)

    missing = conn.execute(
        "SELECT DISTINCT bookmaker FROM _odds_in WHERE bookmaker NOT IN (SELECT code FROM bookmaker)"
    ).fetchall()
    if missing:
        raise RuntimeError(f"bookmakers missing from DB (add to bookmakers.yaml): {missing}")

    cur = conn.execute(
        """
        INSERT INTO odds_snapshot
          (ts, ts_estimated, event_id, market_id, bookmaker_id, selection, price, is_closing, source)
        SELECT i.ts, i.ts_estimated, i.event_id, m.id, b.id, i.selection, i.price, i.is_closing, %s
        FROM _odds_in i
        JOIN bookmaker b ON b.code = i.bookmaker
        JOIN market m ON m.sport_id = %s AND m.code = i.market AND m.line IS NOT DISTINCT FROM i.line
        ON CONFLICT DO NOTHING
        """,
        (PROVIDER, sport_id),
    )
    return cur.rowcount
