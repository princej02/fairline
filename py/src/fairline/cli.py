"""fairline CLI.

fairline db migrate
fairline ingest fdcuk --competition ENG1 --season 2024-25 [--force]
fairline ingest fdcuk --backfill
"""

from __future__ import annotations

import argparse
import logging
import sys

from fairline.core import db
from fairline.core.config import NotAllowed, get_config


def _print_result(r) -> None:
    load = r.load
    line = f"{r.competition} {r.season}: {r.status}, {r.matches} matches, {r.odds_rows_parsed} odds parsed"
    if load:
        line += (
            f" | +{load.events_created} events, ~{load.events_updated} updated, "
            f"+{load.odds_inserted} odds, +{load.teams_created} teams"
        )
    print(line)
    for w in r.warnings:
        print(f"  warning: {w}")


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser(prog="fairline")
    sub = p.add_subparsers(dest="cmd", required=True)

    pdb = sub.add_parser("db").add_subparsers(dest="action", required=True)
    pdb.add_parser("migrate", help="apply migrations and sync config reference tables")

    ping = sub.add_parser("ingest").add_subparsers(dest="source", required=True)
    f = ping.add_parser("fdcuk", help="football-data.co.uk results + odds")
    f.add_argument("--competition")
    f.add_argument("--season")
    f.add_argument("--backfill", action="store_true")
    f.add_argument("--force", action="store_true", help="ignore the download cache")

    pm_ = sub.add_parser("model").add_subparsers(dest="action", required=True)
    w = pm_.add_parser("walkforward", help="walk-forward Dixon-Coles MLE for one country pool")
    w.add_argument("--pool", required=True, help="e.g. ENG (see models.dixon_coles.pools)")
    w.add_argument("--cold-start", choices=["pooled", "prior"], help="override settings.toml")
    w.add_argument("--persist", action="store_true", help="write model_version + prediction rows")
    bz = pm_.add_parser("bayes", help="PyMC uncertainty bands for the week after --as-of")
    bz.add_argument("--pool", required=True)
    bz.add_argument("--as-of", required=True, help="YYYY-MM-DD (UTC midnight)")

    args = p.parse_args(argv)
    config = get_config()

    with db.connect() as conn:
        if args.cmd == "db":
            applied = db.migrate(conn)
            db.sync_reference(conn, config)
            print(f"applied: {applied or 'nothing new'}; reference tables synced")
            return 0

        if args.cmd == "model":
            from fairline.models.football.run import run_bayes, run_walkforward

            if args.pool not in config.settings.models.dixon_coles.pools:
                print(f"refused: unknown pool {args.pool!r}", file=sys.stderr)
                return 2
            if args.action == "walkforward":
                print(run_walkforward(conn, config, args.pool, cold_start=args.cold_start, persist=args.persist))
            else:
                print(run_bayes(conn, config, args.pool, args.as_of))
            return 0

        from fairline.ingest.football_data_co_uk.pipeline import backfill, ingest_season

        try:
            if args.backfill:
                for r in backfill(conn, config=config, force=args.force):
                    _print_result(r)
            elif args.competition and args.season:
                _print_result(ingest_season(conn, args.competition, args.season, config=config, force=args.force))
            else:
                p.error("give --competition and --season, or --backfill")
        except NotAllowed as e:
            print(f"refused: {e}", file=sys.stderr)
            return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
