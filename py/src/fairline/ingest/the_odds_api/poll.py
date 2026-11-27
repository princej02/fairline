"""One scheduler tick: refresh fixture lists (free), decide which leagues need a paid poll, check
the credit budget, poll, resolve teams/events, store snapshots, then mark closing prices.

Every paid call is an ingestion_run row (source 'the_odds_api', scope 'odds:<sport_key>') whose
stats hold the credits it cost, the API's remaining balance, the poll time and which books priced
which event. That row is both the credit ledger and the evidence a closing snapshot rests on.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import psycopg

from fairline.core.config import Config
from fairline.ingest.the_odds_api import budget as B
from fairline.ingest.the_odds_api.client import OddsApiClient, OddsApiError
from fairline.ingest.the_odds_api.parse import fixtures as to_fixtures
from fairline.ingest.the_odds_api.parse import parse_odds
from fairline.ingest.the_odds_api.planner import Fixture, PollNeed, plan
from fairline.ingest.the_odds_api.resolve import PROVIDER, resolve_event, resolve_teams

log = logging.getLogger(__name__)


def make_client(config: Config) -> OddsApiClient:
    s = config.settings.odds_api
    return OddsApiClient(s.base_url, config.path(s.cache_dir), events_refresh=timedelta(hours=s.events_refresh_hours))


def ou_lines(config: Config) -> set[float]:
    return {float(m.split("_", 1)[1]) for m in config.settings.markets["enabled"] if m.startswith("OU_")}


# ---------------------------------------------------------------------- ledger
def ledger(conn: psycopg.Connection, now: datetime) -> tuple[int, int | None]:
    """(credits we spent this calendar month, the API's last reported remaining balance)."""
    spent = conn.execute(
        "SELECT coalesce(sum((stats->>'credits')::int), 0) FROM ingestion_run "
        "WHERE source = %s AND started_at >= %s AND stats ? 'credits'",
        (PROVIDER, B.month_start(now)),
    ).fetchone()[0]
    rem = conn.execute(
        "SELECT (stats->>'remaining')::int FROM ingestion_run WHERE source = %s AND stats ? 'remaining' "
        "AND stats->>'remaining' IS NOT NULL ORDER BY started_at DESC LIMIT 1",
        (PROVIDER,),
    ).fetchone()
    return int(spent), (rem[0] if rem else None)


def last_polls(conn: psycopg.Connection, sports: list[str]) -> dict[str, datetime | None]:
    rows = conn.execute(
        "SELECT substr(scope, 6), max((stats->>'polled_at')::timestamptz) FROM ingestion_run "
        "WHERE source = %s AND status = 'ok' AND scope = ANY(%s) GROUP BY 1",
        (PROVIDER, [f"odds:{s}" for s in sports]),
    ).fetchall()
    out: dict[str, datetime | None] = dict.fromkeys(sports)
    out.update(dict(rows))
    return out


# ---------------------------------------------------------------------- load
def _insert_snapshots(conn, sport_id: int, rows: list[dict]) -> int:
    if not rows:
        return 0
    conn.execute(
        """
        CREATE TEMP TABLE _live_in (
          ts TIMESTAMPTZ, event_id BIGINT, bookmaker TEXT, market TEXT, line NUMERIC, selection TEXT, price NUMERIC
        ) ON COMMIT DROP
        """
    )
    cols = ["ts", "event_id", "bookmaker", "market", "line", "selection", "price"]
    with conn.cursor() as cur, cur.copy(f"COPY _live_in ({', '.join(cols)}) FROM STDIN") as copy:
        for r in rows:
            copy.write_row([r[c] for c in cols])
    cur = conn.execute(
        """
        INSERT INTO odds_snapshot (ts, ts_estimated, event_id, market_id, bookmaker_id, selection, price, is_closing, source)
        SELECT i.ts, false, i.event_id, m.id, b.id, i.selection, i.price, false, %s
        FROM _live_in i
        JOIN bookmaker b ON b.code = i.bookmaker
        JOIN market m ON m.sport_id = %s AND m.code = i.market AND m.line IS NOT DISTINCT FROM i.line
        ON CONFLICT DO NOTHING
        """,
        (PROVIDER, sport_id),
    )
    return cur.rowcount


@dataclass
class PollResult:
    competition: str
    sport: str
    kind: str
    status: str  # ok | skipped | failed | dry-run
    reason: str = ""
    credits: int = 0
    remaining: int | None = None
    events: int = 0
    snapshots: int = 0
    created: int = 0
    linked: int = 0
    unresolved: list[str] = field(default_factory=list)
    auto_matched: dict[str, str] = field(default_factory=dict)


def poll_competition(
    conn: psycopg.Connection, config: Config, client: OddsApiClient, comp_code: str, kind: str, now: datetime
) -> PollResult:
    comp = config.competition(comp_code)
    sport_key = comp.provider_code(PROVIDER)
    books = config.provider_books(PROVIDER)
    rev = {v: k for k, v in books.items()}
    s = config.settings.odds_api
    res = PollResult(comp_code, sport_key, kind, "running")
    run_id = conn.execute(
        "INSERT INTO ingestion_run (source, scope, started_at) VALUES (%s, %s, %s) RETURNING id",
        (PROVIDER, f"odds:{sport_key}", now),
    ).fetchone()[0]
    conn.commit()
    try:
        resp = client.odds(sport_key, sorted(books.values()), s.markets)
    except OddsApiError as e:
        conn.execute(
            "UPDATE ingestion_run SET status = 'failed', finished_at = now(), error = %s, stats = %s WHERE id = %s",
            (str(e), json.dumps({"kind": kind, "credits": 0}), run_id),
        )
        conn.commit()
        res.status, res.reason = "failed", str(e)
        return res
    res.credits = resp.usage.last if resp.usage.last is not None else B.call_cost(s.markets, len(books))
    res.remaining = resp.usage.remaining
    parsed = parse_odds(resp.data, rev, ou_lines(config), resp.fetched_at)
    with conn.transaction():
        sport_id, comp_id = conn.execute(
            "SELECT c.sport_id, c.id FROM competition c WHERE c.code = %s", (comp_code,)
        ).fetchone()
        teams = resolve_teams(conn, comp, sport_id, [n for e in parsed.events for n in (e.home, e.away)], now)
        res.unresolved = sorted(teams.unresolved)
        res.auto_matched = teams.auto_matched
        ext_to_event: dict[str, int] = {}
        for e in parsed.events:
            if e.home not in teams.ids or e.away not in teams.ids:
                continue
            eid, how = resolve_event(
                conn, comp, comp_id, e.external_id, teams.ids[e.home], teams.ids[e.away], e.commence
            )
            ext_to_event[e.external_id] = eid
            res.created += how == "created"
            res.linked += how == "linked"
        rows = [
            r | {"event_id": ext_to_event[r["external_id"]]} for r in parsed.rows if r["external_id"] in ext_to_event
        ]
        res.snapshots = _insert_snapshots(conn, sport_id, rows)
        res.events = len(ext_to_event)
        stats = {
            "kind": kind,
            "credits": res.credits,
            "remaining": resp.usage.remaining,
            "used": resp.usage.used,
            "polled_at": resp.fetched_at.isoformat(),
            "cache": str(resp.cache_path.relative_to(config.root))
            if resp.cache_path.is_relative_to(config.root)
            else str(resp.cache_path),
            "events": {str(ext_to_event[x]): sorted(b) for x, b in parsed.books_seen.items() if x in ext_to_event},
            "snapshots": res.snapshots,
            "unresolved": res.unresolved,
            "warnings": parsed.warnings[:50],
        }
        conn.execute(
            "UPDATE ingestion_run SET status = 'ok', finished_at = now(), stats = %s WHERE id = %s",
            (json.dumps(stats), run_id),
        )
    res.status = "ok"
    return res


# ---------------------------------------------------------------------- closing
def finalize_closing(conn: psycopg.Connection, config: Config, now: datetime, lookback_days: int = 7) -> dict:
    """For each kicked-off event with live odds and no live closing rows yet: if a poll landed in
    [kickoff - close_max_age, kickoff), copy each book's latest pre-kickoff price from that poll
    as is_closing (ts = the poll time). Books absent from that poll get no closing price, and
    an event with no qualifying poll gets none at all: a T-3h price is not a close."""
    s = config.settings.odds_api
    max_age = timedelta(minutes=s.close_max_age_minutes)
    events = conn.execute(
        """
        SELECT e.id, e.starts_at FROM event e
        WHERE e.starts_at <= %s AND e.starts_at > %s
          AND EXISTS (SELECT 1 FROM odds_snapshot o WHERE o.event_id = e.id AND o.source = %s AND NOT o.is_closing)
          AND NOT EXISTS (SELECT 1 FROM odds_snapshot o WHERE o.event_id = e.id AND o.source = %s AND o.is_closing)
        """,
        (now, now - timedelta(days=lookback_days), PROVIDER, PROVIDER),
    ).fetchall()
    out = {"closed": 0, "no_close_poll": 0, "rows": 0}
    for eid, kickoff in events:
        run = conn.execute(
            """
            SELECT (stats->>'polled_at')::timestamptz, stats->'events'->%s FROM ingestion_run
            WHERE source = %s AND status = 'ok' AND stats->'events' ? %s
              AND (stats->>'polled_at')::timestamptz >= %s AND (stats->>'polled_at')::timestamptz < %s
            ORDER BY (stats->>'polled_at')::timestamptz DESC LIMIT 1
            """,
            (str(eid), PROVIDER, str(eid), kickoff - max_age, kickoff),
        ).fetchone()
        if run is None:
            out["no_close_poll"] += 1
            continue
        polled_at, books = run
        cur = conn.execute(
            """
            INSERT INTO odds_snapshot (ts, ts_estimated, event_id, market_id, bookmaker_id, selection, price, is_closing, source)
            SELECT %s, false, o.event_id, o.market_id, o.bookmaker_id, o.selection, o.price, true, %s
            FROM (
              SELECT DISTINCT ON (o.market_id, o.bookmaker_id, o.selection) o.*
              FROM odds_snapshot o JOIN bookmaker b ON b.id = o.bookmaker_id
              WHERE o.event_id = %s AND o.source = %s AND NOT o.is_closing AND o.ts <= %s AND b.code = ANY(%s)
              ORDER BY o.market_id, o.bookmaker_id, o.selection, o.ts DESC
            ) o
            ON CONFLICT DO NOTHING
            """,
            (polled_at, PROVIDER, eid, PROVIDER, polled_at, list(books or [])),
        )
        out["closed"] += 1
        out["rows"] += cur.rowcount
    conn.commit()
    return out


# ---------------------------------------------------------------------- tick
@dataclass
class TickReport:
    now: datetime
    fixtures: dict[str, list[Fixture]] = field(default_factory=dict)
    needs: list[PollNeed] = field(default_factory=list)
    polls: list[PollResult] = field(default_factory=list)
    closing: dict = field(default_factory=dict)
    spent: int = 0
    remaining: int | None = None
    warnings: list[str] = field(default_factory=list)


def tick(
    conn: psycopg.Connection,
    config: Config,
    client: OddsApiClient | None = None,
    now: datetime | None = None,
    dry_run: bool = False,
    force: list[str] | None = None,
) -> TickReport:
    """dry_run: free calls only (fixture lists), report what would be polled. force: poll these
    competitions now regardless of the plan (still budget-checked, as a 'window' poll)."""
    s = config.settings.odds_api
    now = now or datetime.now(UTC)
    client = client or make_client(config)
    rep = TickReport(now)
    by_sport = {config.competition(c).provider_code(PROVIDER): c for c in s.competitions}
    for sport, comp in by_sport.items():
        try:
            rep.fixtures[sport] = [f for f in to_fixtures(client.events(sport).data) if f.commence > now]
        except OddsApiError as e:
            rep.warnings.append(f"{comp} fixtures: {e}")
    rep.needs = plan(rep.fixtures, last_polls(conn, list(by_sport)), now, s)
    for c in force or []:
        sport = config.competition(c).provider_code(PROVIDER)
        if all(n.sport != sport for n in rep.needs):
            rep.needs.append(PollNeed(sport, "window", now, ["forced"]))
    rep.spent, rep.remaining = ledger(conn, now)
    cost = B.call_cost(s.markets, len(config.provider_books(PROVIDER)))
    for need in rep.needs:
        comp = by_sport.get(need.sport) or next(
            c for c in force or [] if config.competition(c).provider_code(PROVIDER) == need.sport
        )
        d = B.decide(need.kind, cost, rep.spent, rep.remaining, now, s)
        if d.warn and not any("budget" in w for w in rep.warnings):
            rep.warnings.append(f"credit budget: {rep.spent + cost}/{s.monthly_credit_budget} used this month")
        if not d.allowed:
            rep.polls.append(PollResult(comp, need.sport, need.kind, "skipped", d.reason))
            continue
        if dry_run:
            rep.polls.append(PollResult(comp, need.sport, need.kind, "dry-run", "; ".join(need.reasons[:4]), cost))
            continue
        r = poll_competition(conn, config, client, comp, need.kind, now)
        rep.polls.append(r)
        rep.spent += r.credits
        rep.remaining = r.remaining if r.remaining is not None else rep.remaining
    if not dry_run:
        rep.closing = finalize_closing(conn, config, now)
    for w in rep.warnings:
        log.warning(w)
    return rep
