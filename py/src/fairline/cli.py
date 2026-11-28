"""fairline CLI.

fairline db migrate
fairline ingest fdcuk --competition ENG1 --season 2024-25 [--force]
fairline ingest fdcuk --backfill
fairline fixtures season | window
fairline odds sync | plan | poll [--dry-run] [--force ENG1] | close | budget | aliases | alias add NAME TEAM
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

    pe = sub.add_parser("edge").add_subparsers(dest="action", required=True)
    eb = pe.add_parser("board", help="value board for one historical matchday")
    eb.add_argument("--pool", required=True)
    eb.add_argument("--date", required=True, help="London calendar day, YYYY-MM-DD")
    eb.add_argument("--offset-hours", type=float, default=2.0, help="decide this long before each kickoff")
    eb.add_argument("--all", action="store_true", help="show every evaluated price, not just the board")
    eb.add_argument("--persist", action="store_true", help="write all evaluated prices to the edge table")

    pbt = sub.add_parser("backtest").add_subparsers(dest="action", required=True)
    br = pbt.add_parser("run", help="walk-forward betting + probability backtest")
    br.add_argument("--pools", default="ENG,ESP,GER,FRA")
    br.add_argument("--from", dest="first", default="2017-18")
    br.add_argument("--to", dest="last", default="2025-26")
    br.add_argument("--offset-hours", type=float, default=2.0)
    br.add_argument("--boot", type=int, default=2000, help="bootstrap resamples")
    br.add_argument("--persist", action="store_true", help="store a backtest_run row")
    bb = pbt.add_parser("bands", help="check PyMC band calibration on a sample of slates")
    bb.add_argument("--pools", default="ENG,GER")
    bb.add_argument("--slates", type=int, default=12, help="slates per pool")

    po = sub.add_parser("odds", help="live odds from The Odds API (P1.1-7)").add_subparsers(
        dest="action", required=True
    )
    po.add_parser("plan", help="what the next tick would poll and why (free calls only)")
    psy = po.add_parser("sync", help="free fixture sync: map team names and create events, 0 credits")
    psy.add_argument("--force", action="store_true", help="refetch fixture lists even if cached")
    pp = po.add_parser("poll", help="run one scheduler tick: plan, budget check, poll, mark closing")
    pp.add_argument("--dry-run", action="store_true", help="free fixture calls only; spend nothing")
    pp.add_argument("--force", action="append", default=[], metavar="COMP", help="poll this competition now")
    pcl = po.add_parser("close", help="mark closing prices for kicked-off events")
    pcl.add_argument("--redo-since", metavar="YYYY-MM-DD",
                     help="rebuild closes for events kicked off since this date (UTC); tracked CLV is recomputed on the next refresh")  # fmt: skip
    prd = po.add_parser("rederive", help="replay cached /odds responses through the O/U derivation (0 credits)")
    prd.add_argument("--date", help="YYYY-MM-DD cache folder (default: all)")
    prd.add_argument("--cache-dir", help="default: ingest.the_odds_api.cache_dir")
    po.add_parser("budget", help="credits used this month and a 7-day spend estimate")
    pal = po.add_parser("aliases", help="team names waiting for review, and unreviewed auto-matches")
    pal.add_argument("--provider", default="the_odds_api", choices=["the_odds_api", "football_data_org"])
    pa_ = po.add_parser("alias")
    pa_.add_argument("--provider", default="the_odds_api", choices=["the_odds_api", "football_data_org"])
    pa = pa_.add_subparsers(dest="alias_action", required=True)
    paa = pa.add_parser("add", help="map a provider team name to one of our teams")
    paa.add_argument("name", help="the provider's spelling, e.g. 'Wolverhampton Wanderers'")
    paa.add_argument("team", help="our team name, e.g. 'Wolves'")
    paa.add_argument("--new", action="store_true", help="create the team if we have never seen it")
    pao = pa.add_parser("ok", help="mark an auto-matched name as reviewed")
    pao.add_argument("name")

    pf = sub.add_parser("fixtures", help="football-data.org fixtures, status and results (P1.1-6)")
    pfs = pf.add_subparsers(dest="action", required=True)
    fs = pfs.add_parser("season", help="whole-season fixture list, one request per competition")
    fs.add_argument("--competition", action="append", help="default: ingest.football_data_org.competitions")
    fs.add_argument("--season", help="e.g. 2026-27 (default: current)")
    fw = pfs.add_parser("window", help="yesterday..tomorrow: kickoff moves, postponements, fresh results")
    fw.add_argument("--competition", action="append")

    args = p.parse_args(argv)
    config = get_config()

    with db.connect() as conn:
        if args.cmd == "db":
            applied = db.migrate(conn)
            db.sync_reference(conn, config)
            print(f"applied: {applied or 'nothing new'}; reference tables synced")
            return 0

        if args.cmd == "fixtures":
            from fairline.ingest.football_data_org import sync as fdo

            if args.action == "season":
                rows = fdo.sync_season(conn, config, comps=args.competition, season=args.season)
            else:
                rows = fdo.sync_window(conn, config, comps=args.competition)
            for r in rows:
                if r.error:
                    print(f"  {r.competition:<5} {r.mode:<16} FAILED {r.error}")
                    continue
                print(f"  {r.competition:<5} {r.mode:<16} {r.matches} matches, {r.events} matched "
                      f"(+{r.created} new, {r.linked} linked), {r.status_changes} status / {r.kickoff_changes} kickoff "
                      f"changes, {r.results} results")  # fmt: skip
                for n, ours in r.auto_matched.items():
                    print(f"        auto-matched {n!r} -> {ours!r}")
                if r.unresolved:
                    print(
                        f"        unresolved (skipped): {r.unresolved}  -> fairline odds aliases --provider football_data_org"
                    )
                for d in r.disagreements:
                    print(f"        SCORE DISAGREEMENT {d}")
                for w in r.warnings:
                    print(f"        warning: {w}")
            return 1 if any(r.error for r in rows) else 0

        if args.cmd == "odds":
            from fairline.ingest.the_odds_api import cli as odds_cli

            try:
                return odds_cli.run(conn, config, args)
            except NotAllowed as e:
                print(f"refused: {e}", file=sys.stderr)
                return 2

        if args.cmd == "backtest":
            pools = [x.strip() for x in args.pools.split(",")]
            bad = [x for x in pools if x not in config.settings.models.dixon_coles.pools]
            if bad:
                print(f"refused: unknown pools {bad}", file=sys.stderr)
                return 2
            if args.action == "run":
                from fairline.backtest.run import run_backtest

                run_backtest(conn, config, pools, args.first, args.last, args.offset_hours, args.persist, args.boot)
            else:
                from fairline.backtest.bands import run_bands

                run_bands(conn, config, pools, args.slates)
            return 0

        if args.cmd == "edge":
            import polars as pl

            from fairline.edge.run import run_board

            if args.pool not in config.settings.models.dixon_coles.pools:
                print(f"refused: unknown pool {args.pool!r}", file=sys.stderr)
                return 2
            board = run_board(conn, config, args.pool, args.date, args.offset_hours)
            df = board.frame(board_only=not args.all)
            cols = ["label", "market", "selection", "bookmaker", "price", "p_model", "p_ref", "ev_model",
                    "ev_vs_ref", "ev_vs_ref_conservative", "stake_pct", "capped_by", "flags"]  # fmt: skip
            with pl.Config(tbl_rows=60, tbl_cols=20, tbl_width_chars=220, fmt_str_lengths=40, float_precision=3):
                print(df.select(cols) if not df.is_empty() else "board is empty")
            print(f"evaluated {len(board.candidates)} prices; skipped snapshots: {board.skipped_snapshots or 'none'}")
            if args.persist:
                from fairline.edge.store import persist

                n = persist(conn, board, config.settings.edge.devig_method)
                print(f"persisted {n} evaluated prices")
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
