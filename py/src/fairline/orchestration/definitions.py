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


defs = Definitions(
    assets=[fdcuk_season, dixon_coles_walkforward],
    jobs=[fdcuk_job],
    schedules=[fdcuk_current_season_daily],
)
