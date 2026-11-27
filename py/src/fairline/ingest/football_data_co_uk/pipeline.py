"""football-data.co.uk pipeline: allowlist check -> fetch (cached) -> parse/validate -> load -> audit row."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field

import psycopg

from fairline.core.config import Config, NotAllowed, get_config, season_range, season_start_year
from fairline.ingest.football_data_co_uk.load import PROVIDER, LoadStats, load_season
from fairline.ingest.football_data_co_uk.parse import parse_season
from fairline.ingest.football_data_co_uk.source import FootballDataCoUkSource

log = logging.getLogger(__name__)


@dataclass
class SeasonResult:
    competition: str
    season: str
    status: str  # ok | skipped
    url: str = ""
    matches: int = 0
    odds_rows_parsed: int = 0
    load: LoadStats | None = None
    warnings: list[str] = field(default_factory=list)


def make_source(config: Config) -> FootballDataCoUkSource:
    s = config.settings.fdcuk
    return FootballDataCoUkSource(
        base_url=s.base_url,
        cache_dir=config.path(s.cache_dir),
        current_season=s.current_season,
        refresh_current_hours=s.refresh_current_hours,
        request_delay_seconds=s.request_delay_seconds,
    )


def ingest_season(
    conn: psycopg.Connection,
    competition: str,
    season: str,
    *,
    config: Config | None = None,
    source: FootballDataCoUkSource | None = None,
    force: bool = False,
) -> SeasonResult:
    config = config or get_config()
    comp = config.competition(competition)  # raises NotAllowed
    division = comp.provider_code(PROVIDER)  # raises NotAllowed
    if season_start_year(season) > season_start_year(config.settings.fdcuk.current_season):
        raise NotAllowed(f"season {season} is after current_season in settings.toml")
    source = source or make_source(config)

    run_id = conn.execute(
        "INSERT INTO ingestion_run (source, scope) VALUES (%s, %s) RETURNING id",
        (PROVIDER, f"{competition}:{season}"),
    ).fetchone()[0]
    conn.commit()
    try:
        dl = source.fetch(division, season, force=force)
        result = SeasonResult(competition, season, "ok", url=dl.url)
        parsed = parse_season(dl.content, division, season)
        result.matches = parsed.events.height
        result.odds_rows_parsed = parsed.odds.height
        result.warnings = parsed.warnings
        for w in parsed.warnings:
            log.warning("%s %s: %s", competition, season, w)
        if parsed.events.is_empty():
            result.status = "skipped"
        else:
            result.load = load_season(conn, comp, season, parsed)
        _finish(conn, run_id, result.status, result)
        return result
    except Exception as exc:
        conn.rollback()
        _finish(conn, run_id, "failed", None, error=repr(exc))
        raise


def backfill(conn: psycopg.Connection, *, config: Config | None = None, force: bool = False):
    config = config or get_config()
    s = config.settings.fdcuk
    source = make_source(config)
    for comp in config.competitions_for(PROVIDER):
        for season in season_range(s.backfill_from, s.current_season):
            yield ingest_season(conn, comp.code, season, config=config, source=source, force=force)


def _finish(conn, run_id: int, status: str, result: SeasonResult | None, error: str | None = None) -> None:
    stats = asdict(result) if result else {}
    conn.execute(
        "UPDATE ingestion_run SET finished_at = now(), status = %s, stats = %s, error = %s WHERE id = %s",
        (status, json.dumps(stats, default=str), error, run_id),
    )
    conn.commit()
