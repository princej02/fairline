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
from pathlib import Path

import psycopg

from fairline.core.config import Config
from fairline.ingest.the_odds_api import budget as B
from fairline.ingest.the_odds_api import derive as D
from fairline.ingest.the_odds_api.client import OddsApiClient, OddsApiError
from fairline.ingest.the_odds_api.parse import fixtures as to_fixtures
from fairline.ingest.the_odds_api.parse import parse_events, parse_odds
from fairline.ingest.the_odds_api.planner import Fixture, PollNeed, plan
from fairline.ingest.the_odds_api.resolve import PROVIDER, resolve_event, resolve_teams

log = logging.getLogger(__name__)


def make_client(config: Config) -> OddsApiClient:
    s = config.settings.odds_api
    return OddsApiClient(s.base_url, config.path(s.cache_dir), events_refresh=timedelta(hours=s.events_refresh_hours))


def ou_lines(config: Config) -> set[float]:
    return {float(m.split("_", 1)[1]) for m in config.settings.markets["enabled"] if m.startswith("OU_")}


# ---------------------------------------------------------------------- ledger
def ledger(conn: psycopg.Connection, now: datetime) -> B.Ledger:
    """Credits spent this calendar month, the API's last reported balance, and the last 7 days'
    closing-poll spend (the basis for reserving credits for the rest of the month's closes)."""
    spent, close_7d = conn.execute(
        """
        SELECT coalesce(sum((stats->>'credits')::int) FILTER (WHERE started_at >= %s), 0),
               coalesce(sum((stats->>'credits')::int) FILTER (WHERE started_at >= %s AND stats->>'kind' = 'close'), 0)
        FROM ingestion_run WHERE source = %s AND stats ? 'credits' AND started_at <= %s
        """,
        (B.month_start(now), now - timedelta(days=7), PROVIDER, now),
    ).fetchone()
    rem = conn.execute(
        "SELECT (stats->>'remaining')::int FROM ingestion_run WHERE source = %s AND stats->>'remaining' IS NOT NULL "
        "AND started_at <= %s ORDER BY started_at DESC LIMIT 1",
        (PROVIDER, now),
    ).fetchone()
    return B.Ledger(int(spent), rem[0] if rem else None, int(close_7d))


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
    conn.execute("DROP TABLE IF EXISTS _live_in")  # ON COMMIT DROP does not fire inside an outer transaction
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
    derived: int = 0  # events given a derived O/U reference
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
    pair = D.config_for(config)
    parsed = parse_odds(resp.data, rev, ou_lines(config), resp.fetched_at, source_book=pair[0] if pair else None)
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
        derived_skipped: dict[str, int] = {}
        if pair:
            rho = D.latest_rho(conn, comp_code, s.derived_rho_default)
            for target in sorted(ou_lines(config)):
                d = D.derive_totals(
                    parsed, ext_to_event, pair[0], pair[1], target, rho, config.settings.edge.devig_method
                )
                rows += d.rows
                D.store_records(conn, sport_id, pair[1], pair[0], target, d.records)
                res.derived += len(d.records)
                for k, v in d.skipped.items():
                    derived_skipped[k] = derived_skipped.get(k, 0) + v
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
            "derived": res.derived,
            "derived_skipped": derived_skipped,
            "unresolved": res.unresolved,
            "warnings": parsed.warnings[:50],
        }
        conn.execute(
            "UPDATE ingestion_run SET status = 'ok', finished_at = now(), stats = %s WHERE id = %s",
            (json.dumps(stats), run_id),
        )
    res.status = "ok"
    return res


# ---------------------------------------------------------------------- fixture sync (free)
@dataclass
class SyncResult:
    competition: str
    fixtures: int = 0
    events: int = 0
    created: int = 0
    linked: int = 0
    unresolved: list[str] = field(default_factory=list)
    auto_matched: dict[str, str] = field(default_factory=dict)
    error: str = ""


def sync_fixtures(
    conn: psycopg.Connection, config: Config, comp_code: str, data: list[dict], now: datetime
) -> SyncResult:
    """Resolve team names and create/link scheduled events from a free /events response, so
    unmapped names reach the review queue before any paid poll needs them."""
    comp = config.competition(comp_code)
    out = SyncResult(comp_code)
    evs = [e for e in parse_events(data) if e.commence > now]
    out.fixtures = len(evs)
    if not evs:
        return out
    with conn.transaction():
        sport_id, comp_id = conn.execute(
            "SELECT sport_id, id FROM competition WHERE code = %s", (comp_code,)
        ).fetchone()
        teams = resolve_teams(conn, comp, sport_id, [n for e in evs for n in (e.home, e.away)], now)
        out.unresolved, out.auto_matched = sorted(teams.unresolved), teams.auto_matched
        for e in evs:
            if e.home in teams.ids and e.away in teams.ids:
                _, how = resolve_event(
                    conn, comp, comp_id, e.external_id, teams.ids[e.home], teams.ids[e.away], e.commence
                )
                out.events += 1
                out.created += how == "created"
                out.linked += how == "linked"
    return out


def sync_all(
    conn: psycopg.Connection,
    config: Config,
    client: OddsApiClient | None = None,
    now: datetime | None = None,
    force: bool = False,
) -> list[SyncResult]:
    now = now or datetime.now(UTC)
    client = client or make_client(config)
    out = []
    s = config.settings.odds_api
    for c in s.fixture_competitions or s.competitions:
        try:
            data = client.events(config.competition(c).provider_code(PROVIDER), force=force).data
        except OddsApiError as e:
            out.append(SyncResult(c, error=str(e)))
            continue
        out.append(sync_fixtures(conn, config, c, data, now))
    return out


# ---------------------------------------------------------------------- replay (free)
def rederive_from_cache(conn: psycopg.Connection, config: Config, files: list[Path]) -> dict:
    """Re-run the O/U derivation over cached /odds responses (no API call, no credits). Each derived
    snapshot keeps the source prices' own last_update ts (capped at that poll's fetch time), so a
    replay never makes a price look observable earlier than it was. Idempotent."""
    pair = D.config_for(config)
    if not pair:
        return {"files": 0}
    books = config.provider_books(PROVIDER)
    rev = {v: k for k, v in books.items()}
    sport_id = conn.execute("SELECT id FROM sport WHERE code = 'football'").fetchone()[0]
    s = config.settings.odds_api
    out = {"files": 0, "derived": 0, "inserted": 0, "skipped": {}}
    by_sport = {config.competition(c).provider_code(PROVIDER): c for c in s.competitions}
    for f in files:
        meta = json.loads(f.with_name(f.name.replace(".json", ".meta.json")).read_text())
        sport = f.parent.parent.name
        comp = by_sport.get(sport)
        if comp is None:
            continue
        parsed = parse_odds(
            json.loads(f.read_bytes()), rev, ou_lines(config), datetime.fromisoformat(meta["fetched_at"]), pair[0]
        )
        ext_to_event = dict(
            conn.execute(
                "SELECT external_id, event_id FROM event_external_id WHERE provider = %s AND external_id = ANY(%s)",
                (PROVIDER, [e.external_id for e in parsed.events]),
            ).fetchall()
        )
        rho = D.latest_rho(conn, comp, s.derived_rho_default)
        with conn.transaction():
            for target in sorted(ou_lines(config)):
                d = D.derive_totals(
                    parsed, ext_to_event, pair[0], pair[1], target, rho, config.settings.edge.devig_method
                )
                D.store_records(conn, sport_id, pair[1], pair[0], target, d.records)
                out["inserted"] += _insert_snapshots(conn, sport_id, d.rows)
                out["derived"] += len(d.records)
                for k, v in d.skipped.items():
                    out["skipped"][k] = out["skipped"].get(k, 0) + v
        conn.commit()
        out["files"] += 1
    return out


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
        conn.execute(
            """
            INSERT INTO odds_derivation (event_id, bookmaker_id, is_closing, ts, market_id, source_book_id, source_line,
              source_over, source_under, lambda_home, lambda_away, rho, p_over, draw_fit_error, method)
            SELECT DISTINCT ON (d.bookmaker_id, d.market_id) d.event_id, d.bookmaker_id, true, %s, d.market_id,
                   d.source_book_id, d.source_line, d.source_over, d.source_under, d.lambda_home, d.lambda_away, d.rho,
                   d.p_over, d.draw_fit_error, d.method
            FROM odds_derivation d JOIN bookmaker b ON b.id = d.bookmaker_id
            WHERE d.event_id = %s AND NOT d.is_closing AND d.ts <= %s AND b.code = ANY(%s)
            ORDER BY d.bookmaker_id, d.market_id, d.ts DESC
            ON CONFLICT DO NOTHING
            """,
            (polled_at, eid, polled_at, list(books or [])),
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
    syncs: list[SyncResult] = field(default_factory=list)
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
    fixture_only = {config.competition(c).provider_code(PROVIDER): c for c in s.fixture_competitions} | by_sport
    for sport, comp in fixture_only.items():
        try:
            ev = client.events(sport)
        except OddsApiError as e:
            rep.warnings.append(f"{comp} fixtures: {e}")
            continue
        if sport in by_sport:  # only paid-poll leagues are planned; the rest are synced for free
            rep.fixtures[sport] = [f for f in to_fixtures(ev.data) if f.commence > now]
        if not ev.from_cache and not dry_run:  # fresh list (every events_refresh_hours): free name/event sync
            sr = sync_fixtures(conn, config, comp, ev.data, now)
            rep.syncs.append(sr)
            if sr.unresolved:
                rep.warnings.append(f"{comp}: unmapped team names {sr.unresolved}; see `fairline odds aliases`")
    rep.needs = plan(rep.fixtures, last_polls(conn, list(by_sport)), now, s)
    for c in force or []:
        sport = config.competition(c).provider_code(PROVIDER)
        if all(n.sport != sport for n in rep.needs):
            rep.needs.append(PollNeed(sport, "window", now, ["forced"]))
    led = ledger(conn, now)
    rep.spent, rep.remaining = led.spent_month, led.remaining
    cost = B.call_cost(s.markets, len(config.provider_books(PROVIDER)))
    for need in rep.needs:
        comp = by_sport.get(need.sport) or next(
            c for c in force or [] if config.competition(c).provider_code(PROVIDER) == need.sport
        )
        d = B.decide(need.kind, cost, led, now, s)
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
        led = B.Ledger(rep.spent, rep.remaining, led.close_7d + (r.credits if r.kind == "close" else 0))
    if not dry_run:
        rep.closing = finalize_closing(conn, config, now)
    for w in rep.warnings:
        log.warning(w)
    return rep
