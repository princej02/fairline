"""Dagster definitions. Partitioned by (competition, season) so each season is one backfillable unit."""

from dagster import (
    AssetExecutionContext,
    Definitions,
    MaterializeResult,
    MultiPartitionKey,
    MultiPartitionsDefinition,
    RunRequest,
    ScheduleEvaluationContext,
    StaticPartitionsDefinition,
    asset,
    define_asset_job,
    schedule,
)

from fairline.core import db
from fairline.core.config import get_config, season_range
from fairline.ingest.football_data_co_uk.load import PROVIDER
from fairline.ingest.football_data_co_uk.pipeline import ingest_season

_cfg = get_config()
_fd = _cfg.settings.fdcuk
COMPETITIONS = [c.code for c in _cfg.competitions_for(PROVIDER)]
SEASONS = season_range(_fd.backfill_from, _fd.current_season)

fdcuk_partitions = MultiPartitionsDefinition(
    {"competition": StaticPartitionsDefinition(COMPETITIONS), "season": StaticPartitionsDefinition(SEASONS)}
)


@asset(partitions_def=fdcuk_partitions, group_name="ingest", compute_kind="python")
def fdcuk_season(context: AssetExecutionContext) -> MaterializeResult:
    """Results + Pinnacle/Betfair/Bet365/William Hill odds (pre-match and closing) for one season."""
    keys = context.partition_key.keys_by_dimension
    with db.connect() as conn:
        r = ingest_season(conn, keys["competition"], keys["season"])
    for w in r.warnings:
        context.log.warning(w)
    load = r.load
    return MaterializeResult(
        metadata={
            "status": r.status,
            "url": r.url,
            "matches": r.matches,
            "odds_rows_parsed": r.odds_rows_parsed,
            "events_created": load.events_created if load else 0,
            "odds_inserted": load.odds_inserted if load else 0,
            "warnings": len(r.warnings),
        }
    )


fdcuk_job = define_asset_job("fdcuk_job", selection=[fdcuk_season], partitions_def=fdcuk_partitions)


@schedule(job=fdcuk_job, cron_schedule="30 6 * * *", execution_timezone="Europe/London")
def fdcuk_current_season_daily(context: ScheduleEvaluationContext):
    """Refresh the current season for every league once a day (cache + conditional GET keep it cheap)."""
    for comp in COMPETITIONS:
        key = MultiPartitionKey({"competition": comp, "season": _fd.current_season})
        yield RunRequest(run_key=f"{key}:{context.scheduled_execution_time:%Y-%m-%d}", partition_key=key)


# ---------------------------------------------------------------- models (P1.2)
from fairline.models.football.run import run_walkforward  # noqa: E402

POOLS = list(_cfg.settings.models.dixon_coles.pools)
pool_partitions = StaticPartitionsDefinition(POOLS)


@asset(partitions_def=pool_partitions, group_name="models", compute_kind="python", deps=[fdcuk_season])
def dixon_coles_walkforward(context: AssetExecutionContext) -> MaterializeResult:
    """Weekly-refit walk-forward Dixon-Coles MLE for one country pool; persists versions + predictions."""
    with db.connect() as conn:
        out = run_walkforward(conn, get_config(), context.partition_key, persist=True)
    return MaterializeResult(metadata=out)


# ---------------------------------------------------------------- live odds (P1.1-7)
from dagster import DefaultScheduleStatus, OpExecutionContext, job, op  # noqa: E402

from fairline.ingest.the_odds_api.poll import tick  # noqa: E402

_odds = _cfg.settings.odds_api


@op
def odds_tick(context: OpExecutionContext) -> dict:
    """Plan -> budget check -> poll -> mark closing. Free fixture calls every tick; paid calls only
    when a fixture enters a window (see ingest/the_odds_api/planner.py)."""
    with db.connect() as conn:
        rep = tick(conn, get_config())
    for w in rep.warnings:
        context.log.warning(w)
    for r in rep.polls:
        msg = f"{r.competition} {r.kind} {r.status} {r.reason} credits={r.credits} snapshots={r.snapshots}"
        (context.log.warning if r.status in ("failed", "skipped") or r.unresolved else context.log.info)(
            msg + (f" unresolved={r.unresolved}" if r.unresolved else "")
        )
    return {"spent": rep.spent, "remaining": rep.remaining, "polls": len(rep.polls), "closing": rep.closing}


@job
def odds_tick_job():
    odds_tick()


@schedule(
    job=odds_tick_job,
    cron_schedule=f"*/{int(_odds.tick_minutes)} * * * *",
    execution_timezone="UTC",
    default_status=DefaultScheduleStatus.STOPPED,  # turning it on is the decision to spend credits
)
def odds_tick_schedule(context: ScheduleEvaluationContext):
    return RunRequest(run_key=f"odds:{context.scheduled_execution_time:%Y%m%d%H%M}")


# ---------------------------------------------------------------- fixtures (P1.1-6)
from fairline.ingest.football_data_org.sync import sync_season, sync_window  # noqa: E402


def _log_fdo(context: OpExecutionContext, rows) -> dict:
    for r in rows:
        if r.error:
            context.log.warning(f"{r.competition} {r.mode}: {r.error}")
        for d in r.disagreements:
            context.log.warning(f"{r.competition}: score disagreement {d}")
        if r.unresolved:
            context.log.warning(f"{r.competition}: unmapped team names {r.unresolved}")
    return {r.competition: {"matches": r.matches, "results": r.results, "error": r.error} for r in rows}


@op
def fdo_season_sync(context: OpExecutionContext) -> dict:
    """Whole-season fixture lists from football-data.org (one request per competition)."""
    with db.connect() as conn:
        return _log_fdo(context, sync_season(conn, get_config()))


@op
def fdo_window_sync(context: OpExecutionContext) -> dict:
    """Yesterday..tomorrow: kickoff moves, postponements, live status, results minutes after FT."""
    with db.connect() as conn:
        return _log_fdo(context, sync_window(conn, get_config()))


@job
def fdo_season_job():
    fdo_season_sync()


@job
def fdo_window_job():
    fdo_window_sync()


@schedule(job=fdo_season_job, cron_schedule="15 5 * * *", execution_timezone="Europe/London",
          default_status=DefaultScheduleStatus.STOPPED)  # fmt: skip
def fdo_season_daily(context: ScheduleEvaluationContext):
    return RunRequest(run_key=f"fdo-season:{context.scheduled_execution_time:%Y%m%d}")


@schedule(job=fdo_window_job, cron_schedule="*/15 * * * *", execution_timezone="UTC",
          default_status=DefaultScheduleStatus.STOPPED)  # fmt: skip
def fdo_window_15m(context: ScheduleEvaluationContext):
    return RunRequest(run_key=f"fdo-window:{context.scheduled_execution_time:%Y%m%d%H%M}")


defs = Definitions(
    assets=[fdcuk_season, dixon_coles_walkforward],
    jobs=[fdcuk_job, odds_tick_job, fdo_season_job, fdo_window_job],
    schedules=[fdcuk_current_season_daily, odds_tick_schedule, fdo_season_daily, fdo_window_15m],
)
