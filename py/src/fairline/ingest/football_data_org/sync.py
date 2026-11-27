"""Apply football-data.org matches to our events: create/link fixtures, keep kickoff and status
current, and write results as soon as a match finishes.

Precedence:
- Kickoff and status: football-data.org is authoritative for fixtures it covers (it is the only
  source that announces postponements and rearranged dates). A finished event only changes
  status if they say it was cancelled (void).
- Results: football-data.org writes `football_result` rows with source 'football_data_org' and
  may update its own rows; it never overwrites a football-data.co.uk row, which carries match
  stats. When both exist and the scores differ, that is reported, not silently resolved.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import psycopg

from fairline.core.config import Config, season_label_for, season_start_year
from fairline.ingest.football_data_org.client import FdoClient, FdoError
from fairline.ingest.football_data_org.parse import Match, parse_matches
from fairline.ingest.resolve import resolve_event, resolve_teams

PROVIDER = "football_data_org"


@dataclass
class SyncStats:
    competition: str
    mode: str
    matches: int = 0
    events: int = 0
    created: int = 0
    linked: int = 0
    status_changes: int = 0
    kickoff_changes: int = 0
    results: int = 0
    unresolved: list[str] = field(default_factory=list)
    auto_matched: dict[str, str] = field(default_factory=dict)
    disagreements: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    error: str = ""


def make_client(config: Config) -> FdoClient:
    s = config.settings.fdorg
    return FdoClient(s.base_url, config.path(s.cache_dir), s.request_delay_seconds)


def apply_matches(
    conn: psycopg.Connection, config: Config, comp_code: str, matches: list[Match], now: datetime, st: SyncStats
) -> None:
    comp = config.competition(comp_code)
    st.matches = len(matches)
    if not matches:
        return
    with conn.transaction():
        sport_id, comp_id = conn.execute(
            "SELECT sport_id, id FROM competition WHERE code = %s", (comp_code,)
        ).fetchone()
        names = sorted({t.name for m in matches for t in (m.home, m.away)})
        variants = {t.name: [t.short] for m in matches for t in (m.home, m.away) if t.short and t.short != t.name}
        teams = resolve_teams(conn, PROVIDER, comp, sport_id, names, now, variants)
        st.unresolved, st.auto_matched = sorted(teams.unresolved), teams.auto_matched
        for m in matches:
            if m.home.name not in teams.ids or m.away.name not in teams.ids:
                continue
            before = conn.execute(
                "SELECT e.starts_at, e.status FROM event e JOIN event_external_id x ON x.event_id = e.id "
                "WHERE x.provider = %s AND x.external_id = %s",
                (PROVIDER, m.external_id),
            ).fetchone()
            eid, how = resolve_event(
                conn, PROVIDER, comp, comp_id, m.external_id, teams.ids[m.home.name], teams.ids[m.away.name], m.kickoff,
                authoritative=True,
            )  # fmt: skip
            st.events += 1
            st.created += how == "created"
            st.linked += how == "linked"
            cur = conn.execute(
                """
                UPDATE event SET status = %s, kickoff_time_known = %s, round = coalesce(%s, round), updated_at = now()
                WHERE id = %s AND (status <> 'finished' OR %s = 'void')
                  AND (status, kickoff_time_known, round) IS DISTINCT FROM (%s, %s, coalesce(%s, round))
                RETURNING status
                """,
                (m.status, m.kickoff_known, _round(m), eid, m.status, m.status, m.kickoff_known, _round(m)),
            ).fetchone()
            if before is not None:
                st.status_changes += cur is not None and before[1] != m.status
                st.kickoff_changes += before[0] != m.kickoff and m.status != "finished"
            if m.status == "finished" and m.score is not None:
                st.results += _write_result(conn, eid, m, st)


def _round(m: Match) -> str | None:
    return None if m.matchday is None else str(m.matchday)


def _write_result(conn, eid: int, m: Match, st: SyncStats) -> int:
    existing = conn.execute(
        "SELECT home_goals, away_goals, source FROM football_result WHERE event_id = %s", (eid,)
    ).fetchone()
    if existing and existing[2] != PROVIDER:
        if (existing[0], existing[1]) != m.score:
            st.disagreements.append(
                f"event {eid}: {existing[2]} {existing[0]}-{existing[1]} vs ours {m.score[0]}-{m.score[1]}"
            )
        return 0
    ht = m.half_time or (None, None)
    cur = conn.execute(
        """
        INSERT INTO football_result (event_id, home_goals, away_goals, ht_home, ht_away, source)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (event_id) DO UPDATE SET home_goals = EXCLUDED.home_goals, away_goals = EXCLUDED.away_goals,
          ht_home = EXCLUDED.ht_home, ht_away = EXCLUDED.ht_away, recorded_at = now()
        WHERE football_result.source = %s
          AND (football_result.home_goals, football_result.away_goals) IS DISTINCT FROM (EXCLUDED.home_goals, EXCLUDED.away_goals)
        """,
        (eid, m.score[0], m.score[1], ht[0], ht[1], PROVIDER, PROVIDER),
    )
    return cur.rowcount


def _run(conn, config, client: FdoClient, comp_code: str, mode: str, fetch, now: datetime) -> SyncStats:
    st = SyncStats(comp_code, mode)
    run_id = conn.execute(
        "INSERT INTO ingestion_run (source, scope, started_at) VALUES (%s, %s, %s) RETURNING id",
        (PROVIDER, f"{comp_code}:{mode}", now),
    ).fetchone()[0]
    conn.commit()
    try:
        resp = fetch(client, config.competition(comp_code).provider_code(PROVIDER))
    except FdoError as e:
        st.error = str(e)
        conn.execute(
            "UPDATE ingestion_run SET status = 'failed', finished_at = now(), error = %s WHERE id = %s",
            (st.error, run_id),
        )
        conn.commit()
        return st
    matches, st.warnings = parse_matches(resp.data)
    apply_matches(conn, config, comp_code, matches, now, st)
    stats = {k: v for k, v in st.__dict__.items() if k not in ("competition", "mode", "error")}
    conn.execute(
        "UPDATE ingestion_run SET status = 'ok', finished_at = now(), stats = %s WHERE id = %s",
        (json.dumps(stats | {"cache": str(resp.cache_path)}), run_id),
    )
    conn.commit()
    return st


def sync_season(conn, config: Config, client: FdoClient | None = None, comps: list[str] | None = None,
                season: str | None = None, now: datetime | None = None) -> list[SyncStats]:  # fmt: skip
    """The whole season's fixture list per competition: one request each."""
    now = now or datetime.now(UTC)
    client = client or make_client(config)
    label = season or season_label_for(now.date())
    y = season_start_year(label)
    return [
        _run(conn, config, client, c, f"season:{label}", lambda cl, code: cl.season(code, y), now)
        for c in comps or config.settings.fdorg.competitions
    ]


def sync_window(conn, config: Config, client: FdoClient | None = None, comps: list[str] | None = None,
                now: datetime | None = None) -> list[SyncStats]:  # fmt: skip
    """Yesterday..tomorrow by default: kickoff moves, postponements, live status and fresh results."""
    s = config.settings.fdorg
    now = now or datetime.now(UTC)
    client = client or make_client(config)
    lo, hi = (now - timedelta(days=s.window_days_back)).date(), (now + timedelta(days=s.window_days_ahead)).date()
    return [
        _run(conn, config, client, c, "window", lambda cl, code: cl.window(code, lo, hi), now)
        for c in comps or s.competitions
    ]
