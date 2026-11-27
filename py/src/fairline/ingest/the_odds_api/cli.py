"""`fairline odds ...` subcommands."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import psycopg

from fairline.core.config import Config
from fairline.ingest.the_odds_api import budget as B
from fairline.ingest.the_odds_api import poll as P
from fairline.ingest.the_odds_api.planner import simulate
from fairline.ingest.the_odds_api.resolve import PROVIDER


def _print_tick(rep: P.TickReport) -> None:
    n_fx = sum(len(v) for v in rep.fixtures.values())
    print(f"{rep.now:%Y-%m-%d %H:%M}Z  {n_fx} upcoming fixtures in {len(rep.fixtures)} leagues; "
          f"credits this month {rep.spent}, API remaining {rep.remaining if rep.remaining is not None else '?'}")  # fmt: skip
    if not rep.polls:
        print("  nothing to poll")
    for r in rep.polls:
        line = f"  {r.competition:<5} {r.kind:<6} {r.status:<8}"
        if r.status == "ok":
            line += (f" {r.credits} credits, {r.events} events, +{r.snapshots} snapshots, "
                     f"{r.created} created, {r.linked} linked")  # fmt: skip
        elif r.reason:
            line += f" {r.reason}"
        print(line)
        for n, ours in r.auto_matched.items():
            print(f"      auto-matched {n!r} -> {ours!r} (check with `fairline odds aliases`)")
        if r.unresolved:
            print(f"      unresolved teams, fixtures skipped: {r.unresolved}")
    if rep.closing:
        print(f"  closing: {rep.closing}")
    for w in rep.warnings:
        print(f"  warning: {w}")


def _sport_id(conn) -> int:
    return conn.execute("SELECT id FROM sport WHERE code = 'football'").fetchone()[0]


def run(conn: psycopg.Connection, config: Config, args) -> int:
    s = config.settings.odds_api
    now = datetime.now(UTC)
    if args.action in ("plan", "poll"):
        dry = args.action == "plan" or args.dry_run
        _print_tick(P.tick(conn, config, now=now, dry_run=dry, force=getattr(args, "force", [])))
        return 0
    if args.action == "sync":
        rows = P.sync_all(conn, config, now=now, force=args.force)
        for r in rows:
            if r.error:
                print(f"  {r.competition:<5} error: {r.error}")
                continue
            print(
                f"  {r.competition:<5} {r.fixtures} fixtures, {r.events} matched ({r.created} new, {r.linked} linked)"
            )
            for n, ours in r.auto_matched.items():
                print(f"        auto-matched {n!r} -> {ours!r}")
            if r.unresolved:
                print(f"        unresolved: {r.unresolved}")
        todo = sum(len(r.unresolved) for r in rows)
        failed = [r.competition for r in rows if r.error]
        if failed:
            print(f"sync FAILED for {failed}: nothing mapped there yet")
        print(
            "free calls only, 0 credits. "
            + (f"{todo} names to map: `fairline odds aliases`" if todo else "no names to map")
            + (" in the leagues that synced" if failed else "")
        )
        return 1 if failed else 0
    if args.action == "close":
        print(P.finalize_closing(conn, config, now))
        return 0
    if args.action == "budget":
        led = P.ledger(conn, now)
        spent, remaining = led.spent_month, led.remaining
        client = P.make_client(config)
        sports = [config.competition(c).provider_code(PROVIDER) for c in s.competitions]
        fx = {sp: P.to_fixtures(client.events(sp).data) for sp in sports}
        polls = simulate(fx, now, now + timedelta(days=7), s, P.last_polls(conn, sports))
        cost = B.call_cost(s.markets, len(config.provider_books(PROVIDER)))
        week = len(polls) * cost
        print(f"this month: {spent} credits spent of {s.monthly_credit_budget}; API says {remaining} remaining")
        print(f"next 7 days at the current schedule: {len(polls)} polls x {cost} = {week} credits "
              f"(~{week * 30.4 / 7:.0f}/month if every week looked like this)")  # fmt: skip
        by = {}
        for _, sp, kind in polls:
            by.setdefault(sp, {}).setdefault(kind, 0)
            by[sp][kind] += 1
        for sp, kinds in sorted(by.items()):
            print(f"  {sp:<34} {kinds}")
        if week * 30.4 / 7 > s.monthly_credit_budget:
            print("  over budget: pacing will drop T-24h/T-3h polls first and keep closing polls;"
                  " or trim ingest.the_odds_api.competitions / poll_windows_hours")  # fmt: skip
        return 0
    if args.action == "aliases":
        rows = conn.execute(
            "SELECT alias, competition, candidates, times_seen, last_seen FROM participant_alias_review "
            "WHERE provider = %s ORDER BY competition, alias",
            (args.provider,),
        ).fetchall()
        print(f"{len(rows)} unresolved (their fixtures are skipped until mapped):")
        for a, comp, cands, n, last in rows:
            print(f"  {comp:<5} {a!r:<32} candidates={list(cands) or '-'} seen {n}x, last {last:%Y-%m-%d}")
        auto = conn.execute(
            "SELECT a.alias, p.name FROM participant_alias a JOIN participant p ON p.id = a.participant_id "
            "WHERE a.provider = %s AND NOT a.reviewed ORDER BY a.alias",
            (args.provider,),
        ).fetchall()
        print(f"{len(auto)} auto-matched, not yet reviewed (`fairline odds alias ok NAME` once checked):")
        for a, n in auto:
            print(f"  {a!r:<32} -> {n!r}")
        return 0
    if args.action == "alias":
        sid = _sport_id(conn)
        if args.alias_action == "ok":
            n = conn.execute(
                "UPDATE participant_alias SET reviewed = true WHERE provider = %s AND sport_id = %s AND alias = %s",
                (args.provider, sid, args.name),
            ).rowcount
            conn.commit()
            print("marked reviewed" if n else "no such alias")
            return 0 if n else 1
        rows = conn.execute("SELECT id FROM participant WHERE sport_id = %s AND name = %s", (sid, args.team)).fetchall()
        if len(rows) > 1:
            print(f"{args.team!r} is ambiguous ({len(rows)} teams)")
            return 1
        if not rows and not args.new:
            print(f"no team called {args.team!r}; use our spelling, or --new to create it")
            return 1
        pid = (
            rows[0][0]
            if rows
            else conn.execute(
                "INSERT INTO participant (sport_id, kind, name) VALUES (%s, 'team', %s) RETURNING id", (sid, args.team)
            ).fetchone()[0]
        )
        conn.execute(
            "INSERT INTO participant_alias (provider, sport_id, alias, participant_id, reviewed) VALUES (%s, %s, %s, %s, true) "
            "ON CONFLICT (provider, sport_id, alias) DO UPDATE SET participant_id = EXCLUDED.participant_id, reviewed = true",
            (args.provider, sid, args.name, pid),
        )
        conn.execute(
            "DELETE FROM participant_alias_review WHERE provider = %s AND sport_id = %s AND alias = %s",
            (args.provider, sid, args.name),
        )
        conn.commit()
        print(f"{args.name!r} -> {args.team!r}; its fixtures are picked up on the next poll")
        return 0
    return 2
